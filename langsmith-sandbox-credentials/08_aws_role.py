"""Test 7: AWS with no AWS credentials. The proxy re-signs SigV4 requests with an IAM role that trusts
LangSmith's OIDC issuer (see the README for the role and trust policy).

On an org with restricted egress, AWS hosts are not on the managed allow list, so every call below
times out until LangChain support grants an exemption. The script reports which case you're in.
"""

import os
import re

from langsmith.sandbox import aws_auth

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
PY = "python3 -c \"import boto3; {}\" 2>&1 | tail -1"

sbx = create_sandbox("08", rules=[AWS_RULE])
try:
    banner("What the sandbox sees")
    print(run(sbx, "env | grep -E '^AWS_' | sort"))
    stored = box(sbx.name)["proxy_config"]["rules"][0]["aws"]
    print("stored rule:", {k: re.sub(r"::\d{12}:", "::<account>:", v) for k, v in stored.items()})

    banner("Can the sandbox reach AWS at all?")
    reach = run(sbx, "curl -s -m 8 -o /dev/null -w '%{http_code}' https://sts.us-east-1.amazonaws.com/ || echo blocked")
    print("sts.us-east-1.amazonaws.com ->", reach)
    if "blocked" in reach or reach == "000":
        raise SystemExit("AWS hosts are blocked by the org's egress policy; ask LangChain support for an exemption.")

    banner("Identity: the role, assumed by the proxy")
    print(run(sbx, PY.format("print(boto3.client('sts', region_name='us-east-1').get_caller_identity()['Arn'])")))

    banner("S3 inside and outside the role's prefix")
    print("get sandbox/hello.txt :", run(sbx, PY.format(f"print(boto3.client('s3').get_object(Bucket='{BUCKET}', Key='sandbox/hello.txt')['Body'].read())")))
    print("put sandbox/from-ls   :", run(sbx, PY.format(f"boto3.client('s3').put_object(Bucket='{BUCKET}', Key='sandbox/from-ls.txt', Body=b'hi'); print('ok')")))
    print("get private/secret.txt:", run(sbx, PY.format(f"print(boto3.client('s3').get_object(Bucket='{BUCKET}', Key='private/secret.txt')['Body'].read())")))
    print("list all buckets      :", run(sbx, PY.format("print(len(boto3.client('s3').list_buckets()['Buckets']))")))

    banner("Claude Code on Bedrock")
    r = claude(sbx, "Reply with exactly: ok", timeout=180)
    print(f"{r.get('result')!r} error={r.get('is_error')} cost=${r.get('total_cost_usd', 0):.4f}")
finally:
    client.delete_sandbox(sbx.name)
