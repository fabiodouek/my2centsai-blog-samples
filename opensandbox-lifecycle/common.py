"""Shared helpers for the Python scripts in this directory.

The server helpers come from part 2's ../opensandbox-eks/common.py. It is loaded from its own
file (not copied), so it keeps reading env.sh and .env in ../opensandbox-eks, where
01_cluster.sh and 04_install.sh wrote them:
  connection(via), require_server(), delete_all_sandboxes(metadata=None), list_sandbox_ids(),
  env_sh(name), api_key(), image(), DOMAIN, PORT_FORWARD

This module adds:
  own_metadata(script)    part 2's, but every sandbox is tagged example=opensandbox-lifecycle,
                          so 99_cleanup.py deletes this directory's sandboxes and no one else's
  ensure_port_forward()   keeps the server reachable on 127.0.0.1:18080 for the whole script: a
                          thread checks /health every second and, whenever nothing answers,
                          starts `kubectl port-forward` (kubectl exits when the server pod is
                          replaced, and sometimes on its own). A forward someone else started is
                          used while it works and never stopped; the one this script started is
                          stopped when the script ends.
  port_forward()          the same as a context manager
  kubectl(), the sandbox namespace and the snapshot repository names
  record_created(id)      notes the id of a sandbox that pauses (results/created-sandboxes.txt),
                          so 99_cleanup.py deletes snapshot images of this directory's sandboxes only
  snapshot_token_status() checks the token in the Secret opensandbox-snapshot-ecr against ECR
Nothing here prints the API key or a registry token.
"""

import atexit
import base64
import importlib.util
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
EKS_DIR = HERE.parent / "opensandbox-eks"
LOGS = HERE / "logs"

if not (EKS_DIR / "env.sh").is_file():
    sys.exit("../opensandbox-eks/env.sh not found: run ../opensandbox-eks/01_cluster.sh first")
if not (EKS_DIR / "common.py").is_file():
    sys.exit("../opensandbox-eks/common.py not found: this directory needs part 2's directory next to it")

_spec = importlib.util.spec_from_file_location("osb_eks_common", EKS_DIR / "common.py")
_eks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_eks)

connection = _eks.connection
require_server = _eks.require_server
delete_all_sandboxes = _eks.delete_all_sandboxes
list_sandbox_ids = _eks.list_sandbox_ids
env_sh = _eks.env_sh
api_key = _eks.api_key
image = _eks.image
DOMAIN = _eks.DOMAIN
PORT_FORWARD = _eks.PORT_FORWARD

# The SDK logs a failed call with its traceback before raising; the scripts catch the exception
# and print one line instead. OSB_SDK_LOG=1 brings the SDK's own log lines back.
if not os.environ.get("OSB_SDK_LOG"):
    logging.getLogger("opensandbox").addHandler(logging.NullHandler())

EXAMPLE = "opensandbox-lifecycle"          # metadata example=<this>, on every sandbox made here
OWN = {_eks.OWN_KEY: EXAMPLE}
KUBE_CONTEXT = env_sh("KUBE_CONTEXT")
AWS_REGION = env_sh("AWS_REGION")
ECR_REGISTRY = env_sh("ECR_REGISTRY")
ECR_PREFIX = env_sh("ECR_PREFIX")
SANDBOX_NAMESPACE = "opensandbox"          # [kubernetes] namespace in the server config
SYSTEM_NAMESPACE = "opensandbox-system"
SNAPSHOT_REPO_NAME = f"{ECR_PREFIX}/snapshots"           # one ECR repository for every snapshot
SNAPSHOT_REPO = f"{ECR_REGISTRY}/{SNAPSHOT_REPO_NAME}"   # controller.snapshot.registry
SNAPSHOT_SECRET = "opensandbox-snapshot-ecr"             # dockerconfigjson in SANDBOX_NAMESPACE
POOL_NAME = "osb-pool"

DEFAULT_DOMAIN = "127.0.0.1:18080"
_FORWARD_CMD = ["kubectl", "--context", KUBE_CONTEXT, "-n", SYSTEM_NAMESPACE, "port-forward",
                "svc/opensandbox-server", "18080:80", "--address", "127.0.0.1"]


def own_metadata(script: str, run_id: str | None = None) -> dict[str, str]:
    """{"example": "opensandbox-lifecycle", "script": <script>, "run": <run id>} for Sandbox.create.

    The metadata becomes pod labels, and list calls filter on it: 99_cleanup.py deletes
    {"example": "opensandbox-lifecycle"}, a script deletes its own {"run": <run id>}.
    """
    meta = _eks.own_metadata(script, run_id)
    meta[_eks.OWN_KEY] = EXAMPLE
    return meta


CREATED_LOG = HERE / "results" / "created-sandboxes.txt"


def record_created(sandbox_id: str) -> None:
    """Remember a sandbox id whose snapshot images this directory may later delete (99_cleanup.py)."""
    CREATED_LOG.parent.mkdir(exist_ok=True)
    with CREATED_LOG.open("a") as out:
        out.write(sandbox_id + "\n")


def registry_answer(dockerconfigjson: str) -> int:
    """HTTP status ECR's GET /v2/ answers to the credentials in a dockerconfigjson (never printed)."""
    auth = json.loads(dockerconfigjson)["auths"][ECR_REGISTRY]["auth"]
    try:
        return httpx.get(f"https://{ECR_REGISTRY}/v2/", headers={"Authorization": f"Basic {auth}"}, timeout=10).status_code
    except httpx.HTTPError:
        return 0


def snapshot_token_status() -> tuple[int, str]:
    """(ECR's answer to the Secret's token: 200 when valid, the token-expires-at annotation). (0, "") if no Secret."""
    out = kubectl("-n", SANDBOX_NAMESPACE, "get", "secret", SNAPSHOT_SECRET, "-o", "json", check=False)
    if not out:
        return 0, ""
    secret = json.loads(out)
    expires = (secret["metadata"].get("annotations") or {}).get("opensandbox-lifecycle/token-expires-at", "")
    config = base64.b64decode(secret.get("data", {}).get(".dockerconfigjson", "")).decode()
    try:
        return registry_answer(config), expires
    except (KeyError, ValueError):
        return 0, expires


def delete_own_sandboxes() -> int:
    """Delete every sandbox this directory's scripts made (metadata example=opensandbox-lifecycle)."""
    return delete_all_sandboxes(metadata=dict(OWN))


def kubectl(*args: str, check: bool = True, timeout: float = 120, stdin: str | None = None) -> str:
    """kubectl pinned to the cluster's context. Returns stdout; raises RuntimeError on failure when check."""
    done = subprocess.run(["kubectl", "--context", KUBE_CONTEXT, *args], capture_output=True, text=True,
                          timeout=timeout, input=stdin)
    if check and done.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def heading(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m", flush=True)


# --- the port-forward -----------------------------------------------------------------------

def _healthy() -> bool:
    try:
        reply = httpx.get(f"http://{DOMAIN}/health", timeout=2)
        return reply.status_code == 200 and reply.json().get("status") == "healthy"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def _listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 18080), timeout=1):
            return True
    except OSError:
        return False


class _Keeper:
    """A thread that keeps 127.0.0.1:18080 answering: every second, when /health fails and the port is
    free, it runs kubectl port-forward until that exits. A healthy forward of someone else's is left alone."""

    def __init__(self) -> None:
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        LOGS.mkdir(exist_ok=True)
        self.thread = threading.Thread(target=self._loop, name="port-forward", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        unhealthy_while_held = 0
        while not self.stopping.is_set():
            if _healthy():
                unhealthy_while_held = 0
                self.stopping.wait(1)
                continue
            if _listening():
                # Someone else holds the port but /health fails: their forward may be reconnecting, or it may
                # point at a replaced pod and never answer again. This script cannot take the port over.
                unhealthy_while_held += 1
                if unhealthy_while_held == 15:
                    print("(127.0.0.1:18080 is held by another process that has not answered /health for 15 s: "
                          "if it is a port-forward to a replaced server pod, stop it so this script can start its own)",
                          flush=True)
                self.stopping.wait(1)
                continue
            unhealthy_while_held = 0
            with open(LOGS / "port-forward.log", "ab") as log:
                with self.lock:
                    if self.stopping.is_set():
                        return
                    self.proc = subprocess.Popen(_FORWARD_CMD, stdin=subprocess.DEVNULL, stdout=log,
                                                 stderr=log, start_new_session=True)
                    self.starts += 1
                if self.starts == 1:
                    print("(nothing answered on 127.0.0.1:18080: started kubectl port-forward for this script)",
                          flush=True)
                self.proc.wait()
            self.stopping.wait(1)

    def stop(self) -> None:
        self.stopping.set()
        with self.lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.thread.join(timeout=5)


_keeper: _Keeper | None = None


def stop_port_forward() -> None:
    """Stop the keeper and the port-forward this script started, if any. Someone else's is left alone."""
    global _keeper
    if _keeper is not None:
        _keeper.stop()
        _keeper = None


def ensure_port_forward(wait_seconds: float = 45) -> None:
    """Start the keeper (once) and return once the server answers /health at DOMAIN.

    Exits with one line if the server does not answer within wait_seconds (for example when
    something that is not the server holds 127.0.0.1:18080). With OSB_DOMAIN pointing somewhere
    else it does nothing (require_server() then reports an unreachable server).
    """
    global _keeper
    if DOMAIN != DEFAULT_DOMAIN:
        return
    if _keeper is None:
        _keeper = _Keeper()
        atexit.register(stop_port_forward)
        # SIGTERM skips atexit: turn it into a normal exit, so the forward this script started is stopped.
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _healthy():
            return
        time.sleep(0.5)
    held = " Something holds the port but is not the server (a port-forward to a replaced pod?)." \
        if _listening() else ""
    sys.exit(f"Server not reachable at {DOMAIN} within {wait_seconds:.0f} s.{held} See logs/port-forward.log")


@contextmanager
def port_forward():
    """with port_forward(): ... runs the body with the server reachable at 127.0.0.1:18080."""
    ensure_port_forward()
    try:
        yield
    finally:
        stop_port_forward()


def connect_server() -> None:
    """The first call of every script: the port-forward, then the health and API key checks."""
    ensure_port_forward()
    require_server()
