# Readable copy of the probe embedded in pod-plain.yaml, pod-sandbox.yaml, pod-netiso.yaml,
# pod-hostnetwork.yaml and pod-hostpid.yaml (args[0], followed there by sleep infinity).

echo "kernel: $(uname -rv)"; echo "boot_id: $(cat /proc/sys/kernel/random/boot_id)"; echo "uptime: $(cut -d' ' -f1 /proc/uptime)"; echo "nproc: $(nproc)"; grep MemTotal /proc/meminfo; echo "disks: $(ls /sys/block | tr '\n' ' ')"; ls -la /dev/nsm 2>&1 | head -1
echo "pci: $(for d in /sys/bus/pci/devices/*; do printf '%s ' $(cat $d/vendor):$(cat $d/device); done | tr ' ' '\n' | sort | uniq -c | tr '\n' ' ')"
T=$(curl -s -m3 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); echo "imdsv2 token: ${#T} chars"
for p in placement/region placement/availability-zone instance-id iam/security-credentials/; do echo "imds $p: $(curl -s -m3 -w ' [HTTP %{http_code}]' -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/$p | tr '\n' ' ' | head -c 120)"; done
echo "sa token: $(ls /var/run/secrets/kubernetes.io/serviceaccount/token 2>&1)"
echo "procs: $(ls /proc | grep -c '^[0-9]')"
