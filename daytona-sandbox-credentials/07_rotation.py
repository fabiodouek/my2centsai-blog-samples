"""Test 6: revoke, rotate, detach and delete a credential under a running sandbox.

Each step polls GET https://api.github.com/user once a second from inside the sandbox and records
how long the change takes to reach the proxy. The placeholder captured at the start is used for
every request, so detaching or deleting the secret can't hide behind a changed env var.
"""

import os
import time

from daytona import CreateSecretParams, UpdateSecretParams

from common import (DEMO_USER, GITHUB_BEARER_SECRET, GITHUB_ENV, SECRET_HOSTS, banner, create_sandbox, daytona,
                    find_secret, require_env, run, secret_name)

require_env("GITHUB_TOKEN")
REVOKED = "ghp_revoked000000000000000000000000000"

sbx = create_sandbox("07", secrets=GITHUB_ENV)
placeholder = run(sbx, "echo $GH_TOKEN")
CHECK = f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' -H 'Authorization: Bearer {placeholder}' https://api.github.com/user"


def wait_for(want: str, limit: float = 120) -> str:
    """Poll until the status is `want`; return the time and every status seen on the way."""
    t0 = time.perf_counter()
    seen = []
    while True:
        s = run(sbx, CHECK)
        if not seen or seen[-1] != s:
            seen.append(s)
        if s == want or time.perf_counter() - t0 > limit:
            return f"{s} after {time.perf_counter() - t0:5.1f}s  (seen: {' -> '.join(seen)})"
        time.sleep(1)


NAME = secret_name(DEMO_USER, GITHUB_BEARER_SECRET)
secret = find_secret(NAME)
try:
    banner("Baseline")
    print(f"GH_TOKEN={placeholder}  GET /user -> {run(sbx, CHECK)}")

    banner("Swap the value for a revoked token, then back, three times")
    for i in range(1, 4):
        daytona.secret.update(secret.id, UpdateSecretParams(value=REVOKED))
        print(f"round {i} revoke:  {wait_for('401')}")
        daytona.secret.update(secret.id, UpdateSecretParams(value=os.environ["GITHUB_TOKEN"]))
        print(f"round {i} restore: {wait_for('200')}")
        print(f"placeholder unchanged: {run(sbx, 'echo $GH_TOKEN') == placeholder}")

    banner("Detach GH_TOKEN from the running sandbox (update_secrets)")
    sbx.update_secrets({k: secret_name(DEMO_USER, v) for k, v in GITHUB_ENV.items() if k != "GH_TOKEN"})
    print(f"detach:   {wait_for('401')}")
    print(f"GH_TOKEN in a new process: {run(sbx, 'echo ${GH_TOKEN:-unset}')}")
    sbx.update_secrets({k: secret_name(DEMO_USER, v) for k, v in GITHUB_ENV.items()})
    print(f"reattach: {wait_for('200')}")

    banner("Delete the secret")
    daytona.secret.delete(secret.id)
    print(f"delete:   {wait_for('401')}")
    print(run(sbx, CHECK.replace("-o /dev/null -w '%{http_code}'", "-w ' [%{http_code}]'")))
finally:
    sbx.delete()
    if not find_secret(NAME):
        s = daytona.secret.create(CreateSecretParams(
            name=NAME, value=os.environ["GITHUB_TOKEN"], hosts=SECRET_HOSTS[GITHUB_BEARER_SECRET],
            description=f"my2cents demo, user {DEMO_USER}"))
        print(f"\nrecreated {s.name} for the remaining tests (new placeholder {s.placeholder})")
