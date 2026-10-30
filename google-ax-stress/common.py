"""Shared helpers for the AX stress-test scripts.

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
ATESPACE = "stress"  # every task these scripts create lives here


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


# --- stress-test helpers -----------------------------------------------------

import concurrent.futures as cf
import csv
import threading

RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    v = sorted(values)
    if not v:
        return float("nan")
    k = max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.5)) - 1))
    return v[k]


def describe(label: str, values_ms: list[float]) -> dict:
    d = {"n": len(values_ms), "p50": pct(values_ms, 50), "p95": pct(values_ms, 95),
         "p99": pct(values_ms, 99), "max": max(values_ms) if values_ms else float("nan")}
    print(f"  {label}: n={d['n']} p50 {d['p50']:.0f} ms, p95 {d['p95']:.0f}, p99 {d['p99']:.0f}, "
          f"max {d['max']:.0f}", flush=True)
    return d


def timed_call(fn, *args, **kwargs) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    p = fn(*args, **kwargs)
    ms = (time.perf_counter() - t0) * 1000
    ok = getattr(p, "returncode", 0) == 0
    err = "" if ok else (p.stdout + p.stderr).strip().split("desc = ")[-1][:120]
    return ok, ms, err


def parallel(fn, items, workers: int):
    with cf.ThreadPoolExecutor(max(1, workers)) as pool:
        return list(pool.map(fn, items))


def save(name: str, data) -> None:
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=1))


def delete_names(names) -> None:
    parallel(lambda n: ax("delete", "task", n, quiet=True, check=False), list(names), 10)


def delete_all(prefix: str = "") -> None:
    """`ax get tasks` returns at most 50 tasks (AX's ListTasks default, and the
    CLI has no flag for more), so list and delete until nothing is left."""
    while True:
        names = [t for t in list_tasks() if t.startswith(prefix)]
        if not names:
            return
        delete_names(names)


class Sampler:
    """Polls each node's kubelet stats summary every `every` seconds and writes
    per-pod and per-node CPU (millicores) and memory (MiB) to results/<name>.csv.
    Substrate runs runsc inside its worker pods, so a worker pod's numbers are
    the sum of the sandboxes it hosts."""

    PODS = ("ax-workers-", "ax-server-", "ax-postgres-", "ate-api-server-", "postgres-",
            "atelet-", "atenet-egress-", "atenet-router-", "ate-controller-")

    def __init__(self, name: str, every: float = 5.0):
        self.path = RESULTS / f"{name}.csv"
        self.every, self._stop = every, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self.t0 = time.time()

    def _run(self):
        nodes = [n.split("/")[1] for n in kubectl("get", "nodes", "-o", "name", quiet=True).stdout.split()]
        with open(self.path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "node", "pod", "cpu_m", "mem_mib"])
            while not self._stop.is_set():
                t = round(time.time() - self.t0, 1)
                for node in nodes:
                    p = kubectl("get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary",
                                quiet=True, check=False)
                    if p.returncode:
                        continue
                    s = json.loads(p.stdout)
                    nd = s["node"]
                    w.writerow([t, node, "_node", nd["cpu"].get("usageNanoCores", 0) / 1e6,
                                nd["memory"].get("workingSetBytes", 0) / 2**20])
                    for pod in s["pods"]:
                        name = pod["podRef"]["name"]
                        if name.startswith(self.PODS):
                            cpu = sum(c.get("cpu", {}).get("usageNanoCores", 0) for c in pod.get("containers", []))
                            mem = sum(c.get("memory", {}).get("workingSetBytes", 0) for c in pod.get("containers", []))
                            w.writerow([t, node, name, cpu / 1e6, mem / 2**20])
                f.flush()
                self._stop.wait(self.every)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        print(f"  (metrics: {self.path.relative_to(HERE)})")
