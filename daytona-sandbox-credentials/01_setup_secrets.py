"""Step 1: store one user's Anthropic key, Bedrock API key and GitHub token as Daytona Secrets.

This is the only script that reads real credentials. They belong to DEMO_USER (default alice), so each
secret is named <user>-<credential>, and bound to the hosts its value may be sent to. Re-running updates the values in place (the placeholder stays the same), which is also
how you rotate. The Bedrock key is a short-term one, minted here from your AWS profile; it expires
after 12 hours, so re-run this script before a later session.
"""

import base64
import os

from aws_bedrock_token_generator import provide_token
from daytona import CreateSecretParams, UpdateSecretParams

from common import (
    ANTHROPIC_SECRET,
    BEDROCK_SECRET,
    GITHUB_BASIC_SECRET,
    GITHUB_BEARER_SECRET,
    SECRET_HOSTS,
    DEMO_USER,
    banner,
    daytona,
    find_secret,
    require_env,
    secret_name,
)

require_env("ANTHROPIC_API_KEY", "GITHUB_TOKEN")

github_token = os.environ["GITHUB_TOKEN"]
values = {
    ANTHROPIC_SECRET: os.environ["ANTHROPIC_API_KEY"],
    BEDROCK_SECRET: provide_token(region=os.environ.get("AWS_REGION", "us-east-1")),
    GITHUB_BEARER_SECRET: github_token,
    GITHUB_BASIC_SECRET: base64.b64encode(f"x-access-token:{github_token}".encode()).decode(),
}

banner(f"Create or update Daytona Secrets for user {DEMO_USER}")
for credential, value in values.items():
    name, hosts = secret_name(DEMO_USER, credential), SECRET_HOSTS[credential]
    if existing := find_secret(name):
        s = daytona.secret.update(existing.id, UpdateSecretParams(value=value, hosts=hosts))
        action = "updated"
    else:
        s = daytona.secret.create(CreateSecretParams(name=name, value=value, hosts=hosts, description=f"my2cents demo, user {DEMO_USER}"))
        action = "created"
    print(f"{action:8} {s.name:24} {s.placeholder}  -> {', '.join(s.hosts)}")

banner("What the API gives back (values are write-only)")
for s in daytona.secret.list(name=f"{DEMO_USER}-").items:
    print(s.model_dump(include={"name", "placeholder", "hosts", "updated_at"}))
