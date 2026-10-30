"""Step 5: control-plane throughput. 120 tasks are created, resumed, suspended
and deleted with 20 calls in flight, while the sampler records the CPU and
memory of ax-server, ate-api-server and both PostgreSQL databases. The tasks
are tiny, so the sandboxes themselves are not the bottleneck."""

import time

from common import (delete_all, Sampler, ax, banner, create, describe, parallel, save, timed_call)

delete_all()  # start from an empty atespace

N, CONC = 120, 20
LIMITS = {"cpu": "25m", "memory": "64Mi"}
names = [f"st-c-{i:03d}" for i in range(N)]
phases = {}


def create_one(name):
    t0 = time.perf_counter()
    create(name, **LIMITS)
    return True, (time.perf_counter() - t0) * 1000, ""


with Sampler("control_plane", every=3) as s:
    for verb, fn in (("create", create_one),
                     ("resume", lambda n: timed_call(ax, "resume", "task", n, quiet=True, check=False)),
                     ("suspend", lambda n: timed_call(ax, "suspend", "task", n, quiet=True, check=False)),
                     ("delete", lambda n: timed_call(ax, "delete", "task", n, quiet=True, check=False))):
        banner(f"{verb} {N} tasks, {CONC} at a time")
        t0 = time.time()
        out = parallel(fn, names, CONC)
        wall = time.time() - t0
        ok = [ms for good, ms, _ in out if good]
        errs = [e for good, _, e in out if not good]
        stats = describe(verb, ok)
        print(f"  {len(ok)} ok, {len(errs)} failed, {wall:.1f} s wall, {len(ok) / wall:.1f} {verb}s/s"
              + (f"  first error: {errs[0]}" if errs else ""))
        phases[verb] = {"start": round(t0 - s.t0, 1), "end": round(time.time() - s.t0, 1),
                        "rate": len(ok) / wall, "ms": ok, "errors": len(errs), "stats": stats}
        time.sleep(10)  # a gap between phases in the CPU chart

save("control_plane", phases)
