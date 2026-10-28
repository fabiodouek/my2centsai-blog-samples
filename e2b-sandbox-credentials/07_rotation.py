"""Test 7: revoke and rotate a credential under a running sandbox, without touching the sandbox."""

import os
import time

from e2b import Secret

from common import GITHUB_BEARER_SECRET, banner, create_sandbox, require_env, run

require_env("GITHUB_TOKEN")
CHECK = "curl -s -o /dev/null -w '%{http_code}' https://api.github.com/user"


def status(sbx) -> str:
    return run(sbx, CHECK)


def wait_for(sbx, want: str, limit: float = 30) -> float:
    t0 = time.perf_counter()
    while (s := status(sbx)) != want:
        if time.perf_counter() - t0 > limit:
            raise SystemExit(f"still {s} after {limit}s")
        time.sleep(0.5)
    return time.perf_counter() - t0


sbx = create_sandbox("07", anthropic=False)
try:
    banner("Baseline")
    print(f"GET /user -> {status(sbx)}")

    banner("Swap the secret for a revoked value")
    Secret.update(GITHUB_BEARER_SECRET, "ghp_revoked000000000000000000000000000")
    print(f"GET /user -> 401 after {wait_for(sbx, '401'):.1f}s")

    banner("Rotate back to a valid token")
    Secret.update(GITHUB_BEARER_SECRET, os.environ["GITHUB_TOKEN"])
    print(f"GET /user -> 200 after {wait_for(sbx, '200'):.1f}s")

    banner("Delete the secret")
    Secret.destroy(GITHUB_BEARER_SECRET)
    print(f"GET /user -> 401 after {wait_for(sbx, '401'):.1f}s  (request still sent, without the header)")
    print(run(sbx, "curl -s https://api.github.com/user | head -3"))
finally:
    sbx.kill()
    if not Secret.exists(GITHUB_BEARER_SECRET):
        Secret.create(GITHUB_BEARER_SECRET, os.environ["GITHUB_TOKEN"], metadata={"demo": "my2cents-credentials"})
        print("\nrecreated the secret for the remaining tests")
