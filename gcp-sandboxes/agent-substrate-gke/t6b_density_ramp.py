"""Step 2: density ceiling. Add idle tasks in batches of 10 until Substrate
refuses them, and record what each step costs.

Tasks run `sleep infinity` with cpu 50m / memory 64Mi, the smallest sandbox
that still boots AX's runner comfortably. Substrate books each task's limits
against its worker's capacity, which for a pool with no declared capacity is
the node's allocatable CPU (3.92 CPU on an m7i.xlarge)."""

import json

from common import (delete_all, Sampler, ax, banner, create, describe, kubectl, parallel, save, status, timed_call)

delete_all()  # start from an empty atespace

BATCH, MAX = 10, 200
LIMITS = {"cpu": "50m", "memory": "64Mi"}


def node_mem() -> dict:
    out = {}
    for node in kubectl("get", "nodes", "-o", "name", quiet=True).stdout.split():
        node = node.split("/")[1]
        s = json.loads(kubectl("get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary", quiet=True).stdout)
        out[node] = round(s["node"]["memory"]["workingSetBytes"] / 2**30, 2)
    return out


steps, running = [], 0
banner(f"ramp: batches of {BATCH} idle tasks at cpu {LIMITS['cpu']}, memory {LIMITS['memory']}")
with Sampler("density"):
    for start in range(1, MAX + 1, BATCH):
        names = [f"st-d-{i:03d}" for i in range(start, start + BATCH)]
        parallel(lambda n: create(n, **LIMITS), names, 10)
        res = parallel(lambda n: timed_call(ax, "resume", "task", n, quiet=True, check=False), names, 10)
        ok = [r for r in res if r[0]]
        running += len(ok)
        mem = node_mem()
        step = {"running": running, "batch_ok": len(ok), "resume_ms": [r[1] for r in ok],
                "node_mem_gib": mem, "refused": [r[2] for r in res if not r[0]][:1]}
        steps.append(step)
        p50 = sorted(step["resume_ms"])[len(ok) // 2] if ok else float("nan")
        print(f"  {running:4d} running  (+{len(ok):2d}/{BATCH})  resume p50 {p50:5.0f} ms  "
              f"node memory {' / '.join(f'{v:.1f}' for v in mem.values())} GiB", flush=True)
        if len(ok) < BATCH:
            print("  refused:", step["refused"][0] if step["refused"] else "?")
            break

banner("where they are")
placement = {}
for i in range(1, running + 1, 7):  # sample, ax get per task is slow
    w = status(f"st-d-{i:03d}")["worker"]
    placement[w] = placement.get(w, 0) + 1
print("  sampled placement by worker IP:", placement)
describe("all resumes", [ms for s in steps for ms in s["resume_ms"]])
save("density", steps)

banner("delete them")
delete_all("st-d-")
