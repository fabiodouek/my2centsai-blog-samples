"""Test 3: how timeouts behave. set_timeout sets an exact value, connect only extends, and expiry kills by default."""

import time
from datetime import datetime, timezone

from e2b import Sandbox

from common import banner, tagged


def remaining(sbx: Sandbox) -> float:
    return (sbx.get_info().end_at - datetime.now(timezone.utc)).total_seconds()


sbx = Sandbox.create(timeout=120, metadata=tagged(test="03"))
sandbox_id = sbx.sandbox_id
print(f"sandbox: {sandbox_id}")

banner("Initial timeout")
print(f"remaining: {remaining(sbx):.0f}s (asked for 120)")

banner("set_timeout(20): exact, can shorten")
sbx.set_timeout(20)
print(f"remaining: {remaining(sbx):.0f}s")

banner("connect(timeout=5): only extends, never shortens")
sbx.connect(timeout=5)
print(f"remaining: {remaining(sbx):.0f}s")

banner("Wait for expiry (default on_timeout = kill)")
time.sleep(remaining(sbx) + 10)

try:
    info = Sandbox.get_info(sandbox_id)
    print(f"still there? state={info.state}")
except Exception as e:
    print(f"gone: {type(e).__name__}: {e}")

running = Sandbox.list(query=None).next_items()
print(f"listed: {sandbox_id in [s.sandbox_id for s in running]}")
