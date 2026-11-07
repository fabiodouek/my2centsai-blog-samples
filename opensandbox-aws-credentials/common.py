"""Shared helpers for the part 5 scripts (AWS credentials through the egress SigV4 addon).

What this module does:
  - loads ../opensandbox-eks/common.py and ../opensandbox-lifecycle/common.py by path (not copied) and
    re-exports connection(), ensure_port_forward() and the server settings.
  - the broker client: broker_create / broker_get / broker_refresh / broker_revoke / broker_delete over
    the port-forward to the broker (127.0.0.1:18090). The broker is the only thing that holds AWS
    credentials; a script never sees one.
  - scrub(): the display filter. The test bucket becomes example-bucket, the account ID becomes
    123456789012, anything shaped like a temporary key or a Bedrock key becomes <redacted>, and the home
    directory becomes ~. Every line these helpers print goes through it, and a raw AWS error body is cut
    down to its error code.
  - run() / sh(): run a command in a sandbox. run() prints "$ command", the output and the exit code.
  - the sidecar log: egress_log(), and a mark/since pair to read only the lines one step produced.

The bucket name comes from secrets_env.py (the one parser; the secrets file is never sourced). Nothing
here prints a credential, the bucket name or the account ID.
"""

from __future__ import annotations

import atexit
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
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.filesystem import WriteEntry

HERE = Path(__file__).resolve().parent
EKS_DIR = HERE.parent / "opensandbox-eks"
LIFECYCLE_DIR = HERE.parent / "opensandbox-lifecycle"
LOGS = HERE / "logs"
EXAMPLE = "opensandbox-aws-credentials"
SANDBOX_NAMESPACE = "opensandbox"
BROKER_URL = os.environ.get("OSB_BROKER_URL", "http://127.0.0.1:18090")
BROKER_FORWARD_PORT = 18090  # not 8080: another local service often owns that

# What the sandbox is given instead of keys (the broker sets these; see broker/broker.py sandbox_env()).
PLACEHOLDER_ACCESS_KEY_ID = "AKIA_OSB_PLACEHOLDER"
PLACEHOLDER_SECRET = "osb-placeholder-secret"
# The pattern the repository's secret scan flags. Built from pieces so this file does not match it itself.
KEY_ID_SHAPE = re.compile("AKI" + "A[0-9A-Z]{16}")
TEMP_KEY_SHAPE = re.compile("ASI" + "A[0-9A-Z]{16}")
assert not KEY_ID_SHAPE.search(PLACEHOLDER_ACCESS_KEY_ID), "the placeholder key must not look like an access key ID"
assert not TEMP_KEY_SHAPE.search(PLACEHOLDER_ACCESS_KEY_ID)

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"  # the pinned Bedrock model; not the .env BEDROCK_MODEL_ID
CA_BUNDLE = "/opt/opensandbox/merged-ca-certificates.pem"


# --- the earlier parts' helpers, loaded by path ---------------------------------------------------
def _load(name: str, path: Path):
    if not path.is_file():
        sys.exit(f"../{path.parent.name}/{path.name} not found: this directory needs the earlier parts' companion code next to it")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_eks = _load("osb_eks_common", EKS_DIR / "common.py")
_lc = _load("osb_lifecycle_common", LIFECYCLE_DIR / "common.py")
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import secrets_env  # noqa: E402  (this directory's parser; needs the path line above)

DOMAIN = _eks.DOMAIN
connection = _eks.connection
delete_all_sandboxes = _eks.delete_all_sandboxes
env_sh = _eks.env_sh
ensure_port_forward = _lc.ensure_port_forward
KUBE_CONTEXT = _lc.KUBE_CONTEXT
ECR_REGISTRY = _lc.ECR_REGISTRY
ECR_PREFIX = _lc.ECR_PREFIX


def workload_image() -> str:
    """Part 5's image (00_image.sh): part 4's egress-tools plus boto3."""
    return f"{ECR_REGISTRY}/{ECR_PREFIX}/aws-tools:1"


# --- broker.env and the bucket ---------------------------------------------------------------------
def broker_env() -> dict[str, str]:
    """The settings 01_iam.sh wrote to broker.env. Exits with one line if it is missing."""
    path = HERE / "broker.env"
    if not path.is_file():
        sys.exit("broker.env not found: run ./01_iam.sh first")
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def account_id() -> str:
    """The account ID, from the role ARN in broker.env. Used for display filtering only."""
    parts = broker_env().get("SANDBOX_ROLE_ARN", "").split(":")
    return parts[4] if len(parts) > 4 else ""


def test_bucket() -> str:
    """AWS_TEST_BUCKET from the secrets file, through the one parser. Never printed (scrub() shows example-bucket)."""
    try:
        return secrets_env.read_secret("AWS_TEST_BUCKET")
    except (KeyError, FileNotFoundError) as error:
        sys.exit(str(error.args[0]))


def ddb_table() -> str:
    return broker_env().get("DDB_TABLE", "osb-cred-test")


def sandbox_role_arn() -> str:
    return broker_env()["SANDBOX_ROLE_ARN"]


# --- display filtering -------------------------------------------------------------------------------
_BEDROCK_KEY = re.compile(r"bedrock-api-key-(?!osb-placeholder)[A-Za-z0-9+/=_\-]{16,}")
_AWS_ERROR = re.compile(r"(An error occurred \(\w+\) when calling the \w+ operation)")
_BODY_START = re.compile(r"^\s*(<\?xml|<Error|<\w+Response|\{\s*\"(__type|message|Message|Error|Code))")
_SECRET_FIELD = re.compile(r"(secret[_-]?access[_-]?key|session[_-]?token|x-osb-aws-[a-z-]+)(\s*[:=]\s*)(?!osb-placeholder)\S+", re.I)


def scrub(text: str) -> str:
    """Make output safe to show: no bucket name, account ID, home directory, temporary key or token,
    and AWS error bodies cut down to the error code."""
    account = account_id() if (HERE / "broker.env").is_file() else ""
    if account:
        text = text.replace(account, "123456789012").replace(f"{account[:4]}-{account[4:8]}-{account[8:]}", "1234-5678-9012")
    try:
        bucket = secrets_env.read_secret("AWS_TEST_BUCKET")
    except (KeyError, FileNotFoundError):
        bucket = ""
    if bucket:
        text = text.replace(bucket, "example-bucket")
    text = text.replace(str(Path.home()), "~")
    text = TEMP_KEY_SHAPE.sub("<redacted>", text)
    text = _BEDROCK_KEY.sub("<redacted>", text)
    text = _SECRET_FIELD.sub(r"\1\2<redacted>", text)
    out = []
    for line in text.splitlines():
        match = _AWS_ERROR.search(line)
        if match:
            line = line[: match.end()] + (" ..." if len(line) > match.end() else "")
        elif _BODY_START.match(line):
            line = "(response body not shown)"
        out.append(line)
    return "\n".join(out)


def error_code(text: str) -> str:
    """The AWS error code in CLI or boto3 output ('AccessDenied'), or a short classification of a failure
    that never reached an AWS answer. Never returns the message."""
    match = re.search(r"An error occurred \((\w+)\)", text)
    if match:
        return match.group(1)
    for needle, label in (("Connection was closed", "connection closed"), ("Could not connect", "could not connect"),
                          ("Could not resolve", "name not resolved"), ("SSL validation failed", "TLS not trusted"),
                          ("Read timeout", "read timeout"), ("Connect timeout", "connect timeout")):
        if needle in text:
            return label
    return "no AWS error code"


def heading(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def note(text: str) -> None:
    print(f"  note  {scrub(text)}", flush=True)


class Checks:
    """Collects PASS/FAIL lines; finish() exits 1 if any failed."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        line = label + (f"  ({scrub(detail)})" if detail else "")
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


# --- the broker ----------------------------------------------------------------------------------------
class BrokerError(Exception):
    """A non-2xx answer from the broker. status and body are what it said (the body is never a credential)."""

    def __init__(self, status: int, body: dict):
        super().__init__(f"broker answered {status}")
        self.status = status
        self.body = body


_forward: subprocess.Popen | None = None


def _broker_up() -> bool:
    try:
        reply = httpx.get(f"{BROKER_URL}/healthz", timeout=2)
        return reply.status_code == 200 and reply.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def ensure_broker_forward(wait_seconds: float = 30) -> None:
    """Make the broker reachable on 127.0.0.1:18090: start `kubectl port-forward` if it is not, and stop it
    again at exit if this process started it. Exits with one line if the broker does not answer."""
    global _forward
    if _broker_up():
        return
    if BROKER_URL != "http://127.0.0.1:18090":
        sys.exit(f"broker not reachable at {BROKER_URL}")
    ns = broker_env().get("BROKER_NS", "opensandbox-broker")
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / "broker-port-forward.log", "ab")  # noqa: SIM115  (lives as long as the process)
    _forward = subprocess.Popen(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", ns, "port-forward", "svc/osb-broker", f"{BROKER_FORWARD_PORT}:80"],
        stdout=log, stderr=log,
    )
    atexit.register(lambda: _forward and _forward.poll() is None and _forward.terminate())
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _broker_up():
            return
        time.sleep(0.5)
    sys.exit(f"broker not reachable at {BROKER_URL} within {wait_seconds:.0f} s: run ./02_broker.sh (see logs/broker-port-forward.log)")


def _broker(method: str, path: str, body: dict | None = None, timeout: float = 120) -> dict:
    reply = httpx.request(method, f"{BROKER_URL}{path}", json=body, timeout=timeout)
    try:
        data = reply.json()
    except ValueError:
        data = {"error": reply.text[:200]}
    if reply.status_code >= 300:
        raise BrokerError(reply.status_code, data)
    return data


def own_metadata(script: str, run_id: str | None = None) -> dict[str, str]:
    """{"example": "opensandbox-aws-credentials", "script": ..., "run": ...}: ../opensandbox-eks/common.py's shape
    with this directory's name, so cleanup() finds only these sandboxes."""
    return {**_eks.own_metadata(script, run_id), _eks.OWN_KEY: EXAMPLE}


_created: list[str] = []


def broker_create(bedrock_bearer: bool = False, script: str = "", run_id: str | None = None,
                  timeout_minutes: int = 30) -> dict:
    """Ask the broker for a sandbox with AWS credentials bound. Returns its view: sandbox_id, revision,
    expires_at, next_rotation_in_seconds, ... (no credential)."""
    view = _broker("POST", "/sandboxes", {
        "image": workload_image(),
        "bedrock_bearer": bedrock_bearer,
        "timeout_minutes": timeout_minutes,
        "metadata": own_metadata(script or Path(sys.argv[0]).stem, run_id),
    }, timeout=300)
    _created.append(view["sandbox_id"])
    return view


def broker_get(sandbox_id: str) -> dict:
    return _broker("GET", f"/sandboxes/{sandbox_id}")


def broker_refresh(sandbox_id: str, expected_revision: int | None = None) -> dict:
    """Rotate now. With expected_revision, a stale value raises BrokerError (409 or 412: the vault's own
    status; 412 also means the vault is not Ready)."""
    body = {} if expected_revision is None else {"expected_revision": expected_revision}
    return _broker("POST", f"/sandboxes/{sandbox_id}/refresh", body)


def broker_revoke(sandbox_id: str) -> dict:
    """Delete the sandbox's vault and stop rotating. The STS session itself stays valid until it expires."""
    return _broker("DELETE", f"/sandboxes/{sandbox_id}/vault")


def broker_delete(sandbox_id: str) -> None:
    _broker("DELETE", f"/sandboxes/{sandbox_id}")


def connect(sandbox_id: str) -> SandboxSync:
    """The SDK handle for a broker-made sandbox, through the port-forward to the server."""
    return SandboxSync.connect(sandbox_id, connection_config=connection("proxy"), connect_timeout=timedelta(seconds=60))


def new_sandbox(bedrock_bearer: bool = False, script: str = "") -> tuple[SandboxSync, dict]:
    """broker_create + connect. Prints one line. Returns (handle, view)."""
    view = broker_create(bedrock_bearer=bedrock_bearer, script=script)
    sb = connect(view["sandbox_id"])
    print(f"  sandbox {view['sandbox_id']}  vault revision {view['revision']}  "
          f"credentials expire {view['expires_at']}  bedrock_bearer={view['bedrock_bearer']}", flush=True)
    return sb, view


def cleanup() -> None:
    """End of every script: delete this run's sandboxes through the broker (it stops their rotation), then
    sweep any other sandbox of this directory through the server. Never raises."""
    for sandbox_id in _created:
        try:
            broker_delete(sandbox_id)
        except Exception:
            pass
    _created.clear()
    try:
        ensure_port_forward()
        deleted = delete_all_sandboxes(metadata={_eks.OWN_KEY: EXAMPLE})
        print(f"  clean up: {deleted} sandbox(es) left to delete")
    except BaseException as error:  # SystemExit from the helpers included: cleanup must not hide the result
        print(f"  clean up did not finish: {scrub(str(error))[:200]}; sandboxes expire on their own TTL", file=sys.stderr)


# --- kubectl and the sidecar log -------------------------------------------------------------------------
def kubectl(*args: str, check: bool = True, timeout: float = 120) -> str:
    done = subprocess.run(["kubectl", "--context", KUBE_CONTEXT, *args], capture_output=True, text=True, timeout=timeout)
    if check and done.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {scrub(done.stderr.strip() or done.stdout.strip())[:200]}")
    return done.stdout


def sandbox_pod(sandbox_id: str) -> str:
    name = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"opensandbox.io/id={sandbox_id}",
                   "-o", "jsonpath={.items[0].metadata.name}", check=False).strip()
    if not name:
        raise RuntimeError(f"no pod found for sandbox {sandbox_id}")
    return name


def egress_container(pod: str) -> str:
    spec = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", pod, "-o", "json"))["spec"]
    names = [c["name"] for c in spec.get("containers", []) + spec.get("initContainers", [])]
    found = next((n for n in names if "egress" in n), None)
    if not found:
        raise RuntimeError(f"pod {pod} has no egress container")
    return found


def egress_log(sandbox_id: str) -> list[str]:
    """The whole egress sidecar log of a sandbox, one string per line."""
    pod = sandbox_pod(sandbox_id)
    text = kubectl("-n", SANDBOX_NAMESPACE, "logs", pod, "-c", egress_container(pod), check=False)
    return text.splitlines()


def log_mark(sandbox_id: str) -> int:
    """How many lines the sidecar log has now. Pass to log_since() to read only what came after."""
    return len(egress_log(sandbox_id))


def log_since(sandbox_id: str, mark: int, needle: str | None = None, wait: float = 0, minimum: int = 1) -> list[str]:
    """Sidecar log lines after `mark` that contain `needle` as a plain substring (not anchored: the egress
    process may put its own prefix in front of the addon's `credential proxy: [sigv4] ...`). Waits up to
    `wait` seconds for at least `minimum` of them. Scrubbed."""
    deadline = time.monotonic() + wait
    while True:
        lines = [scrub(line) for line in egress_log(sandbox_id)[mark:] if needle is None or needle in line]
        if len(lines) >= minimum or time.monotonic() >= deadline:
            return lines
        time.sleep(2)


def sigv4_events(lines: list[str]) -> list[str]:
    """Only the addon's own lines from a list of sidecar log lines."""
    return [line for line in lines if "credential proxy: [sigv4]" in line]


def show_log(lines: list[str], limit: int = 6) -> None:
    for line in lines[:limit]:
        text = line[line.find("credential proxy:"):] if "credential proxy:" in line else line
        text = text.split('"', 1)[0]  # the egress process may wrap the line as JSON: drop the trailing '"}'
        print(f"        log: {text[:170]}")
    if len(lines) > limit:
        print(f"        log: ... {len(lines) - limit} more line(s)")


# --- running commands in a sandbox ----------------------------------------------------------------------------
class Out(NamedTuple):
    rc: int
    text: str  # stdout and stderr together, scrubbed


_RC = re.compile(r"@@rc=(\d+)\s*$")


def _healthy() -> bool:
    try:
        reply = httpx.get(f"http://{DOMAIN}/health", timeout=2)
        return reply.status_code == 200 and reply.json().get("status") == "healthy"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def sh(sb: SandboxSync, command: str, timeout: int = 120) -> Out:
    """Run shell text in the sandbox; return (exit code, combined scrubbed output). 124 means no exit code came back.
    One reconnect if the port-forward to the server died."""
    wrapped = f"( {command}\n) 2>&1; echo \"@@rc=$?\""
    opts = RunCommandOpts(timeout=timedelta(seconds=timeout))
    for attempt in (1, 2):
        try:
            execution = sb.commands.run(wrapped, opts=opts)
            break
        except Exception:
            if attempt == 2 or _healthy():
                raise
            ensure_port_forward()
    text = "".join(part.text for part in execution.logs.stdout)
    rc = 124
    match = _RC.search(text)
    if match:
        rc = int(match.group(1))
        text = text[: match.start()]
    if execution.error is not None and not match:
        text += f"\n[{execution.error.name}]"
    return Out(rc, scrub(text.rstrip("\n")))


def run(sb: SandboxSync, command: str, timeout: int = 120, limit: int = 12, label: str | None = None) -> int:
    """Run a command in the sandbox and print `$ command`, its output (scrubbed, at most `limit` lines) and the
    exit code. `label` replaces the command in the printout (for long commands). Returns the exit code."""
    shown = label or command
    print(f"  $ {scrub(shown)}", flush=True)
    out = sh(sb, command, timeout=timeout)
    lines = out.text.splitlines() or ["(no output)"]
    for line in lines[:limit]:
        print(f"      {line[:180]}")
    if len(lines) > limit:
        print(f"      ... {len(lines) - limit} more line(s)")
    print(f"      exit code {out.rc}", flush=True)
    return out.rc


def put_file(sb: SandboxSync, path: str, text: str) -> None:
    sb.files.write_files([WriteEntry(path=path, data=text, mode=644)])


def python_in(sb: SandboxSync, source: str, timeout: int = 120, limit: int = 14, label: str = "python3 (a short boto3 script)") -> Out:
    """Run Python source in the sandbox (written to a file first) and print what it printed. Returns the Out."""
    name = f"/tmp/osb_{uuid.uuid4().hex[:8]}.py"
    put_file(sb, name, source)
    print(f"  $ {label}", flush=True)
    out = sh(sb, f"python3 {name}", timeout=timeout)
    for line in (out.text.splitlines() or ["(no output)"])[:limit]:
        print(f"      {line[:180]}")
    print(f"      exit code {out.rc}", flush=True)
    return out


def aws_env(sb: SandboxSync) -> dict[str, str]:
    """The sandbox's AWS_* variables (names and values: all placeholders; the check is that nothing else is there)."""
    out = sh(sb, "env | grep '^AWS_' | sort")
    pairs = (line.split("=", 1) for line in out.text.splitlines() if "=" in line)
    return {name: value for name, value in pairs}


def short_time(iso: str | None) -> str:
    return (iso or "?").replace("T", " ").replace("Z", " UTC")
