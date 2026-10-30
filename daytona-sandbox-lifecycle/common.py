"""Shared helpers: env loading, timing, and a label so cleanup only touches our sandboxes."""

import time
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

# Every sandbox created by these scripts carries this label. 09_cleanup.py deletes by it.
DEMO_LABELS = {"demo": "my2cents-lifecycle"}
# Safety net: anything a crashed script leaves behind is destroyed after this many minutes, in any state.
SAFETY_TTL = 60


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


def labels(**extra: str) -> dict:
    return {**DEMO_LABELS, **extra}
