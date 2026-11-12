"""Context scaling: latency as the shared input grows from about 1K to about 250K tokens.

Google's claim is 150 to 250 ms end to end for contexts up to 16K tokens. The
clock here wraps the whole HTTPS request on a warm connection, so it includes
the network path from wherever you run it.

The largest sizes need a paid tier: the free tier allows 50,000 tokens per
minute, which is less than one of these requests.
"""

import json
import random

from common import banner, post, save, summary, usd

REPS = 5
TARGETS = [1_000, 4_000, 16_000, 64_000, 128_000, 250_000]
QUESTIONS = {
    "mentions_refund": {"type": "noul", "instructions": "Does any log line mention a refund being issued?"},
    "worst_severity": {"type": "choice", "instructions": "What is the highest severity that appears in the log?",
                       "criteria": {"info": "", "warning": "", "error": "", "critical": ""}},
}
rng = random.Random(7)
EVENTS = ["parcel scanned at depot", "label printed", "payment authorised", "address validated",
          "courier assigned", "delivery attempted", "customer notified by email", "invoice generated"]


def log(n_lines: int) -> str:
    lines = [f"2026-11-{rng.randint(1, 12):02d}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d} INFO "
             f"order {rng.randint(100000, 999999)} {rng.choice(EVENTS)}" for _ in range(n_lines)]
    lines[len(lines) // 2] = "2026-11-12T09:15 ERROR order 482130 payment captured twice, code PAY-4012"
    return "\n".join(lines)


# Measure tokens per log line once, then size every input from that.
status, body, _ = post({"input": log(200), "questions": QUESTIONS})
if status != 200:
    raise SystemExit(f"{status} {json.dumps(body)[:400]}")
per_line = body["usage"]["total_input_tokens"] / 200
print(f"about {per_line:.1f} tokens per log line")

rows = []
for target in TARGETS:
    text = log(int(target / per_line))
    banner(f"target {target:,} tokens, {REPS} requests")
    times, tokens, answers = [], 0, None
    for _ in range(REPS):
        status, body, ms = post({"input": text, "questions": QUESTIONS}, expect_tokens=target)
        if status != 200:
            print(status, json.dumps(body)[:600])
            rows.append({"target": target, "status": status, "error": body})
            break
        times.append(ms)
        tokens = body["usage"]["total_input_tokens"]
        answers = {k: v.get("noul", v.get("choice")) for k, v in body["answers"].items()}
    if times:
        row = {"target": target, "input_tokens": tokens, **summary(times), "answers": answers}
        rows.append(row)
        print(row)

banner("summary")
print(f"{'tokens':>9} {'p50 ms':>7} {'p95 ms':>7} {'cost/request':>13}  answers")
for r in rows:
    if "p50_ms" in r:
        print(f"{r['input_tokens']:>9,} {r['p50_ms']:>7} {r['p95_ms']:>7} {usd(r['input_tokens']):>13}  {r['answers']}")
save("05_context_scaling", rows)
