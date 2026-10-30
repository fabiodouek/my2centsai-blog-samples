"""Test 6: rotate, disable and delete a credential under a running sandbox.

LangSmith resolves {WORKSPACE_SECRET} references when a proxy config is applied, so this checks what
a changed secret does on its own, and what re-applying the config (update_sandbox) changes.
"""

import os
import time

from langsmith.sandbox import proxy_config

from common import GITHUB_API_RULE, GITHUB_BEARER_SECRET, GITHUB_GIT_RULE, banner, client, create_sandbox, require_env, run, secrets_api

require_env("GITHUB_TOKEN")
GOOD = f"Bearer {os.environ['GITHUB_TOKEN']}"
REVOKED = "Bearer ghp_revoked000000000000000000000000000"
CHECK = "curl -s -m 10 -o /dev/null -w '%{http_code}' https://api.github.com/user"
RULES = [GITHUB_API_RULE, GITHUB_GIT_RULE]

sbx = create_sandbox("07", rules=RULES)


def set_secret(value: str | None) -> None:
    secrets_api("POST", json=[{"key": GITHUB_BEARER_SECRET, "value": value}])


def reapply(rules=RULES) -> float:
    t0 = time.perf_counter()
    client.update_sandbox(sbx.name, proxy_config=proxy_config(rules=rules))
    return time.perf_counter() - t0


def watch(want: str, limit: float = 60) -> str:
    """Poll until the status is `want` or the limit passes; return what was seen and when."""
    t0 = time.perf_counter()
    seen = []
    while True:
        s = run(sbx, CHECK)
        if not seen or seen[-1][1] != s:
            seen.append((round(time.perf_counter() - t0, 1), s))
        if s == want or time.perf_counter() - t0 > limit:
            changes = " -> ".join(f"{c} at {t}s" for t, c in seen)
            return changes if s == want else f"{changes}, still {s} after {time.perf_counter() - t0:.0f}s"
        time.sleep(1)


try:
    banner("Baseline")
    print(f"GET /user -> {run(sbx, CHECK)}")

    banner("A. Change the workspace secret, don't touch the sandbox")
    set_secret(REVOKED)
    print("watch 60 s for a 401:", watch("401"))

    banner("B. Re-apply the same proxy config (update_sandbox)")
    print(f"update_sandbox took {reapply():.1f}s;", "then:", watch("401"))

    banner("C. Restore the secret and re-apply")
    set_secret(GOOD)
    print(f"update_sandbox took {reapply():.1f}s;", "then:", watch("200"))

    banner("D. Disable the api.github.com rule")
    print(f"update_sandbox took {reapply([{**GITHUB_API_RULE, 'enabled': False}, GITHUB_GIT_RULE]):.1f}s;", "then:", watch("401"))
    print("GH_TOKEN in a new command:", run(sbx, "echo ${GH_TOKEN:-unset}"))
    print(run(sbx, "curl -s https://api.github.com/user | head -3"))
    print(f"re-enable: update_sandbox took {reapply():.1f}s;", "then:", watch("200"))

    banner("E. Delete the workspace secret, then re-apply")
    set_secret(None)
    print("watch 30 s without re-applying:", watch("401", limit=30))
    try:
        print(f"update_sandbox took {reapply():.1f}s;", "then:", watch("401"))
    except Exception as e:
        print("re-apply with a missing secret:", type(e).__name__, str(e)[:200])
        print("sandbox keeps the last applied value:", run(sbx, CHECK))
finally:
    client.delete_sandbox(sbx.name)
    set_secret(GOOD)
    print("\nrestored the GITHUB_BEARER workspace secret")
