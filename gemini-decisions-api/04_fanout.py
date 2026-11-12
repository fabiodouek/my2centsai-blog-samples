"""Fan-out: 1, 10, 100 and 1,000 questions over one shared input.

Checks two claims: latency stays roughly flat as questions are added, and the
shared input is billed once per request however many questions ride on it.
"""

import json

from common import TICKET, banner, post, save, summary, usd

REPS = 10
# About 2,000 tokens of shared input: the ticket plus a synthetic account history.
history = "\n".join(
    f"2026-{m:02d}-12 invoice INV-{1000 + m} Business plan EUR 89.00 paid; parcels shipped {300 + 7 * m}; tickets opened {m % 3}"
    for m in range(1, 11)
) * 4
SHARED = json.dumps(TICKET) + "\n" + history

TOPICS = ["a refund", "a duplicate charge", "a failed payment", "a damaged parcel", "a late delivery",
          "a login problem", "a plan upgrade", "a cancellation", "an invoice correction", "a change of address"]


def questions(n: int) -> dict:
    return {
        f"q{i:04d}": {"type": "noul", "instructions": f"Check {i}: does the ticket message concern {TOPICS[i % len(TOPICS)]}?"}
        for i in range(n)
    }


post({"input": "warm up", "questions": questions(1)})  # open the connection before timing
rows = []
for n in (1, 10, 100, 1000):
    banner(f"{n} question(s), {REPS} requests")
    times, tokens = [], 0
    for _ in range(REPS):
        status, body, ms = post({"input": SHARED, "questions": questions(n)}, expect_tokens=tokens or 2500 + 25 * n)
        if status != 200:
            print(status, json.dumps(body)[:400])
            break
        times.append(ms)
        tokens = body["usage"]["total_input_tokens"]
    if not times:
        continue
    row = {"questions": n, "input_tokens": tokens, "answers": len(body["answers"]), **summary(times)}
    rows.append(row)
    print(f"answers={row['answers']} input_tokens={tokens:,} p50={row['p50_ms']} ms p95={row['p95_ms']} ms")

banner("summary")
print(f"{'questions':>9} {'tokens':>8} {'tokens/extra q':>15} {'p50 ms':>7} {'p95 ms':>7} {'cost/request':>13} {'cost/question':>14}")
base = rows[0]["input_tokens"] if rows else 0
for r in rows:
    per_q = (r["input_tokens"] - base) / (r["questions"] - 1) if r["questions"] > 1 else 0
    print(f"{r['questions']:>9} {r['input_tokens']:>8} {per_q:>15.1f} {r['p50_ms']:>7} {r['p95_ms']:>7} "
          f"{usd(r["input_tokens"]):>13} {"$%.8f" % (r["input_tokens"] * 0.10 / 1e6 / r["questions"]):>14}")
save("04_fanout", rows)
