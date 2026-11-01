"""Helpers shared by the Python scripts: connect to the local gateway and run commands."""

from __future__ import annotations

import os
import sys

from openshell import SandboxClient

WORKSPACE = "default"
WORKLOAD_IMAGE = "openshell-workload:0.1.6"


def client() -> SandboxClient:
    """The local gateway from 00_gateway.sh ($OPENSHELL_GATEWAY, default "openshell"), even when
    the CLI has another one marked active, such as part 2's osh-eks."""
    return SandboxClient.from_active_cluster(cluster=os.environ.get("OPENSHELL_GATEWAY", "openshell"))


def require_gateway() -> None:
    try:
        with client() as c:
            c.health()
    except Exception:  # any connection, TLS or lookup failure means the same thing here
        print("OpenShell gateway not reachable: run ./00_gateway.sh first", file=sys.stderr)
        sys.exit(1)


def run(c: SandboxClient, name: str, cmd: list[str]) -> int:
    """Run cmd in sandbox `name`, print it like a terminal would, and return the exit code."""
    print(f"$ {' '.join(cmd)}")
    result = c.exec(name, cmd, workspace=WORKSPACE, no_login_shell=True)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    print(f"(exit {result.exit_code})")
    return result.exit_code
