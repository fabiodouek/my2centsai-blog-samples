"""Shared helpers for the Gemini Decisions API scripts.

Two ways to call the API are used on purpose:

* `sdk_client()` returns the google-genai client, pinned to v1beta. This is what
  you would use in an application (scripts 01 to 03).
* `post()` sends the same JSON over plain HTTPS on one keep-alive connection and
  returns the status code, the parsed body and the wall-clock time. The
  measurement and error scripts (04 to 07) use it so that timings exclude SDK
  retries and so that error bodies come back untouched.
"""

import base64
import json
import os
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
RESULTS = HERE / "results"
DATA = HERE / "data"
MODEL = "gemini-decision-1"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
PRICE_PER_M = 0.10  # USD per 1M input tokens, every modality; output is always 0 tokens


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Set GEMINI_API_KEY first (see .env.example).")
    return key


def sdk_client():
    from google import genai

    # google-genai 3.x defaults to the v1 API. Decisions only exists on v1beta.
    return genai.Client(api_key=api_key(), http_options={"api_version": "v1beta"})


_http: httpx.Client | None = None


def http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=120, headers={"x-goog-api-key": api_key()})
    return _http


class Pacer:
    """Keeps the tokens sent in any 60 s window under 80% of DECISIONS_TPM."""

    def __init__(self) -> None:
        self.budget = int(os.environ.get("DECISIONS_TPM", "1000000")) * 0.8
        self.sent: deque[tuple[float, int]] = deque()

    def wait(self, tokens: int) -> None:
        while True:
            now = time.monotonic()
            while self.sent and now - self.sent[0][0] > 60:
                self.sent.popleft()
            used = sum(t for _, t in self.sent)
            if not self.sent or used + tokens <= self.budget:
                break
            time.sleep(max(0.5, 60 - (now - self.sent[0][0])))
        self.sent.append((time.monotonic(), tokens))


PACER = Pacer()
_LOCK = threading.Lock()  # the calibration scripts call post() from several threads
SPEND = RESULTS / "spend.json"


def _record(tokens: int, model: str = MODEL, usd: float | None = None) -> None:
    with _LOCK:
        RESULTS.mkdir(exist_ok=True)
        s = json.loads(SPEND.read_text()) if SPEND.exists() else {}
        m = s.setdefault(model, {"requests": 0, "input_tokens": 0, "usd": 0.0})
        m["requests"] += 1
        m["input_tokens"] += tokens
        m["usd"] = round(m["usd"] + (tokens * PRICE_PER_M / 1e6 if usd is None else usd), 6)
        SPEND.write_text(json.dumps(s, indent=2))


def post(body: dict, expect_tokens: int = 0, pace: bool = True) -> tuple[int, dict, float]:
    """POST one Decisions request. Returns (status, body, milliseconds)."""
    if pace and expect_tokens:
        with _LOCK:
            PACER.wait(expect_tokens)
    t0 = time.perf_counter()
    r = http().post(ENDPOINT, json={"model": MODEL, **body})
    ms = (time.perf_counter() - t0) * 1000
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text[:2000]}
    if r.status_code == 200:
        _record(out.get("usage", {}).get("total_input_tokens", 0))
    return r.status_code, out, ms


def record_sdk_usage(result) -> None:
    _record(result.usage.total_input_tokens)


def record_other(model: str, tokens: int, usd: float) -> None:
    _record(tokens, model, usd)


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def pct(values: list[float], p: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, round(p / 100 * (len(v) - 1)))]


def summary(ms: list[float]) -> dict:
    return {"n": len(ms), "p50_ms": round(statistics.median(ms)), "p95_ms": round(pct(ms, 95)),
            "min_ms": round(min(ms)), "max_ms": round(max(ms))}


def save(name: str, data) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2))
    print(f"\nsaved results/{name}.json")


def banner(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def show(obj) -> None:
    print(json.dumps(obj, indent=2), flush=True)


def usd(tokens: int) -> str:
    return f"${tokens * PRICE_PER_M / 1e6:.6f}"


TICKET = {
    "ticket_id": "NP-48213",
    "customer": {"plan": "Business", "since": "2023-02", "open_tickets": 1},
    "subject": "Charged twice and the app still says payment failed",
    "message": (
        "Hi, I tried to renew our Business plan this morning. The app showed a red "
        "'Payment failed' error (PAY-4012) so I tapped Pay again and got the same error. "
        "My bank now shows two charges of 89 euros from Northwind Parcels. I need one of "
        "them refunded today. If this is not sorted by Friday we will move our shipping "
        "to another provider."
    ),
}
