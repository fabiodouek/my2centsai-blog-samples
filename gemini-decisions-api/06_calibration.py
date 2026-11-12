"""Calibration check on labeled data the model's makers did not pick.

500 AG News articles, one request each with two questions:
  topic     (choice, 4 options)  -> accuracy, top-1 calibration
  is_sports (noul)               -> yes/no calibration

"Calibrated" means that among answers given with confidence around 0.8, about
80% are right. The reliability table shows that bucket by bucket, and ECE
(expected calibration error) is the size-weighted average gap.

Then 20 items are sent 5 times each to see whether answers change between runs.
"""

import json
from concurrent.futures import ThreadPoolExecutor

from common import banner, post, save, summary
from dataset import CRITERIA, INSTRUCTIONS, load

QUESTIONS = {
    "topic": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA},
    "is_sports": {"type": "noul", "instructions": "Is this article about sports?"},
}
items = load()


def ask(item: dict) -> dict:
    for _ in range(4):
        status, body, ms = post({"input": item["text"], "questions": QUESTIONS}, expect_tokens=200)
        if status == 200:
            return {"label": item["label"], "ms": ms, "topic": body["answers"]["topic"],
                    "is_sports": body["answers"]["is_sports"], "tokens": body["usage"]["total_input_tokens"]}
    return {"label": item["label"], "error": {"status": status, "body": body}}


def reliability(pairs: list[tuple[float, bool]], bins: int = 10) -> tuple[list[dict], float]:
    """pairs = (stated confidence, was it right). Returns the table and ECE."""
    table, ece = [], 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inside = [(c, ok) for c, ok in pairs if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if not inside:
            continue
        conf = sum(c for c, _ in inside) / len(inside)
        acc = sum(ok for _, ok in inside) / len(inside)
        ece += len(inside) / len(pairs) * abs(conf - acc)
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(inside), "mean_confidence": round(conf, 3), "accuracy": round(acc, 3)})
    return table, round(ece, 4)


def show(table: list[dict]) -> None:
    print(f"{'confidence':<11} {'n':>4} {'stated':>7} {'actual':>7}")
    for r in table:
        print(f"{r['bin']:<11} {r['n']:>4} {r['mean_confidence']:>7.3f} {r['accuracy']:>7.3f}")


banner(f"{len(items)} articles, 2 questions each")
post({"input": "warm up", "questions": QUESTIONS})
with ThreadPoolExecutor(4) as pool:
    answers = list(pool.map(ask, items))
errors = [a for a in answers if "error" in a]
ok = [a for a in answers if "error" not in a]
print(f"answered: {len(ok)}, errors: {len(errors)}")
for e in errors[:3]:
    print(json.dumps(e["error"])[:300])

banner("topic (choice)")
correct = [a["topic"]["choice"] == a["label"] for a in ok]
accuracy = sum(correct) / len(ok)
table, ece = reliability([(a["topic"]["confidence"], c) for a, c in zip(ok, correct)])
print(f"accuracy = {accuracy:.3f}   top-1 ECE = {ece}")
show(table)
for threshold in (0.5, 0.7, 0.9, 0.95):
    kept = [c for a, c in zip(ok, correct) if a["topic"]["confidence"] >= threshold]
    if kept:
        print(f"confidence >= {threshold}: keeps {len(kept) / len(ok):.1%} of items, accuracy {sum(kept) / len(kept):.3f}")

banner("is_sports (yes/no)")
truth = [a["label"] == "sports" for a in ok]
p = [a["is_sports"]["noul"] for a in ok]
bin_acc = sum((x > 0.5) == t for x, t in zip(p, truth)) / len(ok)
table2, ece2 = reliability([(max(x, 1 - x), (x > 0.5) == t) for x, t in zip(p, truth)])
print(f"accuracy = {bin_acc:.3f}   ECE = {ece2}   min P = {min(p)}   max P = {max(p)}")
print(f"answers at the clamp: {sum(x <= 0.01 for x in p)} at 0.01, {sum(x >= 0.99 for x in p)} at 0.99")
show(table2)

banner("latency over the run (4 parallel connections)")
lat = summary([a["ms"] for a in ok])
print(lat, f"total tokens = {sum(a['tokens'] for a in ok):,}")

banner("determinism: 20 items, 5 runs each")
drift, flips = [], 0
for item in items[:20]:
    runs = [ask(item) for _ in range(5)]
    runs = [r for r in runs if "error" not in r]
    conf = [r["topic"]["confidence"] for r in runs]
    drift.append(max(conf) - min(conf))
    flips += len({r["topic"]["choice"] for r in runs}) > 1
print(f"items whose top choice changed between runs: {flips} of 20")
print(f"largest confidence spread for one item: {max(drift):.4f}; items with any spread: {sum(d > 0 for d in drift)}")

save("06_calibration", {
    "n": len(ok), "errors": len(errors),
    "topic": {"accuracy": round(accuracy, 4), "ece": ece, "reliability": table},
    "is_sports": {"accuracy": round(bin_acc, 4), "ece": ece2, "reliability": table2, "min": min(p), "max": max(p)},
    "latency": lat, "total_tokens": sum(a["tokens"] for a in ok),
    "determinism": {"choice_flips": flips, "max_confidence_spread": round(max(drift), 4), "items_with_spread": sum(d > 0 for d in drift)},
    "answers": [{"label": a["label"], "choice": a["topic"]["choice"], "confidence": a["topic"]["confidence"], "is_sports": a["is_sports"]["noul"]} for a in ok],
})
