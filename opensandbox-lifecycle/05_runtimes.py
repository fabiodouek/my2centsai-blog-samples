"""The same sandbox under the server's current runtime: what it sees, and how long a create takes.

The runtime is server-wide ([secure_runtime] in config.toml), so this script does not choose it:
run it, switch, run it again.

  uv run python 05_runtimes.py          # runc, the default
  ./switch_runtime.sh kata
  uv run python 05_runtimes.py          # kata-qemu: a VM per sandbox
  ./switch_runtime.sh runc              # parts 4 and 5 expect runc

Each run creates CREATES sandboxes one after the other (default 3) and times each create (it
returns when the sandbox answers its health check). In the first one it reads the kernel, the
CPUs and memory it sees, the machine it thinks it runs on, how long that "machine" has been up,
and the number of device nodes; kubectl adds the pod's runtimeClassName and overhead. Each run
appends one line to results/runtimes.jsonl, and the script prints every runtime it has a line for,
so the second run shows the comparison. It deletes only its own sandboxes.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import timedelta

from opensandbox import SandboxSync

from common import (
    HERE,
    SANDBOX_NAMESPACE,
    connect_server,
    connection,
    delete_all_sandboxes,
    heading,
    image,
    kubectl,
    own_metadata,
)

SCRIPT = "05_runtimes"
CREATES = int(os.environ.get("CREATES", "3"))
RESULTS = HERE / "results" / "runtimes.jsonl"
# Runs inside the sandbox (python:3.12-slim has python3, not always the usual tools).
PROBE = r"""
import json, os
def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return "n/a"
mem = next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]
cpuinfo = open("/proc/cpuinfo").read()
print(json.dumps({
    "kernel": os.uname().release,
    "nproc": len(os.sched_getaffinity(0)),
    "cpu_model": next((l.split(":", 1)[1].strip() for l in cpuinfo.splitlines() if l.startswith("model name")), "?"),
    "mem_gib": round(int(mem) / 1024 / 1024, 1),
    "cgroup_cpu_max": read("/sys/fs/cgroup/cpu.max"),
    "machine": read("/sys/class/dmi/id/sys_vendor") + " / " + read("/sys/class/dmi/id/product_name"),
    "uptime_s": float(read("/proc/uptime").split()[0]),
    "devices": len(os.listdir("/dev")),
}))
"""


def server_runtime() -> str:
    """The runtime the server runs with, from ./server_config.py --show --json (reads the ConfigMap)."""
    done = subprocess.run([sys.executable, str(HERE / "server_config.py"), "--show", "--json"],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        sys.exit(f"server_config.py --show failed: {done.stderr.strip()[:200]}")
    return json.loads(done.stdout)["runtime"]


def human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h {rest // 60:02d} min" if hours else f"{rest // 60} min"


def main() -> int:
    connect_server()
    runtime = server_runtime()
    meta = own_metadata(SCRIPT)
    print(f"server runtime: {runtime}   (sandboxes tagged run={meta['run']})")
    heading(f"{CREATES} creates under {runtime}")
    times: list[float] = []
    facts: dict = {}
    pod: dict = {}
    failures = 0
    try:
        for n in range(1, CREATES + 1):
            started = time.monotonic()
            sb = SandboxSync.create(image(), timeout=timedelta(minutes=10), ready_timeout=timedelta(seconds=180),
                                    metadata=meta, connection_config=connection("proxy"))
            took = time.monotonic() - started
            times.append(took)
            node = kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", f"{sb.id}-0", "-o", "jsonpath={.spec.nodeName}",
                           check=False).strip()
            print(f"  create {n}: {took:5.1f} s   on {node or '?'}", flush=True)
            if n == 1:
                execution = sb.commands.run(f"python3 -c '{PROBE}'")
                out = "".join(part.text for part in execution.logs.stdout).strip()
                try:
                    facts = json.loads(out.splitlines()[-1])
                except (ValueError, IndexError):
                    print(f"  FAIL  the probe printed {out[:200]!r}")
                    failures += 1
                pod = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", f"{sb.id}-0", "-o", "json"))
                node_kernel = kubectl("get", "node", node, "-o", "jsonpath={.status.nodeInfo.kernelVersion}",
                                      check=False).strip()
                facts["node_kernel"] = node_kernel
            sb.kill()
            sb.close()
    except Exception as error:  # the SDK or kubectl gave up: one line, then clean up
        print(f"  FAIL  {type(error).__name__}: {str(error)[:200]}")
        failures += 1
    finally:
        # Each sandbox was killed after its create; this catches any still listed (terminating, or left
        # by an error) and returns once none of this run is listed.
        delete_all_sandboxes(metadata={"run": meta["run"]})
        print("  clean up: no sandbox of this run is left")

    if facts:
        spec = pod.get("spec", {})
        limits = (spec.get("containers") or [{}])[0].get("resources", {}).get("limits", {})
        heading(f"what the sandbox sees under {runtime}")
        rows = [
            ("pod runtimeClassName", spec.get("runtimeClassName") or "(none: runc)"),
            ("pod overhead", ", ".join(f"{k} {v}" for k, v in (spec.get("overhead") or {}).items()) or "(none)"),
            ("pod limits", f"cpu {limits.get('cpu', '?')}, memory {limits.get('memory', '?')}"),
            ("kernel (uname -r)", f"{facts['kernel']}   (node: {facts['node_kernel']})"),
            ("CPUs it can use", f"{facts['nproc']}   cgroup cpu.max: {facts['cgroup_cpu_max']}"),
            ("CPU model", facts["cpu_model"]),
            ("memory (MemTotal)", f"{facts['mem_gib']} GiB"),
            ("machine (DMI)", facts["machine"]),
            ("its kernel has been up", human(facts["uptime_s"])),
            ("entries in /dev", str(facts["devices"])),
        ]
        for label, value in rows:
            print(f"  {label:<24} {value}")
        if facts["kernel"] == facts["node_kernel"]:
            print("  -> the node's own kernel: the sandbox is a process on the node, like any container")
        else:
            print("  -> its own kernel, not the node's: the sandbox runs in a virtual machine")

    if times:
        RESULTS.parent.mkdir(exist_ok=True)
        with RESULTS.open("a") as out:
            out.write(json.dumps({"runtime": runtime, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "creates_s": [round(t, 2) for t in times], **facts}) + "\n")
    runs: dict[str, dict] = {}
    if RESULTS.is_file():
        for line in RESULTS.read_text().splitlines():
            record = json.loads(line)
            runs[record["runtime"]] = record  # the latest run per runtime
    if runs:
        heading("every runtime measured so far (latest run of each, results/runtimes.jsonl)")
        print(f"  {'runtime':<11}{'create median':>14}{'creates (s)':>22}   {'kernel':<34}{'CPUs':>5}{'memory':>10}")
        for name, record in runs.items():
            creates = record["creates_s"]
            print(f"  {name:<11}{statistics.median(creates):>12.1f} s{' / '.join(f'{c:.1f}' for c in creates):>22}   "
                  f"{record.get('kernel', '?'):<34}{record.get('nproc', '?'):>5}{record.get('mem_gib', '?'):>7} GiB")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
