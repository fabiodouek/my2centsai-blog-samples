"""Bedrock with a short-term API key: does the vault replace the placeholder Authorization header?

  uv run python 01_bedrock_bearer.py

Bedrock accepts either a SigV4 signature or a bearer API key. This script asks the broker for a sandbox
with `bedrock_bearer=true`: the broker mints a short-term Bedrock key (at most one hour) from the sandbox's
assumed session, and binds it to bedrock-runtime.us-east-1.amazonaws.com with a plain `bearer` binding. The
sandbox has the placeholder AWS_BEARER_TOKEN_BEDROCK only.

What it finds out:
  - the sandbox's boto3 and AWS CLI send `Authorization: Bearer <placeholder>`; if the vault replaces it
    with the real key, the call works and the addon never re-signs it (no `re-signed ... bedrock-runtime`
    line in the sidecar log);
  - if the vault does NOT replace it, Bedrock rejects the placeholder and the call fails: then Bedrock goes
    through the SigV4 path (04_nons3_body.py) and the script says so.
The script reports which one happened. Measured on the cluster: the bearer path works (HTTP 200 with a placeholder token, so the vault
replaced it; the vault logs `applied binding=bedrock-bearer`; the addon logs nothing for the Bedrock call). It exits 1 if the call does not work that
way, or if the placeholder is missing from the sandbox.

Needs ./01_iam.sh, ./02_broker.sh and ./00_image.sh to have run.
"""


from common import (
    MODEL_ID, Checks, cleanup, ensure_broker_forward, ensure_port_forward, heading, log_mark, log_since, new_sandbox,
    note, python_in, run, aws_env, sh, show_log, sigv4_events,
)

SCRIPT = "01_bedrock_bearer"
BOTO3 = """
import boto3
from botocore.exceptions import ClientError
client = boto3.client("bedrock-runtime")
try:
    reply = client.converse(modelId="%s", messages=[{"role": "user", "content": [{"text": "Reply with exactly one word: ready"}]}],
                            inferenceConfig={"maxTokens": 20})
    print("HTTP", reply["ResponseMetadata"]["HTTPStatusCode"], "| reply:", reply["output"]["message"]["content"][0]["text"].strip()[:40])
except ClientError as error:
    print("HTTP", error.response["ResponseMetadata"]["HTTPStatusCode"], "| error code:", error.response["Error"]["Code"])
except Exception as error:
    print("no AWS answer |", type(error).__name__)
""" % MODEL_ID

ensure_port_forward()
ensure_broker_forward()
checks = Checks()
try:
    heading("1. a sandbox whose Bedrock key is a bearer binding")
    sb, view = new_sandbox(bedrock_bearer=True, script=SCRIPT)
    sandbox_id = view["sandbox_id"]
    env = aws_env(sb)
    run(sb, "env | grep -E '^AWS_(BEARER|ACCESS|SECRET)' | sort")
    checks.check("the sandbox holds a placeholder bearer token", env.get("AWS_BEARER_TOKEN_BEDROCK") == "bedrock-api-key-osb-placeholder")
    checks.check("the bearer value is not a real key", len(env.get("AWS_BEARER_TOKEN_BEDROCK", "")) < 64)

    heading("2. Converse over the bearer path (boto3 reads AWS_BEARER_TOKEN_BEDROCK)")
    mark = log_mark(sandbox_id)
    out = python_in(sb, BOTO3, label="python3 (boto3 bedrock-runtime converse, pinned Haiku model)")
    worked = out.rc == 0 and out.text.strip().startswith("HTTP 200")
    cli = sh(sb, f"aws bedrock-runtime converse --model-id {MODEL_ID} --messages "
                 "'[{\"role\":\"user\",\"content\":[{\"text\":\"Reply with exactly one word: ready\"}]}]' "
                 "--inference-config '{\"maxTokens\":20}' --query output.message.content[0].text --output text", timeout=120)
    print(f"  $ aws bedrock-runtime converse --model-id {MODEL_ID} --messages ...")
    print(f"      exit code {cli.rc}")

    heading("3. what the sidecar did")
    # The sidecar's log only reaches the container log when something flushes it, and the addon (which flushes
    # every line it writes) is silent on a bearer call. One small SigV4 call makes it log, and so flushes the
    # vault's line for the Bedrock calls that came before it.
    sh(sb, "aws sts get-caller-identity --query Arn --output text >/dev/null")
    print("  $ aws sts get-caller-identity   # a SigV4 call: the addon logs it, which flushes the sidecar's log")
    # positive control: the addon's own line for the STS call. If it never arrives, the log did not flush and the
    # absence of the vault's line would mean nothing.
    control = log_since(sandbox_id, mark, "re-signed POST sts.us-east-1.amazonaws.com", wait=30, minimum=1)
    flushed = bool(control)
    print(f"  control: the addon's `re-signed` line for the STS call reached the log: {flushed}")
    checks.check("the sidecar log flushed (the STS call's re-signed line is in it)", flushed)
    all_lines = log_since(sandbox_id, mark, "credential proxy", wait=10, minimum=0)
    applied = [line for line in all_lines if "applied binding=bedrock-bearer" in line]
    lines = sigv4_events(all_lines)
    bedrock_resigned = [line for line in lines if "re-signed" in line and "bedrock-runtime" in line]
    refused = [line for line in lines if "bearer-on-sigv4-binding" in line]
    show_log(lines, limit=4)
    print(f"  vault: `applied binding=bedrock-bearer` lines: {len(applied)}")
    show_log(applied, limit=1)
    print(f"  addon lines in this window (the STS control call included): {len(lines)}; re-signed: {len(bedrock_resigned)}; refused bearer-on-sigv4-binding: {len(refused)}")

    worked = worked and cli.rc == 0  # boto3 and the AWS CLI both succeeded
    heading("4. the result")
    if not flushed:
        print("  RESULT  the sidecar log did not flush: no conclusion about the vault's line can be drawn from this run.")
    elif worked and applied and not bedrock_resigned and not refused:
        print("  RESULT  the vault replaced the placeholder Authorization header: the call worked over the bearer path,")
        print("          and the addon did not re-sign it. A Bedrock short-term key needs no SigV4 re-signing.")
        checks.check("bearer path: the call succeeded with a placeholder token, the vault logged `applied binding=bedrock-bearer`, and the addon neither re-signed nor refused it", True)
    elif worked:
        print("  RESULT  the call worked, but not as expected: the vault's `applied binding=bedrock-bearer` line is missing, or the addon re-signed or refused the call.")
        checks.check("the call succeeded over the bearer path (vault binding applied, no re-signing)", False)
    else:
        print("  RESULT  the call over the bearer path did NOT work.")
        print("          Bedrock goes through the SigV4 path in this setup (04_nons3_body.py); see the sidecar lines above for why.")
        checks.check("the call over the bearer path worked (if it did not, the SigV4 path is the answer: see 04)", False)
        note(f"boto3 said: {out.text.strip()[:120]}; the AWS CLI exit code was {cli.rc}")
    note("the key the vault holds is a Bedrock short-term key the broker minted from the sandbox's assumed session; it expires in at most one hour")
    note("AWS_BEARER_TOKEN_BEDROCK also tells the SDK to skip SigV4 for Bedrock, so the sandbox sends a bearer header with no signature")
finally:
    cleanup()
checks.finish()
