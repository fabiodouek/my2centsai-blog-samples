"""Step 1: store the Anthropic key and the GitHub token as E2B Secrets.

This is the only place the real values are read. After this, sandboxes reference them by name.
Re-running updates the values in place, which is also how you rotate.
"""

import base64
import os

from e2b import Secret

from common import ANTHROPIC_SECRET, DEMO_TAG, GITHUB_BASIC_SECRET, GITHUB_BEARER_SECRET, banner, require_env

require_env("ANTHROPIC_API_KEY", "GITHUB_TOKEN")

github_token = os.environ["GITHUB_TOKEN"]
values = {
    ANTHROPIC_SECRET: os.environ["ANTHROPIC_API_KEY"],
    GITHUB_BEARER_SECRET: github_token,
    GITHUB_BASIC_SECRET: base64.b64encode(f"x-access-token:{github_token}".encode()).decode(),
}

banner("Create or update E2B Secrets")
for name, value in values.items():
    if Secret.exists(name):
        info = Secret.update(name, value, metadata=DEMO_TAG)
        action = "updated"
    else:
        info = Secret.create(name, value, metadata=DEMO_TAG)
        action = "created"
    print(f"{action:8} {info.name:18} version {info.version}  id {info.secret_id}")

banner("What the API gives back (values are write-only)")
paginator = Secret.list()
while paginator.has_next:
    for s in paginator.next_items():
        print(s)

banner("What a sandbox config references")
for name in values:
    print(f"{name:18} -> {Secret.fill(name)}")
