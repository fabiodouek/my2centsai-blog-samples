"""Shared helpers: env loading, workspace secrets, the proxy rules, a named sandbox factory and a runner."""

import json
import os
import shlex
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

from langsmith.sandbox import Sandbox, SandboxClient, proxy_config, workspace_secret  # noqa: E402

# Every sandbox is named my2cents-lscreds-<test>-<ts>. 09_cleanup.py deletes by the prefix.
PREFIX = "my2cents-lscreds"
SNAPSHOT = "my2cents-claude-creds"
GiB = 1024**3
# Safety net: an abandoned sandbox stops after 10 idle minutes and is deleted 10 minutes later.
SAFETY_IDLE_TTL = 600
SAFETY_DELETE_AFTER_STOP = 600

BASE = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")
client = SandboxClient()
_http = httpx.Client(headers={"x-api-key": os.environ["LANGSMITH_API_KEY"]}, timeout=60)

# Workspace secrets created by 01_setup_secrets.py. The proxy resolves {NAME} when the config is applied.
ANTHROPIC_SECRET = "ANTHROPIC_API_KEY"
GITHUB_BEARER_SECRET = "GITHUB_BEARER"  # "Bearer <token>"
GITHUB_BASIC_SECRET = "GITHUB_BASIC"  # "Basic base64(x-access-token:<token>)"

ANTHROPIC_RULE = {
    "name": "anthropic",
    "match_hosts": ["api.anthropic.com"],
    "headers": [{"name": "x-api-key", **workspace_secret(ANTHROPIC_SECRET)}],
    # Claude Code won't start without a key in env; the proxy overwrites the header it sends.
    "env_vars": {"ANTHROPIC_API_KEY": "sk-ant-placeholder-not-a-real-key", "ANTHROPIC_MODEL": "claude-sonnet-4-6"},
}
GITHUB_API_RULE = {
    "name": "github-api",
    "match_hosts": ["api.github.com"],
    "headers": [{"name": "Authorization", **workspace_secret(GITHUB_BEARER_SECRET)}],
    "env_vars": {"GH_TOKEN": "proxy-injected"},
}
GITHUB_GIT_RULE = {
    "name": "github-git",
    "match_hosts": ["github.com"],
    "headers": [{"name": "Authorization", **workspace_secret(GITHUB_BASIC_SECRET)}],
}


def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} in .env first (see .env.example).")


def secrets_api(method: str, path: str = "", **kw):
    r = _http.request(method, f"{BASE}/api/v1/workspaces/current/secrets{path}", **kw)
    if r.is_error:
        raise RuntimeError(f"HTTP {r.status_code} {method} secrets{path}: {r.text.strip()[:300]}")
    return r.json() if r.content else None


def box(sb_name: str) -> dict:
    """The sandbox as the REST API stores it (the SDK model leaves out proxy_config)."""
    r = _http.get(f"{BASE}/api/v2/sandboxes/boxes/{sb_name}")
    r.raise_for_status()
    return r.json()


def name(test: str) -> str:
    return f"{PREFIX}-{test}-{int(time.time() * 1000) % 10**8}"


def snapshot_id(snapshot: str = SNAPSHOT) -> str:
    """Look the snapshot up by name. Dockerfile builds are tagged with a build hash, not "latest",
    so create_sandbox(snapshot_name=...) can't find them."""
    ready = [s for s in client.list_snapshots(name_contains=snapshot) if s.name == snapshot and s.status == "ready"]
    if not ready:
        raise SystemExit(f"snapshot {snapshot} not found; run python snapshot/build.py first")
    return ready[0].id


def create_sandbox(test: str, rules: list | None = None, **kw) -> Sandbox:
    if "snapshot_id" not in kw and "snapshot_name" not in kw:
        kw["snapshot_id"] = snapshot_id()
    return client.create_sandbox(
        name=name(test),
        proxy_config=proxy_config(rules=rules or []),
        idle_ttl_seconds=SAFETY_IDLE_TTL,
        delete_after_stop_seconds=SAFETY_DELETE_AFTER_STOP,
        timeout=120,
        **kw,
    )


def run(sbx: Sandbox, cmd: str, timeout: int = 120) -> str:
    """Run a shell command and return stdout+stderr, without raising on a non-zero exit."""
    r = sbx.run(cmd, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def claude(sbx: Sandbox, prompt: str, timeout: int = 600, flags: str = "", env: str = "") -> dict:
    """Run Claude Code headless and return its JSON result."""
    out = run(sbx, f"{env} claude -p {shlex.quote(prompt)} --output-format json {flags} < /dev/null", timeout=timeout)
    lines = [line for line in out.splitlines() if line.startswith("{")]
    if not lines:
        raise SystemExit(f"no JSON result from claude:\n{out[-2000:]}")
    return json.loads(lines[-1])
