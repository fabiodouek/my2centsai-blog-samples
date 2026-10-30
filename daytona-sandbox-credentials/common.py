"""Shared helpers: env loading, secret names, a labelled sandbox factory and a command runner."""

import json
import os
import shlex

from daytona import CreateSandboxFromSnapshotParams, Daytona, Sandbox
from dotenv import load_dotenv

load_dotenv()

# Every sandbox created by these scripts carries this label. 09_cleanup.py deletes by it.
DEMO_LABELS = {"demo": "my2cents-credentials"}
# Safety net: anything a crashed script leaves behind is destroyed after this many minutes.
SAFETY_TTL = 60
# Built by snapshot/build.py: the default image plus pinned Claude Code and gh.
SNAPSHOT = "my2cents-claude-creds"

# Each end user has their own Daytona Secrets. The secret store is organization-wide with no per-user
# scope, so the mapping is the name: <user>-<credential>, e.g. alice-github-token. A sandbox only gets
# the secrets of the user it runs for. DEMO_USER is the user the tests run as (01_setup_secrets.py
# stores that user's credentials; 08_github_app_user.py adds a second user with a GitHub App token).
DEMO_USER = os.environ.get("DEMO_USER", "alice")

# Credentials every user can have, and the hosts each real value may be sent to.
ANTHROPIC_SECRET = "anthropic-api-key"
BEDROCK_SECRET = "bedrock-api-key"
GITHUB_BEARER_SECRET = "github-token"
GITHUB_BASIC_SECRET = "github-basic"  # base64("x-access-token:<token>"), for git over HTTPS
SECRET_HOSTS = {
    ANTHROPIC_SECRET: ["api.anthropic.com"],
    BEDROCK_SECRET: ["bedrock-runtime.us-east-1.amazonaws.com", "bedrock.us-east-1.amazonaws.com"],
    GITHUB_BEARER_SECRET: ["api.github.com"],
    GITHUB_BASIC_SECRET: ["github.com"],
}

# Env var in the sandbox -> credential it is bound to. The sandbox sees a dtn_secret_... placeholder.
ANTHROPIC_ENV = {"ANTHROPIC_API_KEY": ANTHROPIC_SECRET}
# The preinstalled Claude Code defaults to an older model; pin the same one on both paths.
ANTHROPIC_MODEL = {"ANTHROPIC_MODEL": "claude-sonnet-4-6"}
BEDROCK_ENV = {"AWS_BEARER_TOKEN_BEDROCK": BEDROCK_SECRET}
GITHUB_ENV = {"GH_TOKEN": GITHUB_BEARER_SECRET, "GH_BASIC": GITHUB_BASIC_SECRET}

daytona = Daytona()


def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} in .env first (see .env.example).")


def secret_name(user: str, credential: str) -> str:
    """The Daytona Secret holding one user's credential."""
    return f"{user}-{credential}"


def find_secret(name: str):
    """Return the org secret with exactly this name, or None (list() matches partial names)."""
    return next((s for s in daytona.secret.list(name=name).items if s.name == name), None)


def bedrock_env() -> dict:
    """Plain env vars that switch Claude Code to Bedrock. The key itself comes from BEDROCK_ENV."""
    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    return {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        "ANTHROPIC_MODEL": model,
        # Claude Code also reaches for an Opus and a Haiku model; keep both on models the account has.
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }


def create_sandbox(test: str, secrets: dict | None = None, env_vars: dict | None = None,
                   user: str | None = DEMO_USER) -> Sandbox:
    """A sandbox for one user: `secrets` maps env vars to credentials, resolved to that user's secrets.
    With user=None the names in `secrets` are used as they are (Test 1's throwaway secrets)."""
    mapped = {env: secret_name(user, cred) if user else cred for env, cred in (secrets or {}).items()}
    return daytona.create(
        CreateSandboxFromSnapshotParams(
            snapshot=SNAPSHOT,
            labels={**DEMO_LABELS, "test": test, **({"userId": user} if user else {})},
            secrets=mapped,
            env_vars=env_vars or {},
            ttl_minutes=SAFETY_TTL,
            auto_stop_interval=0,  # auto-stop ignores running processes; agent tasks run for minutes
        )
    )


def run(sbx: Sandbox, cmd: str, timeout: int = 120) -> str:
    """Run a shell command and return stdout+stderr (Daytona merges them), never raising on exit codes."""
    r = sbx.process.exec(cmd, timeout=timeout)
    return r.result.strip()


def configure_git(sbx: Sandbox) -> None:
    """git speaks Basic auth. Send the placeholder in a header; the proxy swaps in the real pair."""
    run(sbx, 'git config --global http.https://github.com/.extraHeader "Authorization: Basic $GH_BASIC"')
    run(sbx, 'git config --global user.name "my2cents demo" && git config --global user.email demo@my2cents.ai')


def claude(sbx: Sandbox, prompt: str, timeout: int = 600, flags: str = "") -> dict:
    """Run Claude Code headless and return its JSON result."""
    # exec keeps stdin open and claude -p waits on it, so close it explicitly.
    out = run(sbx, f"claude -p {shlex.quote(prompt)} --output-format json {flags} < /dev/null", timeout=timeout)
    lines = [line for line in out.splitlines() if line.startswith("{")]
    if not lines:
        raise SystemExit(f"no JSON result from claude:\n{out[-2000:]}")
    return json.loads(lines[-1])
