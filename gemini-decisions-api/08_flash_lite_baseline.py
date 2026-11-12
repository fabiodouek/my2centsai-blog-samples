"""Baseline: the same 500 articles through Gemini 3.5 Flash-Lite with an enum
structured output, the do-it-yourself way to get a closed-set answer.

Compares accuracy, latency and cost with results/06_calibration.json, to test
Google's "nearly five times cheaper" claim on a workload I picked. A generative
model returns one label and no probability, so there is nothing to calibrate.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from common import RESULTS, banner, record_other, save, sdk_client, summary
from dataset import CRITERIA, INSTRUCTIONS, load

MODEL = "gemini-3.5-flash-lite"
IN_PER_M, OUT_PER_M = 0.30, 2.50  # USD per 1M tokens, standard tier
client = sdk_client()
items = load()
PROMPT = INSTRUCTIONS + "\n" + "\n".join(f"- {k}: {v}" for k, v in CRITERIA.items()) + "\n\nArticle:\n"
CONFIG = {
    "response_mime_type": "application/json",
    "response_schema": {"type": "OBJECT", "properties": {"topic": {"type": "STRING", "enum": list(CRITERIA)}}, "required": ["topic"]},
    "temperature": 0,
}


def ask(item: dict) -> dict:
    t0 = time.perf_counter()
    try:
        r = client.models.generate_content(model=MODEL, contents=PROMPT + item["text"], config=CONFIG)
    except Exception as e:
        return {"label": item["label"], "error": f"{type(e).__name__}: {str(e)[:300]}"}
    ms = (time.perf_counter() - t0) * 1000
    u = r.usage_metadata
    out_tokens = (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
    try:
        topic = json.loads(r.text)["topic"]
    except Exception:
        topic = None
    return {"label": item["label"], "topic": topic, "ms": ms, "in": u.prompt_token_count or 0, "out": out_tokens,
            "thoughts": u.thoughts_token_count or 0}


banner(f"{len(items)} articles through {MODEL}")
ask(items[0])  # warm up
with ThreadPoolExecutor(4) as pool:
    answers = list(pool.map(ask, items))
errors = [a for a in answers if "error" in a]
ok = [a for a in answers if "error" not in a]
print(f"answered: {len(ok)}, errors: {len(errors)}")
for e in errors[:3]:
    print(e["error"])
if not ok:
    raise SystemExit("no answers")

tin, tout = sum(a["in"] for a in ok), sum(a["out"] for a in ok)
cost = tin * IN_PER_M / 1e6 + tout * OUT_PER_M / 1e6
record_other(MODEL, tin + tout, cost)
acc = sum(a["topic"] == a["label"] for a in ok) / len(ok)
lat = summary([a["ms"] for a in ok])
row = {"model": MODEL, "n": len(ok), "accuracy": round(acc, 4), **lat, "input_tokens": tin, "output_tokens": tout,
       "thought_tokens": sum(a["thoughts"] for a in ok), "usd": round(cost, 6), "usd_per_1k_items": round(cost / len(ok) * 1000, 5)}
print(row)

banner("side by side")
dec_path = RESULTS / "06_calibration.json"
if dec_path.exists():
    d = json.loads(dec_path.read_text())
    d_cost = d["total_tokens"] * 0.10 / 1e6
    print(f"{'':<24} {'accuracy':>8} {'p50 ms':>7} {'p95 ms':>7} {'tokens in':>10} {'tokens out':>10} {'$ / 1k items':>13}")
    print(f"{'gemini-decision-1 (*)':<24} {d['topic']['accuracy']:>8.3f} {d['latency']['p50_ms']:>7} {d['latency']['p95_ms']:>7} "
          f"{d['total_tokens']:>10,} {0:>10} {d_cost / d['n'] * 1000:>13.5f}")
    print(f"{MODEL:<24} {acc:>8.3f} {lat['p50_ms']:>7} {lat['p95_ms']:>7} {tin:>10,} {tout:>10,} {cost / len(ok) * 1000:>13.5f}")
    print(f"\nFlash-Lite cost / Decisions cost = {cost / len(ok) / (d_cost / d['n']):.1f}x")
    print("(*) the Decisions request also carried a second yes/no question per article")
    row["decisions_usd_per_1k_items"] = round(d_cost / d["n"] * 1000, 5)
else:
    print("run 06_calibration.py first for the comparison")
save("08_flash_lite_baseline", row)
