"""Test 7: density. How many running tasks fit on two gVisor workers (one per
4 vCPU / 16 GiB node), and what decides it?

Substrate books each task's CPU and memory limits against its worker's
capacity. AX gives a task without limits 1 CPU and 512 MiB. Run with the
cluster otherwise idle (09_cleanup.py first)."""

import collections
import concurrent.futures as cf
import json

from common import ax, banner, create, kubectl, list_tasks, status, summary, timed, worker_node


def wave(prefix: str, n: int, **limits) -> list[str]:
    names = [f"{prefix}-{i:02d}" for i in range(1, n + 1)]
    with cf.ThreadPoolExecutor(10) as pool:
        list(pool.map(lambda nm: create(nm, **limits), names))

    def resume(name):
        p, ms = timed(f"resume {name}", ax, "resume", "task", name, check=False, quiet=True)
        return name, p.returncode, (p.stdout + p.stderr).strip(), ms

    with cf.ThreadPoolExecutor(10) as pool:
        results = list(pool.map(resume, names))
    ok = [r for r in results if r[1] == 0]
    failed = [r for r in results if r[1] != 0]
    summary("resume (10 at a time)", [r[3] for r in ok])
    print(f"  running: {len(ok)}, refused: {len(failed)}")
    if failed:
        print("  refused with:", failed[0][2].split("desc = ")[-1])
    placement = collections.Counter(worker_node(status(r[0])["worker"]) for r in ok)
    for where, count in sorted(placement.items()):
        print(f"  {count:3d} on {where}")
    return [r[0] for r in ok]


banner("node capacity")
kubectl("get", "nodes", "-o", "custom-columns=NODE:.metadata.name,CPU:.status.allocatable.cpu,"
        "MEMORY:.status.allocatable.memory")

banner("wave 1: 12 tasks at AX's default size (1 CPU, 512 MiB)")
wave("lc-default", 12)

banner("delete them")
with cf.ThreadPoolExecutor(10) as pool:
    list(pool.map(lambda nm: ax("delete", "task", nm, quiet=True, check=False),
                  [t for t in list_tasks() if t.startswith("lc-default")]))

banner("wave 2: 40 tasks at cpu 250m, memory 256Mi")
running = wave("lc-small", 40, cpu="250m", memory="256Mi")

banner(f"what {len(running)} running sandboxes use (kubelet stats summary)")
for node in kubectl("get", "nodes", "-o", "name", quiet=True).stdout.split():
    node = node.split("/")[1]
    stats = json.loads(kubectl("get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary", quiet=True).stdout)
    print(f"  {node}: node working set {stats['node']['memory']['workingSetBytes'] / 2**30:.1f} GiB")
    for pod in stats["pods"]:
        if pod["podRef"]["name"].startswith("ax-workers-"):
            ws = sum(c.get("memory", {}).get("workingSetBytes", 0) for c in pod.get("containers", []))
            print(f"      {pod['podRef']['name']}: {ws / 2**30:.2f} GiB for its sandboxes")

banner("delete them")
with cf.ThreadPoolExecutor(10) as pool:
    list(pool.map(lambda nm: ax("delete", "task", nm, quiet=True, check=False),
                  [t for t in list_tasks() if t.startswith("lc-small")]))
