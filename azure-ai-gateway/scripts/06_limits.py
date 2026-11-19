"""Token limit then cost limit, each hit with a burst. Prints per-call usage and the gateway's cost header."""
import time

import common

IN, OUT = 0.40e-6, 1.60e-6  # gpt-4.1-mini list price per token (input, output), for comparison


PROMPT = "Describe what an API gateway does, in about 120 words."


def burst(n, label):
    print(f"--- {label}")
    total_tokens, total_cost = 0, 0.0
    for i in range(1, n + 1):
        r, dt = common.call(prompt=PROMPT)
        if r.status_code == 200:
            u = r.json()["usage"]
            total_tokens += u["total_tokens"]
            mine = u["prompt_tokens"] * IN + u["completion_tokens"] * OUT
            total_cost += mine
            hdr = {k: v for k, v in r.headers.items() if k.lower().startswith("x-cost")}
            print(f"call {i}: 200  {u['total_tokens']:3d} tokens (running {total_tokens:3d})  my price ${mine:.6f} (running ${total_cost:.6f})  {hdr}")
        else:
            extra = {k: v for k, v in r.headers.items() if k.lower() in ("retry-after",)}
            print(f"call {i}: {r.status_code}  {common.short(r, 60)}  {extra}")
    return total_tokens


common.aig("policy", "set-token-limit", "--token-limit", "300", "--token-period", "minute",
           "--token-counter-key", "identity", "-o", "none")
print("token limit 300 a minute set; waiting 40 s for it to apply")
time.sleep(40)
burst(6, "token limit: 300 tokens a minute")
common.aig("policy", "remove", "--remove-token-limit", "true", "-o", "none")
common.aig("policy", "set-cost-limit", "--amount", "0.001", "--period", "hour", "--counter-key", "identity",
           "--id", "demo-budget", "-o", "none")
print("cost limit $0.001 an hour set; waiting 40 s for it to apply and the token window to reset")
time.sleep(40)
burst(6, "cost limit: $0.001 an hour")
common.aig("policy", "remove", "--remove-cost-limit", "true", "-o", "none")
