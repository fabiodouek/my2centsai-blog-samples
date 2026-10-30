"""Test 7: per-user GitHub credentials, with a GitHub App user token (OAuth device flow) for a second user.

alice's GitHub secrets hold a personal access token (01_setup_secrets.py). bob signs in through a GitHub
App with the device flow: he enters a one-time code at github.com/login/device, and this script stores his
ghu_ user token as bob's secrets. Each user's sandbox gets only that user's secrets.

GitHub App user tokens expire after 8 hours. The refresh token stays outside the sandbox (here in a local
file; in a real service, your backend), and updating bob's secrets moves his running sandbox to the new one.

Needs GITHUB_APP_CLIENT_ID: a GitHub App with Device Flow enabled, installed on GITHUB_REPO (see README).
"""

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from daytona import CreateSecretParams, UpdateSecretParams

from common import (DEMO_USER, GITHUB_BASIC_SECRET, GITHUB_BEARER_SECRET, GITHUB_ENV, SECRET_HOSTS, banner,
                    configure_git, create_sandbox, daytona, find_secret, require_env, run, secret_name)

require_env("GITHUB_APP_CLIENT_ID", "GITHUB_REPO")
CLIENT_ID, REPO = os.environ["GITHUB_APP_CLIENT_ID"], os.environ["GITHUB_REPO"]
APP_USER = "bob"
TOKENS = Path(".github-app-tokens.json")  # bob's tokens; gitignored, mode 600
BRANCH = f"daytona-demo-app-{int(time.time())}"


def post(url: str, **form) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def device_login() -> dict:
    """OAuth device flow: GitHub shows bob a code to enter; no client secret is involved."""
    code = post("https://github.com/login/device/code", client_id=CLIENT_ID)
    print(f"{APP_USER}: open {code['verification_uri']} and enter {code['user_code']} "
          f"(valid for {code['expires_in'] // 60} minutes)", flush=True)
    interval = code["interval"]
    while True:
        time.sleep(interval)
        t = post("https://github.com/login/oauth/access_token", client_id=CLIENT_ID, device_code=code["device_code"],
                 grant_type="urn:ietf:params:oauth:grant-type:device_code")
        if "access_token" in t:
            return t
        if t.get("error") == "slow_down":
            interval += 5
        elif t.get("error") != "authorization_pending":
            raise SystemExit(f"device flow failed: {t.get('error')}: {t.get('error_description')}")


def refresh(tokens: dict) -> dict:
    """Trade the refresh token for a new user token (and a new refresh token). Device-flow tokens need no secret."""
    t = post("https://github.com/login/oauth/access_token", client_id=CLIENT_ID, grant_type="refresh_token",
             refresh_token=tokens["refresh_token"])
    if "access_token" not in t:
        raise SystemExit(f"refresh failed: {t.get('error')}: {t.get('error_description')}")
    return t


def save(tokens: dict) -> None:
    TOKENS.write_text(json.dumps({**tokens, "obtained_at": int(time.time())}))
    TOKENS.chmod(0o600)


def describe(t: dict) -> str:
    return (f"access token {t['access_token'][:4]}... expires in {t['expires_in'] // 3600} h, "
            f"refresh token {t['refresh_token'][:4]}... expires in {t['refresh_token_expires_in'] // 86400} days")


def store(user: str, token: str) -> None:
    """Create or update one user's two GitHub secrets: Bearer for the API, Basic for git."""
    values = {GITHUB_BEARER_SECRET: token,
              GITHUB_BASIC_SECRET: base64.b64encode(f"x-access-token:{token}".encode()).decode()}
    for credential, value in values.items():
        name, hosts = secret_name(user, credential), SECRET_HOSTS[credential]
        if s := find_secret(name):
            daytona.secret.update(s.id, UpdateSecretParams(value=value, hosts=hosts))
        else:
            daytona.secret.create(CreateSecretParams(name=name, value=value, hosts=hosts,
                                                     description=f"my2cents demo, user {user}"))


def expiry(sbx) -> str:
    """The expiry GitHub reports for the token that authenticated the request (none for a PAT without one)."""
    return run(sbx, "curl -s -D - -o /dev/null -H \"Authorization: Bearer $GH_TOKEN\" https://api.github.com/user"
                    " | grep -i '^github-authentication-token-expiration' | cut -d' ' -f2- | tr -d '\\r'") or "none"


def github_view(sbx) -> None:
    """Who GitHub thinks the sandbox is, and what its credential can reach."""
    print("  login        :", run(sbx, "gh api user --jq .login"))
    print("  installations:", run(sbx, "gh api user/installations --jq '[.installations[].app_slug] | join(\",\")' 2>&1 | tail -1"))
    print("  repos visible:", run(sbx, "gh api 'user/repos?per_page=100' --jq length 2>&1 | tail -1"))
    print("  token expiry :", expiry(sbx))


banner(f"A. {APP_USER} signs in to the GitHub App (device flow)")
tokens = json.loads(TOKENS.read_text()) if TOKENS.exists() else None
if tokens and time.time() < tokens["obtained_at"] + tokens["expires_in"] - 600:
    print(f"reusing {APP_USER}'s saved token")
else:
    tokens = device_login()
    save(tokens)
print(describe(tokens))
store(APP_USER, tokens["access_token"])

banner("B. One set of secrets per user")
for s in sorted(daytona.secret.list().items, key=lambda s: s.name):
    if s.name.split("-", 1)[0] in (DEMO_USER, APP_USER) and "github" in s.name:
        print(f"  {s.name:22} {s.placeholder}  -> {', '.join(s.hosts)}")

alice = create_sandbox("08", secrets=GITHUB_ENV, user=DEMO_USER)
bob = create_sandbox("08", secrets=GITHUB_ENV, user=APP_USER)
try:
    banner("C. Each user's sandbox, each with its own credential")
    for user, sbx in ((DEMO_USER, alice), (APP_USER, bob)):
        print(f"{user}: labels {sbx.labels.get('userId')}, GH_TOKEN={run(sbx, 'echo $GH_TOKEN')}")
        github_view(sbx)

    banner(f"D. {APP_USER}'s sandbox tries {DEMO_USER}'s placeholder")
    other = run(alice, "echo $GH_TOKEN")
    print(run(bob, f"curl -s -m 15 -w ' [%{{http_code}}]' -H 'Authorization: Bearer {other}' https://api.github.com/user | tr -d '\\r' | tr -s ' \\n' ' ' | cut -c1-160"))

    banner(f"E. {APP_USER} pushes a branch and opens a PR with the GitHub App token")
    configure_git(bob)
    print(run(bob, f"git clone -q https://github.com/{REPO}.git repo && cd repo && git checkout -q -b {BRANCH}"
                   f" && date -u > daytona-app-demo.txt && git add daytona-app-demo.txt"
                   f" && git commit -qm 'per-user GitHub App token demo' && git push -q origin {BRANCH} 2>&1"
                   f" && gh pr create --repo {REPO} --head {BRANCH} --title 'Per-user GitHub App token demo'"
                   f" --body 'Opened from a Daytona sandbox holding only a placeholder for bob' 2>&1 | tail -1", timeout=180))
    pr = run(bob, f"gh pr view {BRANCH} --repo {REPO} --json author,number --jq '\"#\" + (.number|tostring) + \" by \" + .author.login'")
    print(f"  PR {pr}")

    banner(f"F. Refresh {APP_USER}'s token and update his secrets under the running sandbox")
    # A refresh revokes the old user token. Once the sandbox sees that, only the new token can
    # bring it back to 200, so the recovery time is how long the secret update takes to land.
    placeholder = run(bob, "echo $GH_TOKEN")
    check = "curl -s -o /dev/null -w '%{http_code}' -H \"Authorization: Bearer $GH_TOKEN\" https://api.github.com/user"
    tokens = refresh(tokens)
    save(tokens)
    print(describe(tokens))
    t0 = time.perf_counter()
    while (status := run(bob, check)) != "401" and time.perf_counter() - t0 < 120:
        time.sleep(1)
    print(f"  old token revoked: the sandbox gets {status} after {time.perf_counter() - t0:.1f}s")
    t0 = time.perf_counter()
    store(APP_USER, tokens["access_token"])
    while (status := run(bob, check)) != "200" and time.perf_counter() - t0 < 120:
        time.sleep(1)
    print(f"  secrets updated: back to {status} after {time.perf_counter() - t0:.1f}s, "
          f"placeholder unchanged: {run(bob, 'echo $GH_TOKEN') == placeholder}")
finally:
    alice.delete()
    bob.delete()
