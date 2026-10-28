"""Shared helpers: env loading, secret names, the locked-down network config, and a sandbox factory."""

import json
import os
import shlex

from dotenv import load_dotenv
from e2b import Sandbox, Secret

load_dotenv()

# Every sandbox created by these scripts carries this tag. 08_cleanup.py kills by it.
DEMO_TAG = {"demo": "my2cents-credentials"}
TEMPLATE = "my2cents-claude-creds"

# Names of the E2B Secrets that 01_setup_secrets.py creates.
ANTHROPIC_SECRET = "anthropic-api-key"
GITHUB_BEARER_SECRET = "github-token"
GITHUB_BASIC_SECRET = "github-basic"  # base64("x-access-token:<token>"), for git over HTTPS

# What the sandbox sees instead of real credentials. The tools need *something* set to start;
# the egress proxy overwrites the header these values end up in.
PLACEHOLDER_ENVS = {
    "ANTHROPIC_API_KEY": "sk-ant-placeholder-not-a-real-key",
    "GH_TOKEN": "placeholder-not-a-real-token",
}


def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def network(anthropic: bool = True, github: bool = True, extra_allow: tuple[str, ...] = ()) -> dict:
    """Allow only the hosts we inject credentials for (plus extra_allow), deny everything else."""
    rules: dict = {}
    if anthropic:
        rules["api.anthropic.com"] = [
            {"transform": {"headers": {"x-api-key": Secret.fill(ANTHROPIC_SECRET)}}},
        ]
    if github:
        # git (smart HTTP) only speaks Basic auth; the proxy cannot base64-encode, so the
        # secret holds the already-encoded "x-access-token:<token>" pair.
        rules["github.com"] = [
            {"transform": {"headers": {"Authorization": f"Basic {Secret.fill(GITHUB_BASIC_SECRET)}"}}},
        ]
        rules["api.github.com"] = [
            {"transform": {"headers": {"Authorization": f"Bearer {Secret.fill(GITHUB_BEARER_SECRET)}"}}},
        ]
    return {
        "allow_out": lambda ctx: list(ctx.rules.keys()) + list(extra_allow),
        "deny_out": lambda ctx: [ctx.all_traffic],
        "rules": rules,
    }


def create_sandbox(test: str, timeout: int = 600, **network_kwargs) -> Sandbox:
    return Sandbox.create(
        TEMPLATE,
        timeout=timeout,
        envs=PLACEHOLDER_ENVS,
        network=network(**network_kwargs),
        metadata={**DEMO_TAG, "test": test},
    )


def run(sbx: Sandbox, cmd: str, timeout: int = 120, **kwargs) -> str:
    """Run a shell command and return stdout+stderr, without raising on a non-zero exit."""
    try:
        r = sbx.commands.run(cmd, timeout=timeout, **kwargs)
    except Exception as e:  # CommandExitException carries the same fields
        r = e
    return (getattr(r, "stdout", "") + getattr(r, "stderr", "")).strip()


def claude(sbx: Sandbox, prompt: str, timeout: int = 600, flags: str = "") -> dict:
    """Run Claude Code headless and return its JSON result."""
    out = run(sbx, f"claude -p {shlex.quote(prompt)} --output-format json {flags}", timeout=timeout)
    lines = [line for line in out.splitlines() if line.startswith("{")]
    if not lines:
        raise SystemExit(f"no JSON result from claude:\n{out[-2000:]}")
    return json.loads(lines[-1])


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} in .env first (see .env.example).")
