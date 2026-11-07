"""Shared helpers for the Python scripts in this directory (07, 98, and parts 3 to 6).

Where things come from:
  - the server address: OSB_DOMAIN, default 127.0.0.1:18080 (the kubectl port-forward below)
  - the API key: OSB_API_KEY from the environment, else from .env (gitignored)
  - the image: $ECR_REGISTRY/$ECR_PREFIX/python:3.12-slim, with the two values read from env.sh,
    which 01_cluster.sh writes
Nothing here prints the API key.

Start the port-forward in another terminal and leave it running:
  kubectl -n opensandbox-system port-forward svc/opensandbox-server 18080:80
Port 18080 on purpose: something else on a laptop often owns 8080 and answers 404.
"""

import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Literal

import httpx
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.models.sandboxes import SandboxFilter
from opensandbox.sync import SandboxManagerSync

HERE = Path(__file__).resolve().parent
DOMAIN = os.environ.get("OSB_DOMAIN", "127.0.0.1:18080")
PORT_FORWARD = "kubectl -n opensandbox-system port-forward svc/opensandbox-server 18080:80"
KEY_HEADER = "OPEN-SANDBOX-API-KEY"
# Every sandbox a script creates carries this metadata key, so a script can list and delete
# only its own (see own_metadata and delete_all_sandboxes(metadata=...)).
# The key is "example" because the series' scripts are the "examples" of this repository; any key
# not used by the server or by Kubernetes works. The metadata becomes pod labels, so keys and values
# must be valid label parts.
OWN_KEY = "example"
OWN_VALUE = "opensandbox-eks"


def _read_assignments(path: Path, export: bool) -> dict[str, str]:
    """Parse KEY=value (or `export KEY='value'`) lines. Nothing is executed."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$" if export else r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for line in path.read_text().splitlines():
        match = pattern.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def env_sh(name: str) -> str:
    """A value from env.sh (written by 01_cluster.sh). Exits with one line if env.sh is missing."""
    path = HERE / "env.sh"
    if not path.is_file():
        sys.exit("env.sh not found: run ./01_cluster.sh first")
    value = _read_assignments(path, export=True).get(name, "")
    if not value:
        sys.exit(f"env.sh has no {name}: run ./01_cluster.sh again")
    return value


def api_key() -> str:
    """OSB_API_KEY from the environment, else from .env. Exits with one line if neither has it."""
    key = os.environ.get("OSB_API_KEY") or _read_assignments(HERE / ".env", export=False).get("OSB_API_KEY", "")
    if not key:
        sys.exit("OSB_API_KEY is not set and .env has none: run ./04_install.sh first")
    return key


def image() -> str:
    """The workload image in the cluster's ECR registry."""
    return f"{env_sh('ECR_REGISTRY')}/{env_sh('ECR_PREFIX')}/python:3.12-slim"


def connection(via: Literal["proxy", "gateway"]) -> ConnectionConfigSync:
    """How the SDK reaches the server and the sandboxes.

    Both talk to the lifecycle server at DOMAIN (through the port-forward) for create, list and kill.
    They differ in where the calls to the sandbox itself (commands, files) go:
      proxy    use_server_proxy=True: the server relays them to the sandbox. Needs only the port-forward.
      gateway  use_server_proxy=False: the server announces the ingress gateway's endpoint (the NLB
               from 05_gateway.sh) and the SDK calls that directly. Needs this machine's IP to be
               the one the NLB allows.
    """
    if via not in ("proxy", "gateway"):
        raise ValueError(f"via must be 'proxy' or 'gateway', not {via!r}")
    return ConnectionConfigSync(domain=DOMAIN, api_key=api_key(), use_server_proxy=(via == "proxy"))


def require_server() -> None:
    """Exit 1 with one line if the server is not reachable, or rejects the API key.

    The message names the port-forward command for port 18080 whatever OSB_DOMAIN says: it is the
    command to start, not the address that was tried (that is in the message too).

    Checks the /health body: with no port-forward, another local service on the port may answer
    (404, or 200 with a page that is not the server's).
    """
    base = f"http://{DOMAIN}"
    try:
        reply = httpx.get(f"{base}/health", timeout=3)
        healthy = reply.status_code == 200 and reply.json().get("status") == "healthy"
    except (httpx.HTTPError, ValueError, AttributeError):
        healthy = False
    if not healthy:
        sys.exit(f"Server not reachable at {DOMAIN}: start {PORT_FORWARD}")
    try:
        reply = httpx.get(f"{base}/v1/sandboxes", params={"pageSize": 1}, headers={KEY_HEADER: api_key()}, timeout=10)
    except httpx.HTTPError:
        sys.exit(f"Server at {DOMAIN} did not answer the API key check within 10 s: check the port-forward and the server pod")
    if reply.status_code == 401:
        sys.exit(f"Server at {DOMAIN} rejected the API key: OSB_API_KEY must match the Secret opensandbox-server-auth (see .env)")
    if reply.status_code >= 400:
        sys.exit(f"Server at {DOMAIN} answered HTTP {reply.status_code} to the API key check: check the server pod's logs")


def own_metadata(script: str, run_id: str | None = None) -> dict[str, str]:
    """Metadata to pass to Sandbox.create so that the script can find its own sandboxes later.

    Returns {"example": "opensandbox-eks", "script": <script>, "run": <run_id>}. A new run_id is
    made when none is given. Use delete_all_sandboxes(metadata={"run": run_id}) to remove only these.
    """
    return {OWN_KEY: OWN_VALUE, "script": script, "run": run_id or uuid.uuid4().hex[:12]}


def list_sandbox_ids(manager: SandboxManagerSync, metadata: dict[str, str] | None = None, page_size: int = 100) -> list[tuple[str, str]]:
    """Every (id, state) the server lists, all pages. OSB_PAGE_SIZE overrides page_size (to test paging)."""
    raw = os.environ.get("OSB_PAGE_SIZE", str(page_size))
    if not raw.isdigit() or int(raw) < 1:
        sys.exit(f"OSB_PAGE_SIZE must be a positive whole number (got {raw!r})")
    page_size = int(raw)
    found: list[tuple[str, str]] = []
    page = 1
    while True:
        result = manager.list_sandbox_infos(SandboxFilter(page_size=page_size, page=page, metadata=metadata))
        found += [(info.id, str(info.status.state)) for info in result.sandbox_infos]
        if not result.pagination.has_next_page:
            return found
        page += 1


def delete_all_sandboxes(metadata: dict[str, str] | None = None) -> int:
    """Kill every sandbox the server lists (or, with metadata, every one whose metadata matches),
    then list again until nothing is left. Returns how many distinct sandboxes were deleted.

    metadata={} is an error (it would read as "no filter" and delete everything); pass None for that.
    Pages through the whole list on every pass, so it works past the first page. Exits with a message
    if sandboxes are still listed after about 2 minutes.
    """
    if metadata is not None and not metadata:
        raise ValueError("metadata={} is ambiguous: pass None to delete every sandbox, or a non-empty filter")
    deleted: set[str] = set()
    still_listed = 0
    with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
        for _ in range(60):
            listed = list_sandbox_ids(manager, metadata)
            still_listed = len(listed)
            if not listed:
                return len(deleted)
            for sandbox_id, state in listed:
                if sandbox_id in deleted and "erminating" in state:
                    continue  # already asked; the controller is still removing it
                try:
                    manager.kill_sandbox(sandbox_id)
                    deleted.add(sandbox_id)
                except Exception as error:  # keep going; the next pass lists what is left
                    print(f"could not kill {sandbox_id}: {error}", file=sys.stderr)
            time.sleep(2)
    sys.exit(f"{still_listed} sandboxes are still listed after 2 minutes; deleted {len(deleted)}")
