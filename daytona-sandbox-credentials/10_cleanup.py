"""Clean up: delete demo sandboxes (before the secrets they use), every user's Daytona Secrets, the snapshot,
the S3 test bucket and the saved GitHub App tokens, and close demo PRs and delete their branches."""

import json
import os
import urllib.request
from pathlib import Path

import boto3
from daytona import ListSandboxesQuery

from common import DEMO_LABELS, SECRET_HOSTS, SNAPSHOT, banner, daytona

# Per-user secrets are <user>-<credential>, so match on the credential suffix.
DEMO_SECRET_SUFFIXES = tuple(f"-{c}" for c in SECRET_HOSTS)
DEMO_SECRETS = ["aws-secret-access-key", "aws-session-token"]
DEMO_SECRET_PREFIXES = ("mech-",)

banner("Sandboxes")
for s in daytona.list(ListSandboxesQuery(labels=DEMO_LABELS)).items:
    s.delete()
    print(f"deleted {s.name}")

banner("Secrets")
for s in daytona.secret.list().items:
    if s.name in DEMO_SECRETS or s.name.startswith(DEMO_SECRET_PREFIXES) or s.name.endswith(DEMO_SECRET_SUFFIXES):
        daytona.secret.delete(s.id)
        print(f"deleted {s.name}")

banner("Saved GitHub App tokens")
tokens = Path(".github-app-tokens.json")
if tokens.exists():
    tokens.unlink()
    print(f"deleted {tokens} (revoke the app's access at github.com/settings/applications if you're done)")

banner("Snapshot")
for snap in daytona.snapshot.list().items:
    if snap.name == SNAPSHOT:
        daytona.snapshot.delete(snap)
        print(f"deleted {snap.name}")

banner("S3 test bucket")
bucket = os.environ.get("AWS_TEST_BUCKET")
if not bucket:
    print("AWS_TEST_BUCKET not set, skipping")
else:
    b = boto3.resource("s3", region_name=os.environ.get("AWS_REGION", "us-east-1")).Bucket(bucket)
    try:
        b.objects.all().delete()
        b.delete()
        print(f"deleted {bucket}")
    except b.meta.client.exceptions.NoSuchBucket:
        print(f"{bucket} already gone")

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
        if pr["head"]["ref"].startswith("daytona-demo-"):
            gh("PATCH", f"/repos/{repo}/pulls/{pr['number']}", {"state": "closed"})
            print(f"closed PR #{pr['number']}")
    for ref in gh("GET", f"/repos/{repo}/git/matching-refs/heads/daytona-demo-"):
        gh("DELETE", f"/repos/{repo}/git/{ref['ref']}")
        print(f"deleted {ref['ref']}")
