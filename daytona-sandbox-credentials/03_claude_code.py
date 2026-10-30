"""Test 2: Claude Code with placeholder keys, against the Anthropic API and against Amazon Bedrock.

The sandbox only ever holds dtn_secret_... values. Claude Code sends them as x-api-key (Anthropic) or
Authorization: Bearer (Bedrock API key); the proxy swaps in the real value for the bound hosts.
"""

import time

from common import ANTHROPIC_ENV, ANTHROPIC_MODEL, BEDROCK_ENV, banner, bedrock_env, claude, create_sandbox, run

PROMPT = "Reply with exactly: ok"


def attempt(sbx, label: str, flags: str = "", timeout: int = 180) -> None:
    t0 = time.perf_counter()
    try:
        r = claude(sbx, PROMPT, timeout=timeout, flags=flags)
        print(f"{label:34} {r.get('result', '')!r:10} error={r.get('is_error')} "
              f"cost=${r.get('total_cost_usd', 0):.4f} {time.perf_counter() - t0:.1f}s")
    except Exception as e:
        print(f"{label:34} FAILED: {str(e)[-300:]}")


banner("A. Anthropic API, key not attached (plain placeholder-looking string)")
sbx = create_sandbox("03a", env_vars={**ANTHROPIC_MODEL, "ANTHROPIC_API_KEY": "dtn_secret_notarealsecret0000"})
try:
    print(run(sbx, "claude --version"))
    # With a bad key Claude Code keeps retrying, so cap it and show what it printed.
    out = run(sbx, f"timeout 45 claude -p '{PROMPT}' --output-format json < /dev/null; echo exit=$?", timeout=90)
    print("claude, no secret attached:", " ".join(out.split())[-300:] or "(no output)")
    print("same key with curl:", run(sbx, "curl -s -w ' [%{http_code}]' https://api.anthropic.com/v1/models"
                                          " -H \"x-api-key: $ANTHROPIC_API_KEY\" -H 'anthropic-version: 2023-06-01'"))
finally:
    sbx.delete()

banner("B. Anthropic API, ANTHROPIC_API_KEY bound to a Daytona Secret")
sbx = create_sandbox("03b", secrets=ANTHROPIC_ENV, env_vars=ANTHROPIC_MODEL)
try:
    print(run(sbx, "echo ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"))
    attempt(sbx, "json")
    out = run(sbx, f"claude -p '{PROMPT}' --output-format stream-json --verbose < /dev/null | wc -l", timeout=180)
    print(f"{'stream-json':34} {out} events")
finally:
    sbx.delete()

banner("C. Amazon Bedrock, AWS_BEARER_TOKEN_BEDROCK bound to a Daytona Secret")
sbx = create_sandbox("03c", secrets=BEDROCK_ENV, env_vars=bedrock_env())
try:
    print(run(sbx, "env | grep -E '^(AWS_|CLAUDE_CODE_USE_BEDROCK|ANTHROPIC_MODEL)' | sort"))
    attempt(sbx, "bedrock json")
    out = run(sbx, f"claude -p '{PROMPT}' --output-format stream-json --verbose < /dev/null | wc -l", timeout=180)
    print(f"{'bedrock stream-json':34} {out} events")
    print("no AWS credential files:", run(sbx, "ls ~/.aws 2>&1 | head -1"))
finally:
    sbx.delete()
