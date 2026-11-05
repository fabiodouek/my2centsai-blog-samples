"""Estimated Claude API cost for Claude Code sessions, from token counts.

PRICING: USD per million tokens. Source: the `claude-api` skill bundled with Claude Code 2.1.317 (its
"Current Models" table, cached 2026-10-06, and shared/models.md / shared/prompt-caching.md), read
2026-11-03. Input/output are first-party API list prices. Cache writes are priced at the 5-minute TTL rate
(1.25x input; 1-hour writes are 2x). Cache reads: the skill states $0.25 for Fable 5.1, $1.00 for Fable 5,
$0.20 for Opus 5.5 and Sonnet 5.5; other models use the standard 0.1x input. Haiku 5.5 is the <=100K-token
prompt rate. Claude Code sessions via Bedrock/Vertex bill differently; this is an estimate, not a bill.
"""

from __future__ import annotations

# User-facing text (the Claude Code panel shows source and as_of); provenance is in the docstring above.
PRICING_SOURCE = "Anthropic"
PRICING_AS_OF = "2026-11-03"
PRICING_NOTE = "Estimated from Anthropic list prices as of 2026-11-03"


def _p(inp: float, out: float, cache_read: float | None = None) -> dict:
    return {"input": inp, "output": out, "cache_write": round(inp * 1.25, 4), "cache_write_1h": inp * 2,
            "cache_read": cache_read if cache_read is not None else round(inp * 0.1, 4)}


PRICING: dict[str, dict] = {
    "claude-fable-5-1": _p(10.00, 50.00, 0.25),
    "claude-mythos-5-1": _p(10.00, 50.00, 0.25),
    "claude-fable-5": _p(10.00, 50.00, 1.00),
    "claude-mythos-5": _p(10.00, 50.00, 1.00),
    "claude-opus-5-5": _p(4.00, 20.00, 0.20),
    "claude-opus-5": _p(5.00, 25.00),
    "claude-opus-4-8": _p(5.00, 25.00),
    "claude-opus-4-7": _p(5.00, 25.00),
    "claude-opus-4-6": _p(5.00, 25.00),
    "claude-sonnet-5-5": _p(2.00, 10.00, 0.20),
    "claude-sonnet-5": _p(2.00, 10.00),
    "claude-sonnet-4-6": _p(3.00, 15.00),
    "claude-haiku-5-5": _p(0.10, 0.50),
    "claude-haiku-4-5": _p(1.00, 5.00),
}


def price_for(model: str | None) -> tuple[str, dict] | None:
    """Longest model-ID prefix match, so dated or suffixed IDs (`claude-haiku-4-5-20251001`,
    `us.anthropic.claude-opus-5-5`, `claude-opus-5-5[1m]`) find their row. `claude-opus-5` must not
    swallow `claude-opus-5-5`, hence longest first."""
    if not model:
        return None
    m = model.lower()
    for prefix in ("us.anthropic.", "eu.anthropic.", "global.anthropic.", "anthropic."):
        if m.startswith(prefix):
            m = m[len(prefix):]
    for key in sorted(PRICING, key=len, reverse=True):
        if m == key or m.startswith(key + "-") or m.startswith(key + "[") or m.startswith(key + "@"):
            return key, PRICING[key]
    return None


def _n(d: dict, k: str) -> int:
    try:
        return int(d.get(k) or 0)
    except (TypeError, ValueError):
        return 0


def session_cost(sess: dict) -> float | None:
    hit = price_for(sess.get("model"))
    if not hit:
        return None
    p = hit[1]
    write_1h = _n(sess, "cache_creation_1h_tokens")
    write_5m = max(0, _n(sess, "cache_creation_tokens") - write_1h)
    usd = (_n(sess, "input_tokens") * p["input"] + _n(sess, "output_tokens") * p["output"]
           + _n(sess, "cache_read_tokens") * p["cache_read"] + write_5m * p["cache_write"]
           + write_1h * p["cache_write_1h"]) / 1_000_000
    return round(usd, 6)


TOKEN_FIELDS = ("turns", "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def with_costs(usage: dict) -> dict:
    """Agent /claude-usage payload + est_cost_usd per session and in the totals."""
    sessions = [dict(s) for s in (usage.get("sessions") or []) if isinstance(s, dict)]
    total_cost, unpriced = 0.0, set()
    for s in sessions:
        c = session_cost(s)
        s["est_cost_usd"] = c
        hit = price_for(s.get("model"))
        s["priced_as"] = hit[0] if hit else None
        if c is None:
            if s.get("model"):
                unpriced.add(s["model"])
        else:
            total_cost += c
    totals = dict(usage.get("totals") or {})
    for f in TOKEN_FIELDS:
        totals.setdefault(f, sum(_n(s, f) for s in sessions))
    totals["sessions"] = totals.get("sessions", len(sessions))
    totals["est_cost_usd"] = round(total_cost, 6)
    out = {k: v for k, v in usage.items() if k not in ("sessions", "totals")}
    out.update(sessions=sessions, totals=totals, unpriced_models=sorted(unpriced),
               pricing={"source": PRICING_SOURCE, "as_of": PRICING_AS_OF, "note": PRICING_NOTE})
    return out


# ---------------------------------------------------------------- Lambda MicroVM compute

# Lambda MicroVMs, us-east-1, per second while running: vCPU and memory (GB), from the size's baseline (an image's
# minimumMemoryInMiB, vCPU = memory / 2 GB). Medium (1 vCPU, 2 GB) is about $0.126/h. Bursts above the baseline and
# requests aren't counted; this is an estimate, not a bill.
MICROVM_VCPU_SECOND = 0.0000276944
MICROVM_GB_SECOND = 0.0000036667
MICROVM_PRICING_NOTE = "Lambda MicroVMs, us-east-1: $0.0000276944 per vCPU-second plus $0.0000036667 per GB-second while running"


def microvm_hourly(vcpu: float, memory_gb: float) -> float:
    """USD per hour of running at the size's baseline."""
    return round((vcpu * MICROVM_VCPU_SECOND + memory_gb * MICROVM_GB_SECOND) * 3600, 4)


def public_pricing() -> dict:
    return {"source": PRICING_SOURCE, "as_of": PRICING_AS_OF, "note": PRICING_NOTE, "unit": "USD per 1M tokens",
            "notes": "cache_write = 5-minute TTL rate (1.25x input); estimates only",
            "models": {k: {f: v[f] for f in ("input", "output", "cache_read", "cache_write")} for k, v in PRICING.items()},
            "microvm": {"vcpu_second": MICROVM_VCPU_SECOND, "gb_second": MICROVM_GB_SECOND, "note": MICROVM_PRICING_NOTE}}
