"""Helpers for the Python scripts in this directory (parts 3 to 6 import or copy them)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from openshell import SandboxClient

GATEWAY = "osh-eks"   # CLI registration from 05_install.sh (port-forward on 127.0.0.1:$LOCAL_PORT)
WORKSPACE = "default"
NAMESPACE = "openshell"
CONTEXT = "osh-eks"   # kubectl context from 01_cluster.sh
PORT = os.environ.get("LOCAL_PORT", "17680")  # the port-forward 05_install.sh started


def client() -> SandboxClient:
    # On Kubernetes, DeleteSandbox returns only when the Pods and volume are gone (about 30 s
    # here), past the SDK's default 30 s RPC timeout.
    return SandboxClient.from_active_cluster(cluster=GATEWAY, timeout=120.0)


def require_gateway() -> None:
    try:
        with client() as c:
            c.health()
    except Exception:  # any connection, TLS or lookup failure means the same thing here
        print(
            f"Gateway not reachable at 127.0.0.1:{PORT}: run ./05_install.sh, or "
            f"kubectl -n openshell port-forward svc/openshell {PORT}:8080",
            file=sys.stderr,
        )
        sys.exit(1)


def phase_name(phase: int) -> str:
    """SANDBOX_PHASE_READY -> Ready. The SDK returns the raw enum number and doesn't
    re-export the enum, so read its name from the generated protobuf module."""
    from openshell._proto import openshell_pb2

    return openshell_pb2.SandboxPhase.Name(phase).removeprefix("SANDBOX_PHASE_").title()


def delete_all_sandboxes(c: SandboxClient, timeout: float = 300) -> int:
    """Delete every sandbox in the workspace (all pages) and wait until none is listed.
    Deletion is asynchronous: each sandbox is asked once, then the list is polled."""
    asked: set[str] = set()
    start = time.monotonic()
    while True:
        refs = list(c.list(workspace=WORKSPACE).all())  # list() pages; all() walks every page
        if not refs:
            return len(asked)
        for ref in refs:
            if ref.name not in asked:
                c.delete(ref.name, workspace=WORKSPACE, allow_missing=True)
                asked.add(ref.name)
        if time.monotonic() - start > timeout:
            raise RuntimeError(f"still listed after {timeout:.0f} s: {', '.join(r.name for r in refs)}")
        time.sleep(2)


def kubectl_json(*args: str) -> dict:
    out = subprocess.run(["kubectl", "--context", CONTEXT, *args, "-o", "json"],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def objects_for(sandbox_id: str) -> dict[str, list[str]]:
    """The Kubernetes objects one sandbox becomes. The driver labels its own objects with
    openshell.ai/sandbox-id; both Pods carry openshell.ai/boundary-pair; the Agent Sandbox
    controller names the workspace PVC workspace-<Sandbox name>."""
    by_id = f"openshell.ai/sandbox-id={sandbox_id}"
    found: dict[str, list[str]] = {}
    crs = kubectl_json("-n", NAMESPACE, "get", "sandboxes.agents.x-k8s.io", "-l", by_id)["items"]
    found["sandboxes.agents.x-k8s.io"] = [i["metadata"]["name"] for i in crs]
    pods = kubectl_json("-n", NAMESPACE, "get", "pods", "-l", f"openshell.ai/boundary-pair={sandbox_id}")["items"]
    found["pods"] = [f"{p['metadata']['name']} ({p['metadata']['labels'].get('openshell.ai/boundary-role', '?')})"
                     for p in pods]
    for kind in ("services", "networkpolicies", "secrets"):
        found[kind] = [i["metadata"]["name"] for i in kubectl_json("-n", NAMESPACE, "get", kind, "-l", by_id)["items"]]
    pvcs = kubectl_json("-n", NAMESPACE, "get", "pvc")["items"]
    found["persistentvolumeclaims"] = [p["metadata"]["name"] for p in pvcs
                                       if any(p["metadata"]["name"] == f"workspace-{n}" for n in found["sandboxes.agents.x-k8s.io"])]
    return found
