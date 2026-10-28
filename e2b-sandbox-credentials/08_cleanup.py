"""Clean up: kill demo sandboxes, delete the E2B Secrets, delete demo branches and close their PRs."""

import json
import os
import urllib.request

from e2b import Sandbox, SandboxQuery, Secret

from common import ANTHROPIC_SECRET, DEMO_TAG, GITHUB_BASIC_SECRET, GITHUB_BEARER_SECRET, banner

banner("Sandboxes")
paginator = Sandbox.list(query=SandboxQuery(metadata=DEMO_TAG))
while paginator.has_next:
    for s in paginator.next_items():
        Sandbox.kill(s.sandbox_id)
        print(f"killed {s.sandbox_id}")

banner("Secrets")
for name in (ANTHROPIC_SECRET, GITHUB_BEARER_SECRET, GITHUB_BASIC_SECRET, "mechanics-dummy"):
    if Secret.exists(name):
        Secret.destroy(name)
        print(f"destroyed {name}")

banner("GitHub branches and PRs from the demo")
token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
if not (token and repo):
    print("GITHUB_TOKEN / GITHUB_REPO not set, skipping")
else:

    def gh(method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            method=method,
            data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or "null")

    for pr in gh("GET", f"/repos/{repo}/pulls?state=open&per_page=100"):
        if pr["head"]["ref"].startswith("e2b-demo-"):
            gh("PATCH", f"/repos/{repo}/pulls/{pr['number']}", {"state": "closed"})
            print(f"closed PR #{pr['number']}")
    for ref in gh("GET", f"/repos/{repo}/git/matching-refs/heads/e2b-demo-"):
        gh("DELETE", f"/repos/{repo}/git/{ref['ref']}")
        print(f"deleted {ref['ref']}")
