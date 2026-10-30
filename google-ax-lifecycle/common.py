"""Shared helpers for the AX lifecycle scripts.

Every script drives the real `ax` CLI built by ../google-ax-eks (05_ax.sh), plus
kubectl and the AWS CLI. Settings come from that folder's Terraform outputs, so
the cluster from the install post must be running.
"""

import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent / "google-ax-eks"
AX_BIN = INSTALL / ".src" / "ax" / "bin" / "ax"
ATESPACE = "lifecycle"  # every task these scripts create lives here


def _tf(name: str) -> str:
    return subprocess.run(
        ["terraform", f"-chdir={INSTALL / 'terraform'}", "output", "-raw", name],
        check=True, capture_output=True, text=True,
    ).stdout


CONTEXT = os.environ.get("AX_CONTEXT") or _tf("cluster_name")
SNAPSHOT_LOCATION = _tf("snapshot_location")
REGION = _tf("region")


def banner(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def run(cmd: list[str], check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if not quiet:
        out = (p.stdout + p.stderr).rstrip()
        if out:
            print(out, flush=True)
    if check and p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}): {' '.join(cmd)}")
    return p


def ax(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    return run([str(AX_BIN), "--context", CONTEXT, "-n", "ax-system", "-a", ATESPACE, *args],
               check=check, quiet=quiet)


def kubectl(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    return run(["kubectl", "--context", CONTEXT, *args], check=check, quiet=quiet)


def timed(label: str, fn, *args, **kwargs):
    """Run fn and print how long it took, in milliseconds."""
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  [{ms:7.0f} ms] {label}", flush=True)
    return result, ms


def summary(label: str, values_ms: list[float]) -> None:
    v = sorted(values_ms)
    print(f"  {label}: n={len(v)} median {statistics.median(v):.0f} ms, "
          f"min {v[0]:.0f}, max {v[-1]:.0f}", flush=True)


def task_manifest(name: str, command: list[str], cpu: str | None = None,
                  memory: str | None = None) -> str:
    limits = ""
    if cpu or memory:
        limits = "  resources:\n    limits:\n"
        if cpu:
            limits += f'      cpu: "{cpu}"\n'
        if memory:
            limits += f'      memory: "{memory}"\n'
    return (
        "apiVersion: ax.io/v1alpha1\nkind: Task\n"
        f"metadata:\n  name: {name}\n  atespace: {ATESPACE}\n"
        f"spec:\n  debug: true\n  command: {json.dumps(command)}\n{limits}"
    )


def create(name: str, command: list[str] | None = None, **kw) -> None:
    manifest = task_manifest(name, command or ["sleep", "infinity"], **kw)
    p = subprocess.run([str(AX_BIN), "--context", CONTEXT, "-n", "ax-system", "-a", ATESPACE,
                        "apply", "-f", "-"], input=manifest, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"apply {name} failed: {p.stderr.strip()}")


def status(name: str) -> dict:
    """Phase, worker IP and Ready reason, parsed from `ax get task` YAML."""
    out = ax("get", "task", name, quiet=True, check=False).stdout
    phase = re.search(r"^\s+phase:\s*(\S+)", out, re.M)
    ip = re.search(r"^\s+workerIP:\s*(\S+)", out, re.M)
    ready = re.search(r"type: Ready\n(?:.*\n)*?\s+reason:\s*(\S+)", out)
    return {"phase": phase and phase[1], "worker": ip and ip[1], "ready": ready and ready[1]}


def sh(name: str, script: str, check: bool = True) -> str:
    """Run a shell snippet inside the task over `ax ssh` and return its output."""
    p = ax("ssh", name, "--", "sh", "-c", script, check=check, quiet=True)
    return (p.stdout + p.stderr).rstrip()


def list_tasks() -> list[str]:
    out = ax("get", "tasks", quiet=True, check=False).stdout.splitlines()
    return [line.split()[0] for line in out[1:] if line.strip()]


def s3_summary() -> str:
    p = run(["aws", "s3", "ls", "--recursive", "--summarize", SNAPSHOT_LOCATION, "--region", REGION],
            quiet=True, check=False)
    lines = p.stdout.strip().splitlines()
    return " | ".join(l.strip() for l in lines[-2:]) if lines else "(empty)"


KATE_BIN = INSTALL / ".src" / "bin" / "kubectl-ate"


def actor(name: str) -> dict:
    """Substrate's own record of the task's actor (AX names the actor after the task)."""
    p = run([str(KATE_BIN), "--context", CONTEXT, "get", "actor", name, "-a", ATESPACE, "-o", "json"],
            quiet=True, check=False)
    return json.loads(p.stdout) if p.returncode == 0 else {}


def worker_node(ip: str | None) -> str:
    if not ip:
        return "-"
    out = kubectl("-n", "ax-system", "get", "pods", "-o", "wide", quiet=True).stdout
    for line in out.splitlines():
        if f" {ip} " in line:
            cols = line.split()
            return f"{cols[0]} on {cols[6]}"
    return ip


def delete(*names: str) -> None:
    for name in names:
        ax("delete", "task", name, check=False, quiet=True)


def stop_tunnel() -> None:
    run([str(AX_BIN), "tunnel", "stop", CONTEXT], check=False, quiet=True)
