"""Test 7: AWS with no AWS credentials. The proxy re-signs SigV4 requests with an IAM role that trusts
LangSmith's OIDC issuer (see the README for the role, its policy and the test bucket).

Organizations on restricted egress (the non-Enterprise default) can't reach AWS: it isn't on the managed
allow list, and that list replaces any access_control you pass. The script stops at the reachability
check in that case; LangChain support can grant an exemption. After one, nothing limits where a sandbox
connects unless you set access_control yourself, so this sandbox may reach AWS hosts and nothing else.
"""

import os
import re

from langsmith.sandbox import aws_auth, mount_config, s3_mount

from common import banner, box, claude, client, create_sandbox, require_env, run

require_env("AWS_ROLE_ARN", "AWS_LS_BUCKET")
ROLE, BUCKET = os.environ["AWS_ROLE_ARN"], os.environ["AWS_LS_BUCKET"]
MODEL = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
AWS_RULE = aws_auth(
    role_arn=ROLE,
    env_vars={"AWS_REGION": "us-east-1", "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_MODEL": MODEL,
              "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
              "ANTHROPIC_DEFAULT_HAIKU_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
)
EGRESS = {"allow_list": ["*.amazonaws.com"]}  # this sandbox's own egress policy: AWS only
PY = "python3 -c \"import boto3; {}\" 2>&1 | tail -1"
CODE = "curl -s -m 8 -o /dev/null -w '%{http_code}'"


def show(out: str) -> str:
    """Hide the account ID, and cut an AWS error down to its code and reason."""
    out = re.sub(r"\d{12}", "<account>", out).strip()
    m = re.search(r"\((\w+)\) when calling .*(no identity-based policy allows|explicit deny in a session policy)", out)
    if m:
        return f"{m.group(1)} ({m.group(2)})"
    return re.sub(r"^b'(.*)'$", r"\1", out)


sbx = create_sandbox("08", rules=[AWS_RULE], access_control=EGRESS)
try:
    banner("What the sandbox sees")
    print(run(sbx, "env | grep -E '^AWS_' | sort"))
    stored = box(sbx.name)["proxy_config"]
    print("stored rule          :", show(str(stored["rules"][0]["aws"])))
    print("stored access_control:", stored.get("access_control"))

    banner("Egress: AWS only, by the sandbox's own allow list")
    reach = run(sbx, f"{CODE} https://sts.us-east-1.amazonaws.com/ || echo blocked")
    print("sts.us-east-1.amazonaws.com ->", reach)
    if "blocked" in reach or reach == "000":
        raise SystemExit("AWS hosts are blocked by the org's egress policy; ask LangChain support for an exemption.")
    print("example.com                 ->", run(sbx, f"{CODE} https://example.com || echo ' blocked'"))

    banner("Identity: the role, assumed by the proxy")
    print(show(run(sbx, PY.format("print(boto3.client('sts', region_name='us-east-1').get_caller_identity()['Arn'])"))))

    banner("S3 inside and outside the role's prefix")
    print("get sandbox/hello.txt :", show(run(sbx, PY.format(f"print(boto3.client('s3').get_object(Bucket='{BUCKET}', Key='sandbox/hello.txt')['Body'].read().decode().strip())"))))
    print("put sandbox/from-ls   :", show(run(sbx, PY.format(f"boto3.client('s3').put_object(Bucket='{BUCKET}', Key='sandbox/from-ls.txt', Body=b'hi'); print('ok')"))))
    print("get private/secret.txt:", show(run(sbx, PY.format(f"print(boto3.client('s3').get_object(Bucket='{BUCKET}', Key='private/secret.txt')['Body'].read())"))))
    print("list all buckets      :", show(run(sbx, PY.format("print(len(boto3.client('s3').list_buckets()['Buckets']))"))))

    banner("Claude Code on Bedrock")
    r = claude(sbx, "Reply with exactly: ok", timeout=180)
    print(f"{r.get('result')!r} error={r.get('is_error')} cost=${r.get('total_cost_usd', 0):.4f}")
finally:
    client.delete_sandbox(sbx.name)

# The same role behind a read-only S3 mount. Mount auth is scoped to the mount: LangSmith adds a session
# policy, and the proxy signs only S3 requests with it.
mnt = create_sandbox("08m", mount_config=mount_config(
    auth=[aws_auth(role_arn=ROLE)],
    mounts=[s3_mount(id="data", mount_path="/mnt/mounts/data", bucket=BUCKET, prefix="sandbox/", read_only=True)],
))
try:
    banner("The bucket as a read-only mount")
    print(run(mnt, "ls /mnt/mounts/data && cat /mnt/mounts/data/hello.txt"))
    print("write a file:", run(mnt, "sh -c 'echo hi > /mnt/mounts/data/x.txt' 2>&1 | sed 's/.*: //'"))

    banner("The mount's role, called directly")
    print("s3 get sandbox/hello.txt:", show(run(mnt, PY.format(f"print(boto3.client('s3').get_object(Bucket='{BUCKET}', Key='sandbox/hello.txt')['Body'].read().decode().strip())"))))
    print("s3 put sandbox/x.txt    :", show(run(mnt, PY.format(f"boto3.client('s3').put_object(Bucket='{BUCKET}', Key='sandbox/x.txt', Body=b'hi'); print('ok')"))))
    print("sts get-caller-identity :", show(run(mnt, PY.format("print(boto3.client('sts', region_name='us-east-1').get_caller_identity()['Arn'])"))))
finally:
    client.delete_sandbox(mnt.name)
