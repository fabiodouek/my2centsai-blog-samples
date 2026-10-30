"""Step 1: store the Anthropic key and the GitHub token as LangSmith workspace secrets.

This is the only script that reads real credentials. Proxy rules reference them as {NAME}; LangSmith
resolves the reference when a sandbox's proxy config is applied, so the value never enters the VM.
Re-running updates the values; running sandboxes only pick up the change when their proxy config is
re-applied (see 07_rotation.py).
"""

import base64
import os

from common import ANTHROPIC_SECRET, GITHUB_BASIC_SECRET, GITHUB_BEARER_SECRET, banner, require_env, secrets_api

require_env("ANTHROPIC_API_KEY", "GITHUB_TOKEN")

token = os.environ["GITHUB_TOKEN"]
values = {
    ANTHROPIC_SECRET: os.environ["ANTHROPIC_API_KEY"],
    GITHUB_BEARER_SECRET: f"Bearer {token}",
    GITHUB_BASIC_SECRET: "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode(),
}

banner("Create or update workspace secrets")
secrets_api("POST", json=[{"key": k, "value": v} for k, v in values.items()])

banner("What the API gives back (values are write-only)")
for s in secrets_api("GET"):
    print(s)
