"""Delete the demo tasks, close the demo pull requests, delete their branches,
and remove the Secrets and the namespace grant."""

import json
import urllib.request

from common import ATESPACE, SECRETS_NS, ax, banner, kubectl, load_env, stop_tunnel

env = load_env()
REPO, TOKEN = env["GITHUB_REPO"], env["GITHUB_TOKEN"]


def github(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"https://api.github.com{path}", method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"null")


banner(f"tasks in atespace '{ATESPACE}' (their egress policies go with them)")
for line in ax("get", "tasks", quiet=True).stdout.splitlines()[1:]:
    ax("delete", "task", line.split()[0], check=False)

banner("demo pull requests and branches")
for pr in github("GET", f"/repos/{REPO}/pulls?state=open&per_page=100"):
    if pr["head"]["ref"].startswith("ax-agent-"):
        github("PATCH", f"/repos/{REPO}/pulls/{pr['number']}", {"state": "closed"})
        print(f"  closed #{pr['number']}")
for ref in github("GET", f"/repos/{REPO}/git/matching-refs/heads/ax-"):
    github("DELETE", f"/repos/{REPO}/git/{ref['ref']}")
    print(f"  deleted {ref['ref']}")

banner("secrets and the namespace grant")
kubectl("delete", "namespace", SECRETS_NS, "--ignore-not-found")
empty = kubectl("-n", "ate-system", "create", "configmap", "k8s-credential-provider-namespace-policy",
                "--from-literal=namespace-policy.yaml=policies: []\n", "--dry-run=client", "-o", "yaml",
                quiet=True).stdout
kubectl("apply", "-f", "-", input=empty)
kubectl("-n", "ate-system", "rollout", "restart", "deployment/k8s-credential-provider")
stop_tunnel()
