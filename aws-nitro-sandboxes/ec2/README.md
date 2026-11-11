# AWS Nitro Sandboxes on Amazon EC2

Part 2 of the AWS Nitro Sandboxes series. One `m8g.xlarge` with Nitro Enclaves turned on, and Firecracker 1.18.0 driving the sandboxes directly. The steps launch the parent, reserve the memory pool the docs skip, boot a sandbox in direct and confidential mode and look at it from the parent, get an attestation document out of the Nitro Secure Module (NSM) with [`../nsm/nsm.py`](../nsm/nsm.py), release a KMS secret only to a measured sandbox, try what KMS refuses, push the density limits, and compare a sandbox with a container.

Blog post: [AWS Nitro Sandboxes on EC2: Hands-On with Firecracker and Attestation](https://my2cents.ai/deep-dive/nitro-sandboxes-ec2)

Video walkthrough: https://youtu.be/_ausIROu0Gw

## Where the commands run

- **Your machine:** Steps 1, 2 and 11, and Cleanup. You need the tools in the [series prerequisites](../README.md#prerequisites). Run the commands in `bash` (zsh splits words differently).
- **The parent instance, as root:** every other step. Open a shell with Session Manager:

```bash
aws ssm start-session --target "$INSTANCE_ID"
sudo -i
```

On the parent, the AWS CLI that ships with Amazon Linux 2023 is enough (it was 2.38.0 in the recorded run); `kms decrypt --recipient` works with it.

| File | What it is |
|---|---|
| `kms-policy.json` | Key policy template for Step 11: the account gets `kms:*`, the parent's role gets `kms:Decrypt` only for a recipient whose PCR0 matches `<PCR0>` |
| [`../nsm/nsm.py`](../nsm/nsm.py) | The NSM client the guest runs in Steps 10 to 13 |

Files your machine writes go to `build/` (gitignored).

## Step 1: An instance role for Session Manager (your machine)

No key pair and no inbound rules: Session Manager is the only way in.

```bash
cd aws-nitro-sandboxes/ec2 && mkdir -p build
export AWS_REGION=us-east-1
aws iam create-role --role-name nitro-sandbox-ec2 \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name nitro-sandbox-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name nitro-sandbox-ec2
aws iam add-role-to-instance-profile --instance-profile-name nitro-sandbox-ec2 --role-name nitro-sandbox-ec2
aws iam wait instance-profile-exists --instance-profile-name nitro-sandbox-ec2
```

## Step 2: Launch the parent with Nitro Enclaves enabled

**Console (the path used in the recorded run):** EC2 > **Launch instance**.

1. Name it `nitro-sandbox-ec2`. The Quick Start default is "Amazon Linux 2023 kernel-6.18 AMI"; set the architecture to **64-bit (Arm)**.
2. Instance type `m8g.xlarge`.
3. Key pair: **Proceed without a key pair**. Network settings: untick the wizard's "Allow SSH traffic" rule. The wizard creates a `launch-wizard-N` security group with no inbound rules.
4. Storage: 20 GiB gp3.
5. **Advanced details**: IAM instance profile `nitro-sandbox-ec2`, and **Nitro Enclave: Enable**. That one setting is the whole feature switch. A few fields below, the console says "Nested virtualization is not supported with the selected instance type": sandboxes don't use it.

**CLI equivalent:** `--enclave-options Enabled=true` is the switch. This uses the default VPC, whose subnets give the instance a public IP for its downloads.

```bash
VPC_ID=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SG_ID=$(aws ec2 create-security-group --group-name nitro-sandbox-ec2 \
  --description "Nitro Sandboxes demo: no inbound rules" --vpc-id "$VPC_ID" --query GroupId --output text)
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.18-arm64 \
  --instance-type m8g.xlarge \
  --iam-instance-profile Name=nitro-sandbox-ec2 \
  --security-group-ids "$SG_ID" \
  --enclave-options Enabled=true \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=nitro-sandbox-ec2}]' \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
aws ec2 describe-instance-attribute --instance-id "$INSTANCE_ID" --attribute enclaveOptions --query EnclaveOptions
```

```json
{
    "Enabled": true
}
```

If `run-instances` says the instance profile is invalid, IAM hasn't propagated it yet: wait a few seconds and run it again. If you launched from the console, set `INSTANCE_ID` from the console, or with `aws ec2 describe-instances --filters Name=tag:Name,Values=nitro-sandbox-ec2 Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text`.

The parent needs the AL2023 `kernel6.18` package `6.18.51-120.162` or later; the AMI above booted `6.18.56-125.164.amzn2023.aarch64`, so there is nothing to install. Session Manager was online about 30 seconds after launch.

## Step 3: The parent before any sandbox

```bash
uname -r
ls -la /dev/nitro_enclaves
NE=/sys/class/misc/nitro_enclaves
for f in cpu_pool/total cpu_pool/free mem_pool/total pcie_slots pcie_slots_total pcie_slots_in_use; do echo "$f: $(cat $NE/$f)"; done
grep CmaTotal /proc/meminfo
grep -E "CONFIG_NSM|CONFIG_NITRO_ENCLAVES" /boot/config-$(uname -r)
ip -br link
which nft unsquashfs
```

```text
6.18.56-125.164.amzn2023.aarch64
crw-rw----. 1 root root 10, 259 Nov  9 22:59 /dev/nitro_enclaves
cpu_pool/total: 3
cpu_pool/free: 3
mem_pool/total: 0
pcie_slots: 4
pcie_slots_total: 20
pcie_slots_in_use: 0
CmaTotal:          65536 kB
CONFIG_NSM=m
CONFIG_NITRO_ENCLAVES=y
lo               UNKNOWN        00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>
ens34            UP             0a:ff:xx:xx:xx:xx <BROADCAST,MULTICAST,UP,LOWER_UP>
which: no nft in (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin)
which: no unsquashfs in (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin)
```

| Value | What it means |
|---|---|
| `cpu_pool/total: 3` | Cores available for dedicated-core sandboxes; CPU 0's core stays with the parent |
| `pcie_slots: 4` | Sandboxes the parent can hold right now |
| `pcie_slots_total: 20` | The most it can ever hold: 5 × its 4 vCPUs |
| `mem_pool/total: 0` | No memory reserved for sandboxes yet (Step 4) |
| `ens34` | The uplink. The Firecracker network guide hard-codes `eth0` |

The driver is built into the kernel, and the Nitro Enclaves CLI (`aws-nitro-enclaves-cli`) is not installed. You don't need it: Firecracker talks to `/dev/nitro_enclaves` directly.

## Step 4: Reserve the memory pool and reboot

Sandbox memory has to be physically contiguous. Without a reservation the driver takes it from the kernel's contiguous memory area, which on this AMI is the 64 MiB from `numa_cma=1:64M` on the kernel command line. A 1 GiB sandbox then dies 50 ms after start, with this in its console log:

```text
CMA allocation failed: size=1073741824 bytes gpa=0x40000000 parent_numa=0 prot=0x3: Cannot create mmap region: Out of memory (os error 12)
```

Reserve a pool at boot. 12 GiB leaves the parent about 4 GiB:

```bash
grubby --update-kernel=ALL --args="nitro_enclaves.mempool=12G"
grubby --info=DEFAULT | grep args
systemctl reboot
```

Reconnect with Session Manager after a minute, `sudo -i`, and check:

```bash
grep -o 'nitro_enclaves[^ ]*' /proc/cmdline
grep -i cma /proc/meminfo
echo "mem_pool/free: $(( $(cat /sys/class/misc/nitro_enclaves/mem_pool/free) / 1048576 )) MiB"
free -m | head -2
```

```text
nitro_enclaves.mempool=12G
CmaTotal:       12648448 kB
CmaFree:        12276716 kB
mem_pool/free: 12284 MiB
               total        used        free      shared  buff/cache   available
Mem:           15671         192       15173           0         306       15278
```

The pool is CMA, so the parent keeps using that memory while no sandbox holds it: `free` still shows about 15 GiB free. A sandbox's memory leaves the parent only when the sandbox starts. AWS's EKS node image does the same with `nitro_enclaves.mempool=95%-1G`.

Do this before Step 6: the TAP device and the nftables rules don't survive a reboot.

## Step 5: Firecracker and a guest

Firecracker 1.18.0 is the first release with Nitro Sandboxes support, and its aarch64 tarball runs on Graviton. For a quick guest, Firecracker's CI publishes a kernel and an Ubuntu root filesystem; on aarch64 its `vmlinux` is already an EFI-bootable image.

```bash
dnf install -y nftables squashfs-tools e2fsprogs
mkdir -p /opt/sandbox && cd /opt/sandbox

FC=v1.18.0; ARCH=$(uname -m)
curl -fsSL https://github.com/firecracker-microvm/firecracker/releases/download/$FC/firecracker-$FC-$ARCH.tgz | tar -xz
mv release-$FC-$ARCH/firecracker-$FC-$ARCH /usr/local/bin/firecracker
firecracker --version | head -1

# Guest kernel and rootfs from Firecracker's CI, the 20261105 build used in the recorded run
S3=https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20261105-6ea6634994fa-0/$ARCH
curl -fsSLO $S3/vmlinux-6.18.56
curl -fsSL -o ubuntu-24.04.squashfs $S3/ubuntu-24.04.squashfs
rm -rf squashfs-root && unsquashfs -q ubuntu-24.04.squashfs
ssh-keygen -q -f id_rsa -N ""
mkdir -p squashfs-root/root/.ssh && cp id_rsa.pub squashfs-root/root/.ssh/authorized_keys
chown -R root:root squashfs-root
truncate -s 1G ubuntu-24.04.ext4
mkfs.ext4 -q -d squashfs-root -F ubuntu-24.04.ext4
file vmlinux-6.18.56
```

```text
Firecracker v1.18.0
vmlinux-6.18.56: Linux kernel ARM64 boot executable Image, little-endian, 4K pages
```

To find a newer CI build, list the prefixes (the newest sorts last); the kernel's file name changes with the build:

```bash
curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min?list-type=2&prefix=firecracker-ci/&delimiter=/" \
  | grep -oP '(?<=<Prefix>)firecracker-ci/[0-9]{8}-[^/]+/(?=</Prefix>)' | sort | tail -1
```

## Step 6: The guest network

A TAP device that the parent NATs out of its own uplink. Look the uplink up instead of assuming `eth0`:

```bash
HOST_IFACE=$(ip -j route list default | jq -r '.[0].dev')
echo "uplink: $HOST_IFACE"
ip tuntap add tap0 mode tap
ip addr add 172.16.0.1/30 dev tap0
ip link set tap0 up
echo 1 > /proc/sys/net/ipv4/ip_forward
nft add table firecracker
nft 'add chain firecracker postrouting { type nat hook postrouting priority srcnat; policy accept; }'
nft 'add chain firecracker filter { type filter hook forward priority filter; policy accept; }'
nft add rule firecracker postrouting ip saddr 172.16.0.0/24 oifname $HOST_IFACE counter masquerade
nft add rule firecracker filter iifname '"tap*"' oifname $HOST_IFACE accept
```

```text
uplink: ens34
```

The guest's rootfs configures `eth0` as `172.16.0.2/30` from its MAC address. Every later step reaches it with these two variables; set them again whenever you open a new shell:

```bash
SSH="ssh -i /opt/sandbox/id_rsa -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=1 root@172.16.0.2"
SCP="scp -i /opt/sandbox/id_rsa -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
```

Until Step 15, a guest that adds a default route reaches the parent's instance metadata service through this NAT, and with it the parent's role credentials.

## Step 7: Boot a sandbox in direct mode

The only addition to an ordinary Firecracker configuration is the `enclave` key. `{}` is confidential mode, the default; `{"direct_mode": true}` is direct mode.

```bash
cd /opt/sandbox
cat > direct.json <<'EOF'
{
  "boot-source": { "kernel_image_path": "/opt/sandbox/vmlinux-6.18.56" },
  "machine-config": { "vcpu_count": 2, "mem_size_mib": 1024 },
  "drives": [
    { "drive_id": "rootfs", "path_on_host": "/opt/sandbox/ubuntu-24.04.ext4",
      "is_root_device": true, "is_read_only": false }
  ],
  "network-interfaces": [
    { "iface_id": "eth0", "guest_mac": "06:00:AC:10:00:02", "host_dev_name": "tap0" }
  ],
  "enclave": { "direct_mode": true }
}
EOF
rm -f /tmp/fc.sock
T0=$(date +%s.%N)
setsid nohup firecracker --api-sock /tmp/fc.sock --config-file direct.json > direct-console.log 2>&1 < /dev/null &
until $SSH true 2>/dev/null; do sleep 0.2; done
echo "time to SSH: $(echo "$(date +%s.%N) - $T0" | bc) s"
curl -s --unix-socket /tmp/fc.sock http://localhost/enclave; echo
```

```text
time to SSH: 1.700868036 s
{"debug_mode":false,"direct_mode":true,"cpu_overcommit":false,"cpu_accounting":false,"hotplug_mode":false,"numa_nodes":[{"parent_numa":0,"vcpu_count":2,"mem_size_mib":1024}]}
```

If the loop never ends, Firecracker has exited: `tail -5 direct-console.log` shows why (Step 4's CMA error lands there). Inside, it's a separate machine:

```bash
$SSH 'uname -rv; cat /proc/sys/kernel/random/boot_id; cut -d" " -f1 /proc/uptime; nproc; grep MemTotal /proc/meminfo; ls /sys/block | tr "\n" " "; echo; ls /dev/nsm; dmesg | grep -E "Booting Linux|efi: EFI"'
```

```text
6.18.56+ #1 SMP Thu Nov  5 13:12:30 UTC 2026
3065962e-76ae-4139-afc3-bf15e94376cf
2.09
2
MemTotal:         993832 kB
loop0 loop1 loop2 loop3 loop4 loop5 loop6 loop7 vda
ls: cannot access '/dev/nsm': No such file or directory
[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x410fd4f1]
[    0.000000] efi: EFI v2.7 by EDK II
```

Its own kernel, `boot_id` and uptime, one disk (`vda`), booted through UEFI. On arm64, `/sys/class/dmi/id/product_name` (the `*.enclave` check the docs give for x86) doesn't exist in the guest. From the parent:

```bash
NE=/sys/class/misc/nitro_enclaves
for f in cpu_pool/in_use cpu_pool/free pcie_slots_in_use; do echo "$f: $(cat $NE/$f)"; done
echo "mem_pool/free: $(( $(cat $NE/mem_pool/free) / 1048576 )) MiB"
nproc; cat /sys/devices/system/cpu/online
```

```text
cpu_pool/in_use: 2
cpu_pool/free: 1
pcie_slots_in_use: 1
mem_pool/free: 11258 MiB
4
0-3
```

The two donated cores don't disappear from `nproc` or `/sys/devices/system/cpu/online`, although the docs say they're removed from the parent's scheduler. Stop the sandbox:

```bash
$SSH poweroff 2>/dev/null; sleep 2; pkill -x firecracker
cat $NE/pcie_slots_in_use
```

From here on, the same start-and-wait lines are wrapped in two shell functions. Define them in the shell you use for the rest of the steps:

```bash
fc_boot() {  # fc_boot <config.json>: start Firecracker in the background, wait for SSH, print the time
  rm -f /tmp/fc.sock; local t0=$(date +%s.%N)
  setsid nohup firecracker --api-sock /tmp/fc.sock --config-file "$1" > "${1%.json}-console.log" 2>&1 < /dev/null &
  until $SSH true 2>/dev/null; do
    pgrep -x firecracker >/dev/null || { echo "firecracker exited:"; tail -3 "${1%.json}-console.log"; return 1; }
    sleep 0.2
  done
  echo "[$1] time to SSH: $(echo "$(date +%s.%N) - $t0" | bc) s"
}
fc_stop() { $SSH poweroff 2>/dev/null; sleep 2; pkill -x firecracker; sleep 1; }
```

## Step 8: Direct and confidential mode, seen from the parent

The two modes differ in whether the parent can read the guest's memory. The guest writes a unique marker to `/dev/shm` 2,000 times; the parent finds the Firecracker process's 1 GiB mapping of `/dev/nitro_enclaves` and greps it through `/proc/<pid>/mem`. (`gcore` won't do: it skips that mapping, and its core file is 5 MB.)

```bash
cd /opt/sandbox
jq '.enclave = {}' direct.json > confidential.json
for mode in direct confidential; do
  fc_boot $mode.json
  MARK="my2cents-secret-$(date +%s%N)"
  $SSH "for i in \$(seq 1 2000); do echo $MARK; done > /dev/shm/secret"
  FCPID=$(pgrep -x firecracker)
  echo "firecracker RSS: $(ps -o rss= -p $FCPID) KiB"
  RANGE=$(awk '$6 == "/dev/nitro_enclaves" { split($1, a, "-"); if (strtonum("0x" a[2]) - strtonum("0x" a[1]) >= 268435456) { print $1; exit } }' /proc/$FCPID/maps)
  echo "guest memory mapping: $RANGE"
  echo "copies of the secret the parent can read: $(dd if=/proc/$FCPID/mem bs=1M iflag=skip_bytes,count_bytes \
    skip=$((0x${RANGE%-*})) count=$((0x${RANGE#*-} - 0x${RANGE%-*})) 2>/dev/null | grep -a -c "$MARK")"
  $SSH 'for d in /sys/bus/pci/devices/*; do v=$(cat $d/vendor):$(cat $d/device); case $v in 0x1af4:*) echo "virtio $(basename $d) $v";; esac; done; dmesg | grep -i dmb'
  fc_stop
done
```

```text
[direct.json] time to SSH: 1.700868036 s
firecracker RSS: 1053496 KiB
guest memory mapping: ffff57200000-ffff97200000
copies of the secret the parent can read: 1993
virtio 0000:04:00.0 0x1af4:0x1042
virtio 0000:05:00.0 0x1af4:0x1041
[confidential.json] time to SSH: 1.868795533 s
firecracker RSS: 1086264 KiB
guest memory mapping: ffff50a00000-ffff90a00000
copies of the secret the parent can read: 0
virtio 0000:00:02.0 0x1af4:0x1061
virtio 0000:04:00.0 0x1af4:0x1042
virtio 0000:05:00.0 0x1af4:0x1041
[    0.829937] virtio_blk virtio1: Enabled 16777216-byte Virtio DMB (lockless bitmap, 4096 slots) for 0000:04:00.0
[    0.838449] virtio_net virtio2: Enabled 16777216-byte Virtio DMB (lockless bitmap, 4096 slots) for 0000:05:00.0
```

The `dd` read succeeds in both modes (1 GiB in about 2 seconds); in confidential mode it just doesn't return the guest's data. Confidential mode adds the NSM (`1af4:1061`) and a 16 MiB device memory buffer (DMB) per virtio device, which is how I/O crosses to the parent.

## Step 9: An AL2023 guest with an NSM

The Firecracker CI kernel is built without the NSM driver. The AL2023 kernel has it as a module (`CONFIG_NSM=m`, Step 3), and its `/boot/vmlinuz` is already an EFI application, so boot the parent's own kernel as the guest, with an initramfs that loads the virtio and NSM drivers:

```bash
cd /opt/sandbox
dracut -q --force --no-hostonly --no-hostonly-cmdline \
  --add-drivers "virtio_pci virtio_blk virtio_net ext4" \
  --force-drivers "nsm virtio_net" \
  /opt/sandbox/initrd-al-guest.img $(uname -r)
cp /boot/vmlinuz-$(uname -r) /opt/sandbox/vmlinuz-al
cat > conf.json <<'EOF'
{
  "boot-source": {
    "kernel_image_path": "/opt/sandbox/vmlinuz-al",
    "initrd_path": "/opt/sandbox/initrd-al-guest.img",
    "boot_args": "panic=1 initcall_blacklist=i8042_init net.ifnames=0"
  },
  "machine-config": { "vcpu_count": 2, "mem_size_mib": 1024 },
  "drives": [ { "drive_id": "rootfs", "path_on_host": "/opt/sandbox/ubuntu-24.04.ext4", "is_root_device": true, "is_read_only": false } ],
  "network-interfaces": [ { "iface_id": "eth0", "guest_mac": "06:00:AC:10:00:02", "host_dev_name": "tap0" } ],
  "enclave": {}
}
EOF
fc_boot conf.json
$SSH 'uname -r; ls -la /dev/nsm; lsmod | grep -E "^(nsm|virtio_net|virtio_blk) "; ip -br addr show eth0'
```

```text
[conf.json] time to SSH: 3.182338953 s
6.18.56-125.164.amzn2023.aarch64
crw-rw-rw- 1 root root 10, 259 Nov  9 23:10 /dev/nsm
virtio_blk             32768  1
virtio_net            106496  0
nsm                    16384  0
eth0             UP             172.16.0.2/30 fe80::400:acff:fe10:2/64
```

The initramfs is about 17 MB, and boot to SSH takes about 3.1 s instead of 1.9 s. `net.ifnames=0` keeps the interface name `eth0`, which the rootfs's network setup expects. The Ubuntu rootfs already has `python3`, `openssl` and `scp`, which the next step needs.

## Step 10: An attestation document

There is no CLI for the NSM, so the guest runs [`nsm.py`](../nsm/nsm.py), a stdlib-only client. Fetch it onto the parent and copy it in. The guest makes an RSA key pair and asks the NSM for a document that carries the public key.

The private key goes to `/dev/shm`, in the guest's memory. The root filesystem is `ubuntu-24.04.ext4`, a file on the parent's disk that the guest writes through the DMB in clear, so a key written to `/root` would end up readable on the parent.

```bash
cd /opt/sandbox
curl -fsSL -o nsm.py https://raw.githubusercontent.com/fabiodouek/my2centsai-blog-samples/main/aws-nitro-sandboxes/nsm/nsm.py
ATTEST='cd /root && openssl genrsa -out /dev/shm/key.pem 2048 2>/dev/null && openssl rsa -in /dev/shm/key.pem -pubout -outform DER -out pub.der 2>/dev/null && python3 nsm.py attest pub.der att.doc >/dev/null && python3 nsm.py pcrs att.doc'
$SCP nsm.py root@172.16.0.2:/root/nsm.py
$SSH "$ATTEST"
$SCP root@172.16.0.2:/root/att.doc conf1.att.doc
```

```text
module_id: <instance-id>-enc01a1c2952d97ba2e digest: SHA384
PCR0: ab0079bbe9fd7293742bd43e00f042d29abf9ba12bcefd12dba46e160b7287aeeb07d8313aa536889607bb60963f5155
PCR1: 952d3ec27683a000d4cbc4ea0930e79b1d751308ff06bd5ea93fceee8d24d73886a76bd322db697cf62bfe5de28c4393
PCR2: 21b9efbc184807662e966d34f390821309eeac6802309798826296bf3e8bec7c10edb30948c90ba67310f7b964fc500a
PCR3: a88f44c2…def656
PCR4: d959f079…26718c
PCR5: cec03918…2f2d61
PCR6: b0f1f813…ffde8d
```

The document is about 4.7 KB, a COSE_Sign1 structure; `nsm.py pcrs` decodes its payload but doesn't verify the signature (KMS does that in Step 12). Your PCR0 to PCR2 will probably differ from these: they measure the boot image Firecracker builds from your kernel, initramfs and command line.

PCR3 and PCR4 identify the parent, as they do for Nitro Enclaves: SHA-384 over 48 zero bytes followed by the parent's IAM role ARN (PCR3) or its instance ID (PCR4). Check PCR4 on the parent:

```bash
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
python3 -c 'import hashlib, sys; print("PCR4:", hashlib.sha384(bytes(48) + sys.argv[1].encode()).hexdigest())' "$IID"
```

Firecracker doesn't report the measurements, but they are deterministic. Boot the same configuration again and compare:

```bash
fc_stop
fc_boot conf.json
$SSH "$ATTEST" | grep -E 'module_id|PCR0'
$SCP root@172.16.0.2:/root/att.doc conf2.att.doc
```

```text
module_id: <instance-id>-enc01a1c2954d76c649 digest: SHA384
PCR0: ab0079bbe9fd7293742bd43e00f042d29abf9ba12bcefd12dba46e160b7287aeeb07d8313aa536889607bb60963f5155
```

A new sandbox (`module_id`), the same PCR0, PCR1 and PCR2. That lets you read them once from inside and pin them in a key policy, which is fine for a demo; in production you would build the image yourself and compute them offline. Leave this sandbox running: its private key is the one Step 12 needs.

## Step 11: A KMS key that only a measured sandbox can use (your machine)

The key policy gives the parent's role `kms:Decrypt` only when the request carries an attestation document whose image measurement (`kms:RecipientAttestation:ImageSha384`, which is PCR0) matches.

```bash
cd aws-nitro-sandboxes/ec2
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
PCR0=ab0079bbe9fd7293742bd43e00f042d29abf9ba12bcefd12dba46e160b7287aeeb07d8313aa536889607bb60963f5155   # yours, from Step 10
sed -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" -e "s/<PCR0>/$PCR0/" kms-policy.json > build/kms-policy.json
KEY_ID=$(aws kms create-key --description "Nitro Sandboxes on EC2 demo" \
  --policy file://build/kms-policy.json --query KeyMetadata.KeyId --output text)
aws kms create-alias --alias-name alias/nitro-sandbox-ec2 --target-key-id "$KEY_ID"
printf '%s' 'the launch codes are 0000' > build/secret.txt
aws kms encrypt --key-id "$KEY_ID" --plaintext fileb://build/secret.txt --query CiphertextBlob --output text
```

Copy the base64 that `encrypt` prints into the parent's shell:

```bash
echo '<CIPHERTEXT_BASE64>' | base64 -d > /opt/sandbox/secret.enc
```

## Step 12: Decrypt through the parent

The parent relays the sandbox's attestation document to KMS with its own role. KMS checks the document's signature and PCR0, and encrypts the answer to the public key inside the document, so the parent passes along something it can't open.

```bash
cd /opt/sandbox
export AWS_DEFAULT_REGION=us-east-1

# 1. The parent asks with its own role and no attestation
aws kms decrypt --ciphertext-blob fileb://secret.enc --query Plaintext --output text

# 2. The parent relays the sandbox's document as the recipient
aws kms decrypt --ciphertext-blob fileb://secret.enc \
  --recipient "KeyEncryptionAlgorithm=RSAES_OAEP_SHA_256,AttestationDocument=$(base64 -w0 conf2.att.doc)" \
  --output json > resp.json
jq -r '"Plaintext: \(.Plaintext)  CiphertextForRecipient: \(.CiphertextForRecipient | length) base64 chars"' resp.json
jq -r .CiphertextForRecipient resp.json | base64 -d > cfr.der

# 3. The parent tries to open it with a key of its own
openssl genrsa -out parent.pem 2048 2>/dev/null
openssl cms -decrypt -inform DER -in cfr.der -inkey parent.pem

# 4. The sandbox opens it with the key that never left its memory
$SCP cfr.der root@172.16.0.2:/root/cfr.der
$SSH 'cd /root && openssl cms -decrypt -inform DER -in cfr.der -inkey /dev/shm/key.pem; echo'
```

```text
aws: [ERROR]: An error occurred (AccessDeniedException) when calling the Decrypt operation: User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/nitro-sandbox-ec2/<instance-id> is not authorized to perform: kms:Decrypt ...
Plaintext: null  CiphertextForRecipient: 636 base64 chars
Error decrypting CMS structure
...:error:1C800064:Provider routines:ossl_cipher_unpadblock:bad decrypt:providers/implementations/ciphers/ciphercommon_block.c:112:
the launch codes are 0000
```

`AttestationDocument=fileb://att.doc` inside the `--recipient` shorthand fails with `Invalid base64: "fileb://..."`. Pass the base64 inline, as above.

## Step 13: What KMS refuses

One helper for the four attempts; it prints the length of `CiphertextForRecipient` when KMS answers:

```bash
cd /opt/sandbox
kms_try() {
  aws kms decrypt --ciphertext-blob fileb://secret.enc \
    --recipient "KeyEncryptionAlgorithm=RSAES_OAEP_SHA_256,AttestationDocument=$(base64 -w0 "$1")" \
    --query 'length(CiphertextForRecipient)' --output text 2>&1 | sed 's/User: arn[^ ]* //' | cut -c1-160
}

# A. Replay: a valid document from a sandbox that no longer exists
fc_stop
fc_boot conf.json && $SSH "$ATTEST" | grep PCR0
$SCP root@172.16.0.2:/root/att.doc replay.att.doc
fc_stop                                    # that sandbox's private key is gone with it
echo "age: $(( $(date +%s) - $(stat -c %Y replay.att.doc) )) s"; kms_try replay.att.doc

# B. Same kernel and initramfs, one extra boot argument
jq '."boot-source".boot_args += " quiet"' conf.json > conf-quiet.json
fc_boot conf-quiet.json && $SSH "$ATTEST" | grep PCR0
$SCP root@172.16.0.2:/root/att.doc quiet.att.doc; kms_try quiet.att.doc
fc_stop

# C. Same image, debug mode
jq '.enclave = {"debug_mode": true}' conf.json > conf-debug.json
fc_boot conf-debug.json && $SSH "$ATTEST" | grep PCR0
$SCP root@172.16.0.2:/root/att.doc debug.att.doc; kms_try debug.att.doc
fc_stop

# D. A valid document with one byte changed
cp conf2.att.doc bad.att.doc
printf '\x00' | dd of=bad.att.doc bs=1 seek=200 conv=notrunc 2>/dev/null
kms_try bad.att.doc
```

The `time to SSH` lines are left out here:

```text
PCR0: ab0079bbe9fd7293742bd43e00f042d29abf9ba12bcefd12dba46e160b7287aeeb07d8313aa536889607bb60963f5155
age: 90 s
636
PCR0: 0c1ee421212bb99e88c0943b461b9d5f20de1648a931f8b6d02f1ed3a81755b424de195e502ff5289e793adb4d93f702
aws: [ERROR]: An error occurred (AccessDeniedException) when calling the Decrypt operation: is not authorized to perform: kms:Decrypt on resource: arn:aws:kms:u
PCR0: 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
aws: [ERROR]: An error occurred (AccessDeniedException) when calling the Decrypt operation: is not authorized to perform: kms:Decrypt on resource: arn:aws:kms:u
aws: [ERROR]: An error occurred (ValidationException) when calling the Decrypt operation: AWS KMS cannot parse the attestation document.
```

| Attempt | Result |
|---|---|
| A. Replay of a 90-second-old document | **Accepted**, but sealed to a public key whose private half died with that sandbox |
| B. One extra boot argument (`quiet`) | New PCR0, `AccessDeniedException` |
| C. `"debug_mode": true` | PCR0 (and PCR1, PCR2) all zeros, `AccessDeniedException` |
| D. One byte flipped | `ValidationException: AWS KMS cannot parse the attestation document.` |

A is the design working: KMS doesn't need to reject the replayed document, because only the sandbox that made the key pair can open the answer. C is the trap: debug mode gives you a serial console and also zeroes the measurements, so a debug sandbox never passes a policy pinned to real ones. The recorded run only tried a 90-second-old document; older ones weren't tested.

## Step 14: How many sandboxes fit

Small guests: the CI kernel, the rootfs shared read-only, no network, 256 MiB. `slots` prints the driver's budgets; `launch` starts sandboxes from one config until one fails.

```bash
cd /opt/sandbox
NE=/sys/class/misc/nitro_enclaves
slots() { echo "slots in_use=$(cat $NE/pcie_slots_in_use)/$(cat $NE/pcie_slots) (total $(cat $NE/pcie_slots_total)), cpu_pool in_use=$(cat $NE/cpu_pool/in_use)/$(cat $NE/cpu_pool/total), mem_pool free=$(( $(cat $NE/mem_pool/free) / 1048576 )) MiB"; }
launch() {  # launch <config.json> <count>
  for i in $(seq 1 $2); do
    rm -f /tmp/d$i.sock
    setsid nohup firecracker --api-sock /tmp/d$i.sock --config-file $1 > d$i.log 2>&1 < /dev/null &
    sleep 1.5
    pgrep -f "/tmp/d$i.sock" >/dev/null && echo -n " #$i" || { echo; echo "#$i failed: $(grep -oE 'Error: .*' d$i.log | tail -1 | cut -c1-200)"; break; }
  done
  echo; slots
}
cat > small.json <<'EOF'
{ "boot-source": { "kernel_image_path": "/opt/sandbox/vmlinux-6.18.56" },
  "machine-config": { "vcpu_count": 1, "mem_size_mib": 256 },
  "drives": [ { "drive_id": "rootfs", "path_on_host": "/opt/sandbox/ubuntu-24.04.ext4", "is_root_device": true, "is_read_only": true } ],
  "enclave": {} }
EOF
jq '.enclave = {"cpu_overcommit": true}' small.json > small-oc.json
jq '."machine-config".vcpu_count = 4' small-oc.json > small-oc4.json

slots
launch small.json 5;      pkill -x firecracker; sleep 2      # 1. dedicated cores, 1 vCPU each
launch small-oc.json 6;   pkill -x firecracker; sleep 2      # 2. cpu_overcommit, slot budget 4
echo 20 > $NE/pcie_slots; slots; free -m | sed -n 2p         # 3. raise the slot budget to the maximum
launch small-oc4.json 7;  pkill -x firecracker; sleep 2      # 4. cpu_overcommit, 4 vCPUs each
T0=$(date +%s.%N); launch small-oc.json 22                    # 5. 1 vCPU each, up to the ceiling
echo "wall time incl. 1.5 s settle per launch: $(echo "$(date +%s.%N) - $T0" | bc) s"; free -m | sed -n 2p
pkill -x firecracker; sleep 2
```

```text
slots in_use=0/4 (total 20), cpu_pool in_use=0/3, mem_pool free=12284 MiB
 #1 #2 #3
#4 failed: Error: RunWithApi(BuildFromJson(StartMicroVM(Enclave(EnclaveVm(CreateVm(Custom { kind: Other, error: "NE_ERR_NO_CPUS_AVAIL_IN_POOL (errno=272): no CPUs are available in the NE CPU pool" }))))))
slots in_use=3/4 (total 20), cpu_pool in_use=3/3, mem_pool free=11462 MiB
 #1 #2 #3 #4
#5 failed: Error: RunWithApi(BuildFromJson(StartMicroVM(Enclave(EnclaveVm(CreateVm(Os { code: 28, kind: StorageFull, message: "No space left on device" }))))))
slots in_use=4/4 (total 20), cpu_pool in_use=0/3, mem_pool free=11188 MiB
slots in_use=0/20 (total 20), cpu_pool in_use=0/3, mem_pool free=12028 MiB
Mem:           15671         448       14632           0         591       15013
 #1 #2 #3 #4 #5 #6 #7
slots in_use=7/20 (total 20), cpu_pool in_use=0/3, mem_pool free=10110 MiB
 #1 #2 #3 #4 #5 #6 #7 #8 #9 #10 #11 #12 #13 #14 #15 #16 #17 #18 #19 #20
#21 failed: Error: RunWithApi(BuildFromJson(StartMicroVM(Enclave(EnclaveVm(CreateVm(Custom { kind: Other, error: "NE_ERR_ENCLAVE_LIMIT (errno=553): the per-instance enclave quota has been reached" }))))))
slots in_use=20/20 (total 20), cpu_pool in_use=0/3, mem_pool free=6548 MiB
wall time incl. 1.5 s settle per launch: 31.673260411 s
Mem:           15671        5956        9124           0         591        9505
```

Raising `pcie_slots` from 4 to 20 cost the parent 256 MiB (two 128 MiB steps of 8 slots; `used` went from 179 to 448 MiB) until the next reboot. Each 256 MiB confidential sandbox took 274 MiB from the pool: 256 plus a 2 MiB control page and one 16 MiB DMB for its disk.

Are all those vCPUs real to the guests? Twenty 4-vCPU sandboxes with the debug console on, so their boot logs land in the console files:

```bash
jq '.enclave.debug_mode = true' small-oc4.json > small-oc4-debug.json
for i in $(seq 1 20); do
  rm -f /tmp/v$i.sock
  setsid nohup firecracker --api-sock /tmp/v$i.sock --config-file small-oc4-debug.json > v$i.log 2>&1 < /dev/null &
  sleep 2
done
sleep 8
echo "running: $(pgrep -xc firecracker), slots in use: $(cat $NE/pcie_slots_in_use)"
grep -h -m1 -oE 'smp: Brought up .*' v*.log | sort | uniq -c
cat /proc/loadavg
pkill -x firecracker
```

```text
running: 20, slots in use: 20
     20 smp: Brought up 1 node, 4 CPUs
0.08 0.08 0.02 1/260 6141
```

80 sandbox vCPUs on a 4-vCPU instance. The EC2 guide and the Firecracker docs say the total vCPUs across all sandboxes can't exceed 5 times the instance's vCPUs; here the binding limit was the number of sandboxes (20 = 5 × 4), not their vCPUs. The guests were idle, so this says nothing about performance. Treat it as observed behaviour on 2026-11-09, not a promise.

## Step 15: A sandbox, a container and the metadata service

The same probe on the parent, in a `docker run` container on the parent, and in a sandbox (confidential mode with `cpu_overcommit`, 1 vCPU, 512 MiB):

```bash
cd /opt/sandbox
P='echo "kernel: $(uname -rv)"; echo "boot_id: $(cat /proc/sys/kernel/random/boot_id)"; echo "uptime: $(cut -d" " -f1 /proc/uptime)"; echo "disks: $(ls /sys/block | tr "\n" " ")"; T=$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60"); echo "imds role: $(curl -s -m 2 -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/iam/security-credentials/)"'
echo "== parent"; bash -c "$P"
dnf install -y -q docker && systemctl start docker
echo "== container on the parent"; docker run --rm -q public.ecr.aws/amazonlinux/amazonlinux:2023 bash -c "$P"

cat > base.json <<'EOF'
{ "boot-source": { "kernel_image_path": "/opt/sandbox/vmlinux-6.18.56" },
  "machine-config": { "vcpu_count": 1, "mem_size_mib": 512 },
  "drives": [ { "drive_id": "rootfs", "path_on_host": "/opt/sandbox/ubuntu-24.04.ext4", "is_root_device": true, "is_read_only": false } ],
  "network-interfaces": [ { "iface_id": "eth0", "guest_mac": "06:00:AC:10:00:02", "host_dev_name": "tap0" } ],
  "enclave": { "cpu_overcommit": true } }
EOF
fc_boot base.json
$SSH 'ip route add default via 172.16.0.1 dev eth0 2>/dev/null; true'
echo "== Nitro Sandbox"; $SSH "$P"

# Block the metadata service for every sandbox, at the parent
nft insert rule firecracker filter iifname '"tap*"' ip daddr 169.254.169.254 counter drop
echo "== Nitro Sandbox, IMDS dropped at the parent"; $SSH "$P"
nft list chain firecracker filter | grep 169.254
fc_stop
```

```text
== parent
kernel: 6.18.56-125.164.amzn2023.aarch64 #2 SMP Mon Oct 19 19:40:01 UTC 2026
boot_id: fe76f760-5736-45d6-984d-ac024581e114
uptime: 624.80
disks: loop0 loop1 loop2 loop3 loop4 loop5 loop6 loop7 nvme0n1
imds role: nitro-sandbox-ec2
== container on the parent
kernel: 6.18.56-125.164.amzn2023.aarch64 #2 SMP Mon Oct 19 19:40:01 UTC 2026
boot_id: fe76f760-5736-45d6-984d-ac024581e114
uptime: 673.69
disks: loop0 loop1 loop2 loop3 loop4 loop5 loop6 loop7 nvme0n1
imds role: nitro-sandbox-ec2
== Nitro Sandbox
kernel: 6.18.56+ #1 SMP Thu Nov  5 13:12:30 UTC 2026
boot_id: 2d575a59-2b5b-4b8e-bb91-0a6ff0e9375a
uptime: 1.94
disks: loop0 loop1 loop2 loop3 loop4 loop5 loop6 loop7 vda
imds role: nitro-sandbox-ec2
== Nitro Sandbox, IMDS dropped at the parent
kernel: 6.18.56+ #1 SMP Thu Nov  5 13:12:30 UTC 2026
boot_id: 2d575a59-2b5b-4b8e-bb91-0a6ff0e9375a
uptime: 2.09
disks: loop0 loop1 loop2 loop3 loop4 loop5 loop6 loop7 vda
imds role:
		iifname "tap*" ip daddr 169.254.169.254 counter packets 4 bytes 240 drop
```

The container is the parent with a different filesystem view: same kernel, same `boot_id`, same uptime. The sandbox has its own kernel. But through the parent's NAT it reached the instance metadata service and read the parent's role name, so it could fetch the parent's credentials too: the AL2023 AMI's hop limit allows the extra hop. ECS and EKS put a metadata surrogate inside the sandbox; on EC2 that's your job. Keep the drop rule (or set the instance's hop limit to 1) before you run anything untrusted.

## Gotchas

- **Reserve the memory pool** (`nitro_enclaves.mempool=` with `grubby`, then reboot) or anything bigger than the 64 MiB CMA area fails with `CMA allocation failed ... Out of memory (os error 12)`.
- **The uplink is `ens34`**, not the `eth0` the Firecracker docs assume. Look it up with `ip -j route list default`.
- **Install `nftables` and `squashfs-tools`**: the AMI has neither `nft` nor `unsquashfs`.
- **The TAP device, the nftables rules and a raised `pcie_slots` don't survive a reboot.** Reserve the pool first.
- **An AL2023 guest needs a dracut initramfs** with `virtio_pci virtio_blk virtio_net ext4` and `nsm`; the CI kernel has no NSM driver at all.
- **`--recipient AttestationDocument=` takes inline base64** (`$(base64 -w0 att.doc)`), not `fileb://`.
- **`debug_mode: true` zeroes PCR0 to PCR2** in confidential mode, so a debug sandbox never matches a pinned key policy.
- **Block the guest's IMDS** with the `nft` drop rule from Step 15; a NATed sandbox otherwise reaches the parent's role credentials.
- **Keep guest secrets off the rootfs.** It is a file on the parent's disk; use `/dev/shm` (or another tmpfs) for keys.
- **`gcore` skips the sandbox's memory mapping.** To check what the parent can see, read `/proc/<pid>/mem` over the `/dev/nitro_enclaves` mapping (Step 8).

## Cost

Nitro Sandboxes have no additional charge. The `m8g.xlarge` was $0.17952 an hour on demand in us-east-1, plus its 20 GiB gp3 volume and public IPv4 address; the KMS key is $1 a month while it exists (prorated). The recorded exploration ran the instance for about 21 minutes.

## Cleanup

On your machine, in `aws-nitro-sandboxes/ec2`:

```bash
aws ec2 terminate-instances --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
aws ec2 delete-security-group --group-id "$SG_ID"    # console launch: the launch-wizard-N group instead

# schedule-key-deletion takes the key ID, not the alias
KEY_ID=$(aws kms describe-key --key-id alias/nitro-sandbox-ec2 --query KeyMetadata.KeyId --output text)
aws kms delete-alias --alias-name alias/nitro-sandbox-ec2
aws kms schedule-key-deletion --key-id "$KEY_ID" --pending-window-in-days 7

aws iam remove-role-from-instance-profile --instance-profile-name nitro-sandbox-ec2 --role-name nitro-sandbox-ec2
aws iam delete-instance-profile --instance-profile-name nitro-sandbox-ec2
aws iam detach-role-policy --role-name nitro-sandbox-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name nitro-sandbox-ec2
rm -rf build
```
