# Readable copy of the command embedded in td-isolated.json and td-shared.json
# (containerDefinitions[0].command[2]). The task definitions are the source of truth.

echo "kernel: $(uname -rv)"; echo "boot_id: $(cat /proc/sys/kernel/random/boot_id)"; echo "uptime: $(cut -d' ' -f1 /proc/uptime)"; echo "nproc: $(nproc)"; grep MemTotal /proc/meminfo; echo "disks: $(ls /sys/block | tr '\n' ' ')"; df -h / | tail -1; echo "pci: $(for d in /sys/bus/pci/devices/*; do printf '%s ' $(cat $d/vendor):$(cat $d/device); done)"; ls -la /dev/nsm 2>&1; echo "netdevs: $(tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' | tr '\n' ' ')"
T=$(curl -s -m2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); echo "imdsv2 token: ${#T} chars"; echo "imdsv1 instance-id: HTTP $(curl -s -m2 -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/meta-data/instance-id)"
for p in placement/region placement/availability-zone instance-id instance-type iam/security-credentials/ local-ipv4; do echo "imdsv2 $p: $(curl -s -m2 -w ' [HTTP %{http_code}]' -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/$p | tr '\n' ' ' | head -c 140)"; done
echo "task role creds: $(curl -s -m2 169.254.170.2$AWS_CONTAINER_CREDENTIALS_RELATIVE_URI | grep -o '"RoleArn":"[^"]*"')"; echo "task metadata AZ: $(curl -s -m2 $ECS_CONTAINER_METADATA_URI_V4/task | grep -o '"AvailabilityZone":"[^"]*"')"
echo "caps: $(grep CapEff /proc/self/status)"; echo "pid1: $(cat /proc/1/comm)"; echo "procs visible: $(ls /proc | grep -c '^[0-9]')"; echo "egress: $(curl -s -m4 -o /dev/null -w 'HTTP %{http_code}' https://aws.amazon.com)"
echo PROBE_DONE; sleep 3600

