"""Shared helpers: env loading, timing, a REST helper, and a name/label so cleanup only touches our sandboxes."""

import os
import time
from contextlib import contextmanager

import httpx
from dotenv import load_dotenv

load_dotenv()

from langsmith.sandbox import Sandbox, SandboxClient  # noqa: E402  (reads LANGSMITH_* from the env)

# Every sandbox is named my2cents-lc-<test>-<ts> and carries this label. 10_cleanup.py deletes by both.
PREFIX = "my2cents-lc"
DEMO_LABELS = {"demo": "my2cents-lifecycle"}
# Safety nets: a crashed script's sandbox stops after 5 idle minutes and is deleted 10 minutes after that.
SAFETY_IDLE_TTL = 300
SAFETY_DELETE_AFTER_STOP = 600
GiB = 1024**3

client = SandboxClient()
API = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/") + "/api/v2/sandboxes"
_http = httpx.Client(headers={"x-api-key": os.environ["LANGSMITH_API_KEY"]}, timeout=120)


def rest(method: str, path: str, **kw):
    """Call the REST API directly, for what the Python SDK doesn't expose (labels, memory, filters, usage)."""
    r = _http.request(method, f"{API}{path}", **kw)
    if r.is_error:
        raise RuntimeError(f"HTTP {r.status_code} {method} {path}: {r.text.strip()[:300]}")
    return r.json() if r.content else None


def name(test: str) -> str:
    return f"{PREFIX}-{test}-{int(time.time() * 1000) % 10**8}"


def create(test: str, **body) -> Sandbox:
    """Create a labelled sandbox over REST (the SDK has no labels) and wrap it in the SDK's Sandbox."""
    payload = {
        "name": name(test),
        "labels": {**DEMO_LABELS, "test": test},
        "idle_ttl_seconds": SAFETY_IDLE_TTL,
        "delete_after_stop_seconds": SAFETY_DELETE_AFTER_STOP,
        "wait_for_ready": True,
        "timeout": 120,
        **body,
    }
    return Sandbox.from_dict(rest("POST", "/boxes", json=payload), client=client, auto_delete=False)


def wait_status(sb_name: str, want: str, timeout: float = 600, poll: float = 0.5) -> float:
    """Poll the lightweight status endpoint until it reports `want`; return seconds waited."""
    start = time.perf_counter()
    while (status := client.get_sandbox_status(sb_name).status) != want:
        if time.perf_counter() - start > timeout:
            raise TimeoutError(f"{sb_name} still {status} after {timeout}s")
        time.sleep(poll)
    return time.perf_counter() - start


def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


@contextmanager
def timed(label: str, results: dict | None = None):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    if results is not None:
        results[label] = elapsed
    print(f"[{elapsed:6.2f}s] {label}")
