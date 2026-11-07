"""Shared helpers for the scripts in this directory."""

import os
import sys

import httpx
from opensandbox import SandboxSync
from opensandbox.config.connection_sync import ConnectionConfigSync

DOMAIN = os.environ.get("OSB_DOMAIN", "127.0.0.1:8080")
API_KEY = os.environ.get("OSB_API_KEY", "local-dev-key")  # the api_key in sandbox.toml
IMAGE = "python:3.12-slim"


def connection() -> ConnectionConfigSync:
    """Where the server is and how to authenticate. Override with OSB_DOMAIN and OSB_API_KEY."""
    return ConnectionConfigSync(domain=DOMAIN, api_key=API_KEY)


def require_server() -> None:
    """Exit with one clear line if the server is down or rejects the API key."""
    api = connection().get_base_url()  # http://<domain>/v1
    try:
        httpx.get(api.removesuffix("/v1") + "/health", timeout=3).raise_for_status()
    except httpx.HTTPError:
        sys.exit(f"OpenSandbox server not reachable at {DOMAIN}: run ./00_server.sh first")
    # /health needs no key, so ask for something that does.
    try:
        reply = httpx.get(f"{api}/sandboxes", params={"pageSize": 1}, headers={"OPEN-SANDBOX-API-KEY": API_KEY}, timeout=10)
    except httpx.HTTPError:
        sys.exit(f"OpenSandbox server at {DOMAIN} did not answer the API key check within 10 s: check server.log")
    if reply.status_code == 401:
        sys.exit(f"OpenSandbox server at {DOMAIN} rejected the API key: set OSB_API_KEY to the api_key in sandbox.toml")
    if reply.status_code >= 400:
        sys.exit(f"OpenSandbox server at {DOMAIN} answered HTTP {reply.status_code} to the API key check: check server.log")


def run(sb: SandboxSync, cmd: str) -> int:
    """Run a shell command in the sandbox, print it with its output, and return its exit code."""
    print(f"$ {cmd}")
    execution = sb.commands.run(cmd)
    stdout = "".join(part.text for part in execution.logs.stdout).rstrip()
    stderr = "".join(part.text for part in execution.logs.stderr).rstrip()
    if stdout:
        print(stdout)
    if stderr:
        print(f"[stderr] {stderr}")
    code = execution.exit_code
    if code is None:  # no exit status reported: treat an error event as a failure
        code = 1 if execution.error else 0
    print(f"exit code: {code}")
    return code
