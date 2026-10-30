"""Clean up: delete demo sandboxes, the snapshot and the workspace secrets; close demo PRs and delete
their branches. The AWS role, OIDC provider and bucket are left to the commands in the README."""

import json
import os
import urllib.request

from common import ANTHROPIC_SECRET, GITHUB_BASIC_SECRET, GITHUB_BEARER_SECRET, PREFIX, SNAPSHOT, banner, client, secrets_api

banner("Sandboxes")
for sb in client.list_sandboxes():
    if sb.name.startswith(PREFIX):
        client.delete_sandbox(sb.name)
        print(f"deleted {sb.name}")

banner("Snapshot")
for snap in client.list_snapshots(name_contains=SNAPSHOT):
    if snap.name == SNAPSHOT:
        client.delete_snapshot(snap.id)
        print(f"deleted {snap.name} ({snap.id})")

banner("Workspace secrets")
existing = {s["key"] for s in secrets_api("GET")}
for key in (ANTHROPIC_SECRET, GITHUB_BEARER_SECRET, GITHUB_BASIC_SECRET, "MECH_BARE_TOKEN"):
    if key in existing:
        secrets_api("DELETE", f"/{key}")
        print(f"deleted {key}")

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
        if pr["head"]["ref"].startswith("langsmith-demo-"):
            gh("PATCH", f"/repos/{repo}/pulls/{pr['number']}", {"state": "closed"})
            print(f"closed PR #{pr['number']}")
    for ref in gh("GET", f"/repos/{repo}/git/matching-refs/heads/langsmith-demo-"):
        gh("DELETE", f"/repos/{repo}/git/{ref['ref']}")
        print(f"deleted {ref['ref']}")
