"""First call through the alias with the gateway key. Retries while the new role assignment propagates."""
import time

import common

for attempt in range(1, 9):
    r, dt = common.call()
    print(f"attempt {attempt}: HTTP {r.status_code} in {dt:.2f}s, served by {common.served_by(r)}: {common.short(r)}")
    if r.status_code == 200:
        break
    time.sleep(10)
if r.status_code == 200:
    u = r.json()["usage"]
    print("usage:", {k: u[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
    print("served model:", r.headers.get("x-ms-served-model"), "| region:", r.headers.get("x-ms-region"))
