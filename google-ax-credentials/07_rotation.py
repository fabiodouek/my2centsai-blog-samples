"""Step 7: rotate and revoke under a running task. The Kubernetes Secret is the
only copy, so changing it should change what the next request carries."""

import base64
import json
import time

from common import (PLACEHOLDER, SECRETS_NS, banner, create_task, inject, kubectl, load_env, redact,
                    set_policy, sh)

NAME = "cred-rotate"
env = load_env()
REPO, TOKEN = env["GITHUB_REPO"], env["GITHUB_TOKEN"]
PROBE = (f"curl -s -o /dev/null -w '%{{http_code}}' https://api.github.com/repos/{REPO} "
         "-H \"Authorization: token $GH_TOKEN\"")


def put_token(value: str) -> None:
    body = {"stringData": {"token": value}}
    kubectl("-n", SECRETS_NS, "patch", "secret", "github", "--type", "merge", "-p", json.dumps(body), quiet=True)


def poll(want: str, limit: int = 120) -> None:
    t0 = time.time()
    while True:
        code = sh(NAME, PROBE)
        if code == want or time.time() - t0 > limit:
            print(f"  HTTP {code} after {time.time() - t0:.1f} s")
            return
        time.sleep(1)


create_task(NAME, {"GH_TOKEN": PLACEHOLDER})
set_policy(NAME, [inject("api.github.com", "Authorization", "github", "token", prefix="Bearer ")])

banner("baseline")
print("  HTTP", sh(NAME, PROBE))

banner("revoke: replace the token in the Secret with a dead one")
put_token("github_pat_revoked")
poll("401")

banner("restore the real token")
put_token(TOKEN)
poll("200")

banner("delete the Secret entirely")
kubectl("-n", SECRETS_NS, "get", "secret", "github", "-o", "json", quiet=True)
saved = kubectl("-n", SECRETS_NS, "get", "secret", "github", "-o", "json", quiet=True).stdout
kubectl("-n", SECRETS_NS, "delete", "secret", "github", quiet=True)
poll("403")
print(redact(sh(NAME, f"curl -s https://api.github.com/repos/{REPO} -H \"Authorization: token $GH_TOKEN\" | head -c 200"), TOKEN))

banner("put it back")
s = json.loads(saved)
kubectl("apply", "-f", "-", quiet=True, input=json.dumps({
    "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "github", "namespace": SECRETS_NS},
    "data": s["data"]}))
poll("200")
