"""Baseline: the same 500 articles through Gemini 3.5 Flash-Lite with an enum
structured output, the do-it-yourself way to get a closed-set answer.

Then sends the same articles to gemini-decision-1 with the same single question
and compares accuracy, latency and cost, to test Google's "nearly five times
cheaper" claim on a workload I picked. A generative
model returns one label and no probability, so there is nothing to calibrate.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from common import banner, post, record_other, save, sdk_client, summary
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
print(f"accuracy={acc:.3f} p50={lat['p50_ms']} ms p95={lat['p95_ms']} ms input={tin:,} output={tout:,} cost=${cost:.4f}")

banner("like for like: gemini-decision-1 with only the topic question")


def ask_decisions(item: dict) -> dict:
    status, body, ms = post({"input": item["text"], "questions": {
        "topic": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}}}, expect_tokens=200)
    if status != 200:
        return {"error": body}
    return {"label": item["label"], "topic": body["answers"]["topic"]["choice"], "ms": ms, "in": body["usage"]["total_input_tokens"]}


ask_decisions(items[0])  # warm up
with ThreadPoolExecutor(4) as pool:
    dec = [d for d in pool.map(ask_decisions, items) if "error" not in d]
d_in = sum(d["in"] for d in dec)
d_cost = d_in * 0.10 / 1e6
d_acc = sum(d["topic"] == d["label"] for d in dec) / len(dec)
d_lat = summary([d["ms"] for d in dec])
agree = sum(a["topic"] == d["topic"] for a, d in zip(answers, dec) if "error" not in a) / len(dec)

print(f"{'':<22} {'accuracy':>8} {'p50 ms':>7} {'p95 ms':>7} {'tokens in':>10} {'tokens out':>10} {'$ / 1k items':>13}")
print(f"{'gemini-decision-1':<22} {d_acc:>8.3f} {d_lat['p50_ms']:>7} {d_lat['p95_ms']:>7} {d_in:>10,} {0:>10} {d_cost / len(dec) * 1000:>13.5f}")
print(f"{MODEL:<22} {acc:>8.3f} {lat['p50_ms']:>7} {lat['p95_ms']:>7} {tin:>10,} {tout:>10,} {cost / len(ok) * 1000:>13.5f}")
ratio = (cost / len(ok)) / (d_cost / len(dec))
print(f"\nFlash-Lite cost / Decisions cost = {ratio:.1f}x")
print(f"the two models picked the same topic on {agree:.1%} of the articles")
row["decisions"] = {"n": len(dec), "accuracy": round(d_acc, 4), **d_lat, "input_tokens": d_in,
                    "usd_per_1k_items": round(d_cost / len(dec) * 1000, 5), "cost_ratio": round(ratio, 2), "agreement": round(agree, 4)}
save("08_flash_lite_baseline", row)
