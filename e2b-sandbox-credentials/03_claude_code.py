"""Test 3: Claude Code in a sandbox that never holds the Anthropic key.

Runs the same prompt twice: once in a sandbox with no injection rule (control), once with the
x-api-key rule. Both sandboxes carry the same placeholder ANTHROPIC_API_KEY.
"""

import json
import time

from common import banner, claude, create_sandbox, run

PROMPT = "Reply with exactly one word: pong"


def ask(sbx) -> dict:
    t0 = time.perf_counter()
    result = claude(sbx, PROMPT, timeout=180)
    result["_wall_s"] = time.perf_counter() - t0
    return result


def show(r: dict) -> None:
    print(
        f"is_error={r['is_error']}  api_error_status={r.get('api_error_status')}  "
        f"result={r['result']!r}  cost=${r['total_cost_usd']:.4f}  wall={r['_wall_s']:.1f}s"
    )


banner("Control: placeholder key, no injection rule")
sbx = create_sandbox("03-control", anthropic=False, github=False, extra_allow=("api.anthropic.com",))
try:
    print(run(sbx, "echo ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"))
    show(ask(sbx))
finally:
    sbx.kill()

banner("Injected: same placeholder, x-api-key rule on api.anthropic.com")
sbx = create_sandbox("03-injected", github=False)
try:
    print(run(sbx, "echo ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"))
    print("network config E2B stores for this sandbox:")
    print(json.dumps(sbx.get_info().network, indent=2, default=str))
    show(ask(sbx))

    banner("Streaming (SSE) through the proxy")
    out = run(sbx, f"claude -p '{PROMPT}' --output-format stream-json --verbose", timeout=180)
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    print(f"{len(events)} stream events, types: {sorted({e['type'] for e in events})}")
    print(f"final: is_error={events[-1]['is_error']} result={events[-1]['result']!r}")
finally:
    sbx.kill()
