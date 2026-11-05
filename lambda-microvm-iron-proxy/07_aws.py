"""Test 6: AWS without AWS credentials. The MicroVM has placeholder keys and no proxy settings: AWS endpoints
resolve to the NLB like every other name, the AWS CLI signs with the placeholders, and iron-proxy's aws_auth
strips that signature and re-signs with the proxy's Fargate task role (read-only: STS, S3 listing and the demo
bucket, ECS listing). Bucket and cluster names are anonymized in the output."""

import json
import time

from common import anon, audit_line, banner, check, is_request, proxy_audit, redact, run, start_log, summary, tf

start_log("07_aws")
t = tf()
since = int(time.time() * 1000)
vm = run("07-aws")


def aws(cmd: str) -> str:
    return vm.sh(f"aws {cmd} --output json 2>&1", timeout=60)


banner("What the MicroVM has")
print(vm.sh("env | grep -E '^AWS_' | sort; echo HTTPS_PROXY=${HTTPS_PROXY:-unset}; getent hosts sts.us-east-1.amazonaws.com | head -1"))

banner("sts get-caller-identity")
out = aws("sts get-caller-identity")
try:
    arn = json.loads(out)["Arn"]
    print("Arn:", redact(arn))
    check("re-signed as the proxy's task role", ":assumed-role/my2cents-ip-task/" in arn, redact(arn))
except (ValueError, KeyError):
    print(out[:300])
    check("re-signed as the proxy's task role", False, out[:120])

banner("s3 ls (every bucket the task role can see, names anonymized)")
out = aws("s3api list-buckets --query 'Buckets[].Name'")
try:
    names = json.loads(out)
    print(f"{len(names)} buckets:", [anon(n) for n in names][:12], "..." if len(names) > 12 else "")
    check("s3 list-buckets works", len(names) > 0, f"{len(names)} buckets")
except ValueError:
    print(out[:300])
    check("s3 list-buckets works", False, out[:120])

banner("Objects in the demo bucket")
out = vm.sh(f"aws s3 ls s3://{t['demo_bucket']}/ --recursive 2>&1")
print(out)
check("s3 ls of the demo bucket lists the 4 objects", sum(l.strip().endswith(".txt") for l in out.splitlines()) == 4, out[-120:])
out = vm.sh(f"aws s3 cp s3://{t['demo_bucket']}/reports/q3-summary.txt - 2>&1")
print(out)
check("s3 cp of an object works", "demo object reports/q3-summary.txt" in out, out)
print("key with a space (iron-proxy issue #220):",
      vm.sh(f"aws s3 cp 's3://{t['demo_bucket']}/reports/q3 summary.txt' - 2>&1 | tail -1"))

banner("ecs list-clusters (anonymized)")
out = aws("ecs list-clusters")
try:
    clusters = [anon(a.split("/")[-1], "cluster") for a in json.loads(out)["clusterArns"]]
    print(clusters)
    check("ecs list-clusters works", len(clusters) > 0, f"{len(clusters)} clusters")
except (ValueError, KeyError):
    print(out[:300])
    check("ecs list-clusters works", False, out[:120])

banner("Outside the policy")
out = aws("iam list-users")
print("iam list-users (service not in allowed_services):", out[-160:].replace("\n", " "))
check("iam refused by aws_auth", "service_not_allowed" in out or "403" in out, out[-120:].replace("\n", " "))
out = aws("ec2 describe-vpcs --region us-west-2")
print("ec2 in us-west-2 (region not allowed):", out[-160:].replace("\n", " "))
check("us-west-2 refused by aws_auth", "region_not_allowed" in out or "403" in out, out[-120:].replace("\n", " "))
out = vm.sh(f"echo x | aws s3 cp - s3://{t['demo_bucket']}/written-from-sandbox.txt 2>&1 | tail -1")
print("s3 put-object (the task role has no write):", out)
check("upload refused", "upload:" not in out, out)

banner("What aws_auth checks (and doesn't)")
out = vm.sh("AWS_ACCESS_KEY_ID=AKIAANYTHINGATALL000 AWS_SECRET_ACCESS_KEY=whatever aws sts get-caller-identity "
            "--query Arn --output text 2>&1 | tail -1")
print("random fake keys, still re-signed:", out)
check("any keys are re-signed (possession isn't checked)", "my2cents-ip-task" in out, out)
print("no keys at all:", vm.sh("env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY aws sts get-caller-identity 2>&1 | tail -1"))
print("unsigned curl to STS:", vm.sh("curl -s 'https://sts.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15' | head -c 160"))
print("presigned URL (signed with the placeholder):",
      vm.sh(f"curl -s -o /dev/null -w '%{{http_code}}' \"$(aws s3 presign s3://{t['demo_bucket']}/notes/readme.txt)\""))
print("boto3 (python3-boto3 from Debian):",
      vm.sh("python3 -c \"import boto3; print(boto3.__version__, boto3.client('sts').get_caller_identity()['Arn'].split(':')[5])\" 2>&1 | tail -1"))

banner("iron-proxy audit (amazonaws)")
recs = [a for a in proxy_audit(since, "amazonaws") if is_request(a)]
for a in recs:
    print(audit_line(a))
check("iam rejected by aws_auth in the audit log",
      any(a["audit"].get("host", "").startswith("iam.") and a.get("rejected_by") == "aws_auth" for a in recs))
vm.terminate()
summary()
