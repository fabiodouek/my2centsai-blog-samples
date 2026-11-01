#!/usr/bin/env python3
"""Test 3 (Bedrock): Converse and ConverseStream through the supervisor, signed as the `bedrock`
service with the sandbox-role session, plus a model-ARN case that the signer's path handling
refuses.

This runs on the host but does no AWS or boto3 work itself: it creates a sandbox with the p5-aws
provider and the signing policy, then runs a small boto3 program *inside* the sandbox (the sandbox
has boto3 1.43.x). ConverseStream's time-to-first-token is measured inside the sandbox, where the
stream actually arrives. No credential is ever printed; the account id is masked.

Run after ./01_setup.sh:  python3 04_bedrock.py   (or: uv run python 04_bedrock.py)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = "osh-eks"
SANDBOX = "p5-bedrock"
MODEL_ID = "amazon.nova-micro-v1:0"


def load_env(path: str) -> dict[str, str]:
    """Pull `export K='V'` lines out of a shell env file without executing it."""
    env: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
                if match:
                    env[match.group(1)] = match.group(2).strip().strip("'\"")
    except FileNotFoundError:
        pass
    return env


def osh(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["openshell", "-g", GATEWAY, *args],
        check=check,
        text=True,
        capture_output=capture,
    )


def render_policy(bucket: str, region: str) -> str:
    with open(os.path.join(HERE, "policies", "aws-signing.yaml"), encoding="utf-8") as handle:
        body = handle.read()
    return body.replace("${BUCKET}", bucket).replace("${REGION}", region)


# The program that runs inside the sandbox. Keep it dependency-light (boto3 + stdlib).
INNER = r'''
import json, os, sys, time
import boto3
from botocore.config import Config

region = os.environ["AWS_REGION"]
model = os.environ["MODEL_ID"]
profile_arn = os.environ["PROFILE_ARN"]
brt = boto3.client("bedrock-runtime", config=Config(region_name=region, retries={"max_attempts": 0}))
msg = [{"role": "user", "content": [{"text": "Reply with the single word: pong"}]}]
cfg = {"maxTokens": 12, "temperature": 0}

def line(label, **kv):
    print(label + " " + json.dumps(kv))

# 1. Converse (body-signed).
try:
    r = brt.converse(modelId=model, messages=msg, inferenceConfig=cfg)
    line("converse", ok=True, text=r["output"]["message"]["content"][0]["text"],
         stop=r.get("stopReason"), inTok=r["usage"]["inputTokens"], outTok=r["usage"]["outputTokens"])
except Exception as e:
    line("converse", ok=False, error=type(e).__name__, detail=str(e)[:200])

# 2. ConverseStream: time to first token, measured here where the chunks arrive.
try:
    t0 = time.monotonic()
    r = brt.converse_stream(modelId=model, messages=msg, inferenceConfig=cfg)
    ttft_ms = None
    chunks = 0
    for ev in r["stream"]:
        if "contentBlockDelta" in ev and ttft_ms is None:
            ttft_ms = round((time.monotonic() - t0) * 1000)
        chunks += 1
    line("converse_stream", ok=True, ttft_ms=ttft_ms, events=chunks)
except Exception as e:
    line("converse_stream", ok=False, error=type(e).__name__, detail=str(e)[:200])

# 3. A model given as an inference-profile ARN: the request path then contains encoded slashes,
#    which the supervisor's canonicalizer rejects (%2F). Record what the client sees.
try:
    r = brt.converse(modelId=profile_arn, messages=msg, inferenceConfig=cfg)
    line("converse_arn", ok=True, note="unexpected: ARN modelId was accepted",
         text=r["output"]["message"]["content"][0]["text"])
except Exception as e:
    line("converse_arn", ok=False, error=type(e).__name__, detail=str(e)[:200])
'''


def main() -> int:
    env = {**load_env(os.path.join(HERE, "..", "openshell-eks", "env.sh")),
           **load_env(os.path.join(HERE, "env.sh"))}
    region = env.get("AWS_REGION", "us-east-1")
    bucket = env.get("BUCKET")
    account = env.get("GATEWAY_ROLE_ARN", "")
    account_id = account.split(":")[4] if account.count(":") >= 4 else ""
    if not bucket:
        print("run ./01_setup.sh first (env.sh has no BUCKET)", file=sys.stderr)
        return 1
    profile_arn = f"arn:aws:bedrock:{region}:{account_id}:inference-profile/us.{MODEL_ID}"

    def mask(text: str) -> str:
        return text.replace(account_id, "<account>") if account_id else text

    osh("sandbox", "delete", SANDBOX, check=False)
    policy = render_policy(bucket, region)
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as pf:
            pf.write(policy)
            policy_path = pf.name
        try:
            print(f"\n$ openshell -g {GATEWAY} sandbox create --name {SANDBOX} --provider p5-aws --policy <signing>")
            osh("sandbox", "create", "--name", SANDBOX, "--detach", "--no-tty",
                "--provider", "p5-aws", "--policy", policy_path,
                "--env", f"AWS_REGION={region}", "--env", f"AWS_DEFAULT_REGION={region}",
                "--env", f"MODEL_ID={MODEL_ID}", "--env", f"PROFILE_ARN={profile_arn}")
        finally:
            os.unlink(policy_path)

        # Wait until the sandbox can run a command.
        for _ in range(45):
            if osh("sandbox", "exec", SANDBOX, "--no-login-shell", "--no-tty", "--", "true",
                   check=False).returncode == 0:
                break
            subprocess.run(["sleep", "2"], check=False)
        else:
            print("sandbox not usable", file=sys.stderr)
            return 1

        print(f"\n$ (inside {SANDBOX}) python3.12 bedrock converse / converse_stream / arn")
        res = osh("sandbox", "exec", SANDBOX, "--no-login-shell", "--no-tty", "--",
                  "/usr/bin/python3.12", "-c", INNER, check=False)
        out = mask(re.sub(r"\x1b\[[0-9;]*m", "", res.stdout + res.stderr))
        for raw in out.splitlines():
            print("  " + raw)
        return 0 if res.returncode == 0 else 1
    finally:
        osh("sandbox", "delete", SANDBOX, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
