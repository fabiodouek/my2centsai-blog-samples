"""Test 2: Claude Code with no Anthropic key in the sandbox.

The sandbox gets a placeholder ANTHROPIC_API_KEY so Claude Code starts; the proxy overwrites the
x-api-key header on api.anthropic.com with the workspace secret.
"""

import time

from common import ANTHROPIC_RULE, banner, claude, client, create_sandbox, run

PROMPT = "Reply with exactly: ok"


def attempt(sbx, label: str) -> None:
    t0 = time.perf_counter()
    try:
        r = claude(sbx, PROMPT, timeout=120)
        print(f"{label:28} {r.get('result', '')!r:10} error={r.get('is_error')} "
              f"cost=${r.get('total_cost_usd', 0):.4f} {time.perf_counter() - t0:.1f}s")
    except Exception as e:
        print(f"{label:28} FAILED: {str(e)[-300:]}")


banner("A. No rule: placeholder key goes to Anthropic as-is")
sbx = create_sandbox("03a", rules=[{**ANTHROPIC_RULE, "headers": [], "name": "env-only"}])
try:
    print(run(sbx, "claude --version; echo ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"))
    out = run(sbx, f"timeout 60 claude -p '{PROMPT}' --output-format json < /dev/null | jq -c '{{is_error, result}}'", timeout=90)
    print("no rule:", out[-300:])
finally:
    client.delete_sandbox(sbx.name)

banner("B. Header rule on api.anthropic.com")
sbx = create_sandbox("03b", rules=[ANTHROPIC_RULE])
try:
    attempt(sbx, "json")
    out = run(sbx, f"claude -p '{PROMPT}' --output-format stream-json --verbose < /dev/null | wc -l", timeout=120)
    print(f"{'stream-json':28} {out} events")
finally:
    client.delete_sandbox(sbx.name)
