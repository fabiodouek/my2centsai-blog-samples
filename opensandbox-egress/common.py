"""Shared helpers for the part 4 scripts (egress rules, the MITM CA, the Credential Vault).

What this module does:
  - loads ../opensandbox-eks/common.py (by path, not copied) and re-exports its helpers:
    connection(), require_server(), delete_all_sandboxes(), list_sandbox_ids(), env_sh(), api_key(),
    DOMAIN. The server address, the API key and env.sh all come from that directory.
  - ensure_port_forward() / port_forward(): part 3's keeper (../opensandbox-lifecycle/common.py) for the
    kubectl port-forward to the lifecycle server on 127.0.0.1:18080 (not 8080: another local service often
    owns that and answers 404).
  - read_env_key(): the one parser for the real credentials. It reads the file named by OSB_SECRETS_ENV
    (default ~/.config/opensandbox-eks/secrets.env) line by line; the file is never sourced and a value is never printed.
  - scrub(): replaces every value read by read_env_key (and its base64 form) in text with <redacted>.
    Every helper here that prints sandbox output goes through it.
  - small helpers the scripts share: create_sandbox(), sh(), probe(), vault dicts, the echo server's names.

Nothing here prints a credential.
"""

from __future__ import annotations

import base64
import contextlib
import io
import logging
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple

import httpx
from opensandbox import SandboxSync
from opensandbox.exceptions import SandboxApiException, SandboxConnectionException
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import CredentialProxyConfig, NetworkPolicy, NetworkRule

HERE = Path(__file__).resolve().parent
EKS_DIR = HERE.parent / "opensandbox-eks"

# --- the part 2 helpers, loaded by path (not copied) ------------------------------------------
_eks_path = EKS_DIR / "common.py"
if not _eks_path.is_file():
    sys.exit("../opensandbox-eks/common.py not found: this directory needs the part 2 companion code next to it")
_spec = importlib.util.spec_from_file_location("osb_eks_common", _eks_path)
assert _spec is not None and _spec.loader is not None
_eks = importlib.util.module_from_spec(_spec)
sys.modules["osb_eks_common"] = _eks
_spec.loader.exec_module(_eks)

DOMAIN = _eks.DOMAIN
connection = _eks.connection
require_server = _eks.require_server
delete_all_sandboxes = _eks.delete_all_sandboxes
list_sandbox_ids = _eks.list_sandbox_ids
env_sh = _eks.env_sh
api_key = _eks.api_key
image = _eks.image  # the plain python:3.12-slim image; part 4 uses workload_image() instead

EXAMPLE = "opensandbox-egress"
SANDBOX_NAMESPACE = "opensandbox"

# The in-cluster echo server (manifests/echo-server.yaml): plain HTTP on port 80, two Services.
ECHO_HOST = "echo.opensandbox.svc.cluster.local"      # -> listener 8080
ECHO_ALT_HOST = "echo-alt.opensandbox.svc.cluster.local"  # -> listener 8081
# The merged CA bundle (certifi + the proxy's CA) that execd's bootstrap writes in the sandbox.
AWS_CA_BUNDLE = "/opt/opensandbox/merged-ca-certificates.pem"


# --- own sandboxes -------------------------------------------------------------------------------
def own_metadata(script: str, run_id: str | None = None) -> dict[str, str]:
    """Metadata for Sandbox.create: {"example": "opensandbox-egress", "script": ..., "run": ...}.

    Same shape as ../opensandbox-eks/common.py's own_metadata, with this directory's name, so that
    cleanup() deletes only the sandboxes these scripts made.
    """
    return {"example": EXAMPLE, "script": script, "run": run_id or uuid.uuid4().hex[:10]}


def safe_cleanup(run_id: str | None = None) -> None:
    """The cleanup every script ends with: restore the port-forward, delete this directory's sandboxes
    (one run's, or all), never raise. The kill of a sandbox the controller has already removed answers
    "not found"; that is expected (the list lags the controller by a moment) and not shown."""
    quiet = io.StringIO()
    sdk_logger = logging.getLogger("opensandbox")
    previous = sdk_logger.level
    sdk_logger.setLevel(logging.CRITICAL)
    try:
        ensure_port_forward()
        with contextlib.redirect_stderr(quiet):
            deleted = cleanup(run_id)
        print(f"  deleted {deleted} sandbox(es)")
    except BaseException as error:  # SystemExit from the helpers included: cleanup must not hide the script's own result
        print(f"  clean up did not finish: {scrub(str(error))[:200]}; sandboxes expire on their own TTL", file=sys.stderr)
    finally:
        sdk_logger.setLevel(previous)
    leftovers = [line for line in quiet.getvalue().splitlines() if line.strip() and "not found" not in line.lower()]
    for line in leftovers[:3]:
        print(f"  clean up: {scrub(line)[:200]}", file=sys.stderr)


def cleanup(run_id: str | None = None) -> int:
    """Delete this directory's sandboxes (all of them, or one run's). Returns how many."""
    meta = {"example": EXAMPLE}
    if run_id:
        meta["run"] = run_id
    return delete_all_sandboxes(metadata=meta)


# --- the port-forward: part 3's keeper (C3) -----------------------------------------------------
# ../opensandbox-lifecycle/common.py runs a keeper thread that restarts `kubectl port-forward` whenever
# /health stops answering, and stops only a forward it started. Loaded by path, like part 2's helpers.
_lc_path = HERE.parent / "opensandbox-lifecycle" / "common.py"
if not _lc_path.is_file():
    sys.exit("../opensandbox-lifecycle/common.py not found: part 3's directory is needed for ensure_port_forward()")
_lc_spec = importlib.util.spec_from_file_location("osb_lifecycle_common", _lc_path)
assert _lc_spec is not None and _lc_spec.loader is not None
_lc = importlib.util.module_from_spec(_lc_spec)
sys.modules["osb_lifecycle_common"] = _lc
_lc_spec.loader.exec_module(_lc)
ensure_port_forward = _lc.ensure_port_forward
port_forward = _lc.port_forward


def _healthy() -> bool:
    try:
        reply = httpx.get(f"http://{DOMAIN}/health", timeout=2)
        return reply.status_code == 200 and reply.json().get("status") == "healthy"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


# --- the real credentials: one parser, never sourced, never printed ----------------------------------
_SECRETS: set[str] = set()
_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def secrets_path() -> Path:
    """OSB_SECRETS_ENV, default ~/.config/opensandbox-eks/secrets.env."""
    return Path(os.environ.get("OSB_SECRETS_ENV") or "~/.config/opensandbox-eks/secrets.env").expanduser()


def read_env_key(name: str, path: str | os.PathLike[str] | None = None, secret: bool = True) -> str:
    """The value of NAME in the secrets .env. Parsed line by line (KEY=value, optional `export`,
    optional matching quotes); nothing is executed. Raises if the file or the key is missing, with a
    message that never contains a value. The value is remembered so that scrub() can hide it
    (secret=False for a plain setting such as a repository name)."""
    file = Path(path).expanduser() if path else secrets_path()
    if not file.is_file():
        raise RuntimeError(f"secrets file not found: {file} (set OSB_SECRETS_ENV; see .env.example)")
    found: str | None = None
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match and match.group(1) == name:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            found = value  # the last assignment wins, as in a shell
    if not found:
        raise RuntimeError(f"{name} is not set in {file}")
    if secret:
        register_secret(found)
    return found


def register_secret(value: str) -> None:
    """Hide this value (and its base64 form) in everything scrub() sees."""
    if len(value) >= 6:
        _SECRETS.add(value)
        _SECRETS.add(base64.b64encode(value.encode()).decode())


# Built from pieces so that this file does not itself match the secret scans it is checked with.
_TOKEN_SHAPES = re.compile("|".join(["github" + r"_pat_[A-Za-z0-9_]{20,}", "gh" + r"[pousr]_[A-Za-z0-9]{20,}", "sk" + r"-ant-[A-Za-z0-9_\-]{20,}"]))


def scrub(text: str) -> str:
    """Replace every registered secret, and anything shaped like a GitHub or Anthropic token."""
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return _TOKEN_SHAPES.sub("<redacted>", text)


def _scrub_log_records() -> None:
    """Every log record (the SDK's included) passes through scrub() before any handler sees it."""
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
        except Exception:  # a malformed record is left as it is
            pass
        return record

    logging.setLogRecordFactory(factory)


_scrub_log_records()


def basic_credential(user: str, secret: str) -> str:
    """The value a `basic` vault credential holds: base64 of user:secret (the vault sends `Basic <value>`)."""
    value = base64.b64encode(f"{user}:{secret}".encode()).decode()
    register_secret(value)
    return value


def dummy(label: str) -> str:
    """A made-up credential for the echo-server tests. It cannot be mistaken for a real token."""
    return f"dummy-{label}-{uuid.uuid4().hex[:8]}"


# --- output ---------------------------------------------------------------------------------------
class Checks:
    """Collects PASS/FAIL lines; finish() exits 1 if any failed."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        line = f"{label}" + (f"  ({scrub(detail)})" if detail else "")
        if ok:
            print(f"  PASS  {line}", flush=True)
        else:
            self.failures.append(label)
            print(f"  FAIL  {line}", flush=True)
        return ok

    def finish(self) -> None:
        print()
        if self.failures:
            print(f"{len(self.failures)} check(s) failed:")
            for label in self.failures:
                print(f"  - {label}")
            sys.exit(1)
        print("all checks passed")


def heading(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def note(text: str) -> None:
    print(f"  note  {scrub(text)}", flush=True)


def show(text: str, limit: int = 12, indent: str = "        ") -> None:
    """Print command output, scrubbed, indented, at most `limit` lines."""
    lines = scrub(text).rstrip().splitlines() or ["(no output)"]
    for line in lines[:limit]:
        print(f"{indent}{line[:200]}")
    if len(lines) > limit:
        print(f"{indent}... {len(lines) - limit} more line(s)")


# --- kubectl --------------------------------------------------------------------------------------
def kubectl(*args: str, check: bool = True) -> str:
    """kubectl pinned to the cluster's context (from env.sh). Returns stdout."""
    done = subprocess.run(["kubectl", "--context", env_sh("KUBE_CONTEXT"), *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def sandbox_pod(sb: SandboxSync) -> str:
    """The pod of a sandbox, by its opensandbox.io/id label."""
    name = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"opensandbox.io/id={sb.id}",
                   "-o", "jsonpath={.items[0].metadata.name}", check=False).strip()
    if not name:
        raise RuntimeError(f"no pod found for sandbox {sb.id}")
    return name


def pod_containers(pod: str) -> tuple[list[str], list[str]]:
    """(container names, init container names) of a pod in the sandbox namespace."""
    spec = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", pod, "-o", "json"))["spec"]
    return [c["name"] for c in spec.get("containers", [])], [c["name"] for c in spec.get("initContainers", [])]


def egress_container(pod: str) -> str | None:
    """The egress sidecar's container name (a container or init container with 'egress' in its name)."""
    containers, inits = pod_containers(pod)
    return next((name for name in containers + inits if "egress" in name), None)


def egress_log(sb: SandboxSync, tail: int = 400) -> str:
    """The egress sidecar's log (empty if the pod has no sidecar). Scrubbed."""
    pod = sandbox_pod(sb)
    container = egress_container(pod)
    if not container:
        return ""
    return scrub(kubectl("-n", SANDBOX_NAMESPACE, "logs", pod, "-c", container, f"--tail={tail}", check=False))


def echo_cluster_ip() -> str:
    """The ClusterIP of the echo Service (the fallback way to allow it)."""
    ip = kubectl("-n", SANDBOX_NAMESPACE, "get", "svc", "echo", "-o", "jsonpath={.spec.clusterIP}", check=False).strip()
    if not ip:
        sys.exit("the echo Service is missing: run ./00_echo.sh first")
    return ip


# --- sandboxes --------------------------------------------------------------------------------------
def workload_image() -> str:
    """The part 4 image (00_image.sh): python:3.12-slim plus curl, git, Node and the AWS CLI v2."""
    return f"{env_sh('ECR_REGISTRY')}/{env_sh('ECR_PREFIX')}/egress-tools:1"


def allow(target: str | None = None, ports: list[int] | None = None) -> NetworkRule:
    return NetworkRule(action="allow", target=target, ports=ports)


def deny_policy(*targets: str | NetworkRule) -> NetworkPolicy:
    """defaultAction deny plus an allow rule per FQDN, wildcard, IP or CIDR given (or rules as they are)."""
    rules = [t if isinstance(t, NetworkRule) else allow(t) for t in targets]
    return NetworkPolicy(default_action="deny", egress=rules or None)


def create_sandbox(script: str, run_id: str, *, policy: NetworkPolicy | None = None, proxy: bool = False,
                   env: dict[str, str] | None = None, ttl_minutes: int = 20) -> SandboxSync:
    """Create a sandbox from the part 4 image, tagged with this directory's metadata.

    policy:  a network policy (the egress sidecar exists only when this is given).
    proxy:   credential_proxy.enabled, which turns on the transparent MITM the vault needs.
    """
    for attempt in (1, 2):
        try:
            return _create(script, run_id, policy, proxy, env, ttl_minutes)
        except SandboxConnectionException:
            if attempt == 2 or _healthy():
                raise
            ensure_port_forward()  # the forward died (for instance the server pod was replaced)
    raise AssertionError("unreachable")


def _create(script, run_id, policy, proxy, env, ttl_minutes) -> SandboxSync:
    return SandboxSync.create(
        workload_image(),
        connection_config=connection("proxy"),
        metadata=own_metadata(script, run_id),
        network_policy=policy,
        credential_proxy=CredentialProxyConfig(enabled=True) if proxy else None,
        env=env,
        timeout=timedelta(minutes=ttl_minutes),
        ready_timeout=timedelta(seconds=180),
        resource={"cpu": "500m", "memory": "1Gi"},
    )


class Out(NamedTuple):
    rc: int
    text: str  # stdout and stderr together, scrubbed


_RC = re.compile(r"@@rc=(\d+)\s*$")


def sh(sb: SandboxSync, command: str, timeout: int = 60, envs: dict[str, str] | None = None) -> Out:
    """Run shell text in the sandbox; return (exit code, combined output). 124 means no exit code came back.

    One reconnect: if the call fails because the port-forward died, restart it and retry once.
    """
    wrapped = f"( {command}\n) 2>&1; echo \"@@rc=$?\""
    opts = RunCommandOpts(timeout=timedelta(seconds=timeout), envs=envs)
    for attempt in (1, 2):
        try:
            execution = sb.commands.run(wrapped, opts=opts)
            break
        except Exception:
            if attempt == 2 or _healthy():  # not a dead port-forward: report the real error
                raise
            ensure_port_forward()
    text = "".join(part.text for part in execution.logs.stdout)
    rc = 124
    match = _RC.search(text)
    if match:
        rc = int(match.group(1))
        text = text[: match.start()]
    if execution.error is not None and not match:
        text += f"\n[{execution.error.name}: {execution.error.value}]"
    return Out(rc, scrub(text.rstrip("\n")))


def probe(sb: SandboxSync, url: str, timeout: int = 6, extra: str = "") -> tuple[bool, str]:
    """curl the URL from inside the sandbox. Returns (reached, 'http 200' | 'curl rc N').

    reached means a TCP connection was made and an HTTP answer came back (any status). A name that
    the DNS proxy refuses (NXDOMAIN) is curl rc 6; a dropped packet is rc 28 (timeout)."""
    out = sh(sb, f"curl -sS -m {timeout} -o /dev/null -w '%{{http_code}}' {extra} '{url}'", timeout=timeout + 15)
    code = out.text.strip()[-3:] if out.rc == 0 else "000"
    if out.rc == 0 and code != "000":
        return True, f"http {code}"
    return False, f"curl rc {out.rc}"


def resolves(sb: SandboxSync, host: str) -> bool:
    """Does the sandbox's resolver (the sidecar's DNS proxy) answer for this name?"""
    return sh(sb, f"getent hosts '{host}'", timeout=20).rc == 0


def echo_reply(text: str) -> dict:
    """Parse the echo server's JSON out of command output (the last line that is a JSON object)."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def header(reply: dict, name: str) -> str | None:
    """A request header as the echo server saw it, case-insensitive."""
    for key, value in (reply.get("headers") or {}).items():
        if key.lower() == name.lower():
            return value
    return None


# --- vault payloads ------------------------------------------------------------------------------
def credential(name: str, value: str) -> dict:
    """An inline vault credential. The value is write-only: the sidecar never returns it."""
    return {"name": name, "source": {"type": "inline", "value": value}}


def binding(name: str, hosts: list[str], auth: dict, *, schemes: list[str] | None = None,
            paths: list[str] | None = None, methods: list[str] | None = None) -> dict:
    match: dict = {"hosts": hosts}
    if schemes:
        match["schemes"] = schemes
    if paths:
        match["paths"] = paths
    if methods:
        match["methods"] = methods
    return {"name": name, "match": match, "auth": auth}


def conflict_status(error: Exception) -> int | None:
    """The HTTP status of a failed SDK call (the vault's revision conflict is one), else None."""
    return error.status_code if isinstance(error, SandboxApiException) else None
