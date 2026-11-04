"""Test 9: the egress and identity baseline on INTERNET_EGRESS. Egress IPs, raw-IP and UDP traffic,
DNS, and what code inside the VM can do with the execution role (IMDSv2). Sets up part 2.
Never prints secret values: only the credential type, the key prefix and the role name."""

from common import banner, run

PROBES = {
    "egress IP": "curl -s -m 8 https://checkip.amazonaws.com || echo FAIL",
    "raw-IP HTTPS (1.1.1.1)": "curl -s -m 8 -o /dev/null -w '%{http_code}' https://1.1.1.1 || echo ' FAIL'",
    "DNS via the platform resolver": "getent hosts example.com | head -1 || echo FAIL",
    "DNS straight to 1.1.1.1:53 (UDP)": "dig +time=3 +tries=1 +short @1.1.1.1 example.com || echo FAIL",
    "DNS over TCP to 1.1.1.1:53": "dig +tcp +time=3 +tries=1 +short @1.1.1.1 example.com || echo FAIL",
    "IMDSv1 GET (no token)": "curl -s -m 3 -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/meta-data/ || echo ' FAIL'",
    "IMDSv2 token PUT": "curl -s -m 3 -o /dev/null -w '%{http_code}' -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'",
    "role name from IMDS": ("T=$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); "
                            "curl -s -m 3 -H \"X-aws-ec2-metadata-token: $T\" http://169.254.169.254/latest/meta-data/iam/security-credentials/"),
    "credential env vars": "env | grep -E '^AWS_(ACCESS|SECRET|SESSION|CONTAINER)' | cut -d= -f1 || echo none",
    "boto3 credentials": ("python -c \"import boto3; c=boto3.Session().get_credentials(); "
                          "print('none' if c is None else f'method={c.method} key={c.access_key[:4]}... token={bool(c.token)}')\""),
    "sts get-caller-identity": ("python -c \"import boto3; a=boto3.client('sts').get_caller_identity()['Arn']; "
                                "print(a.split(':',5)[-1])\" 2>&1 | tail -1"),
    "s3 list-buckets (role allows it)": ("python -c \"import boto3; print(len(boto3.client('s3').list_buckets()['Buckets']), 'buckets')\" "
                                         "2>&1 | tail -1"),
}
NON_ROOT = ("id -u nobody >/dev/null && su -s /bin/bash nobody -c \"T=\\$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token "
            "-H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); curl -s -m 3 -H \\\"X-aws-ec2-metadata-token: \\$T\\\" "
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/; echo ' (as nobody)'\"")
BLACKHOLE = ("ip route add blackhole 169.254.169.254/32 2>&1 && echo route-added; "
             "curl -s -m 3 -o /dev/null -w 'imds after blackhole: %{http_code}\\n' -X PUT http://169.254.169.254/latest/api/token "
             "-H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || echo 'imds after blackhole: unreachable'; "
             "ip route del blackhole 169.254.169.254/32 2>&1 && echo 'route removed again (any root process can do this)'")

for label, role in (("execution role (logs + s3:ListAllMyBuckets)", "exec_probe"), ("no execution role", None)):
    banner(label)
    vm = run("sandbox", "09-identity", role=role)
    vm.wait("RUNNING")
    vm.ready()
    for k, cmd in PROBES.items():
        out = vm.sh(cmd, timeout=40)
        print(f"{k:34} {out.splitlines()[0] if out else ''}")
    if role:
        print(f"{'IMDS as a non-root user':34} {vm.sh(NON_ROOT)}")
        print(vm.sh(BLACKHOLE))
    vm.terminate()

banner("Egress IPs across five MicroVMs")
ips = []
for i in range(5):
    vm = run("sandbox", "09-ip", role=None)
    vm.wait("RUNNING")
    vm.ready()
    ips.append(vm.sh("curl -s -m 8 https://checkip.amazonaws.com"))
    vm.terminate()
print(ips, f"-> {len(set(ips))} distinct")
