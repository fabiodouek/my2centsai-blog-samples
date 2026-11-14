"""Step 6: noisy neighbors. Six 'victim' tasks run a fixed CPU benchmark and a
small disk benchmark while other tasks on the same workers burn CPU, fill
memory past their limit, or hammer the disk. How much do the victims slow
down, and does suspend/resume latency for them change?"""

import time

from common import (delete_all, Sampler, ax, banner, create, describe, parallel, save, sh, status, timed_call)

delete_all()  # start from an empty atespace

VICTIMS = [f"st-v-{i}" for i in range(6)]
LIMITS = {"cpu": "250m", "memory": "256Mi"}
# A fixed amount of CPU work (python is in the runner image), and a 64 MB
# write + fsync to /workspace. Both print milliseconds.
BENCH = ("python3 -c \"import time;t=time.perf_counter();sum(i*i for i in range(2_000_000));"
         "print(round((time.perf_counter()-t)*1000))\"; "
         "t=$(date +%s%N); dd if=/dev/zero of=/workspace/io bs=1M count=64 conv=fsync 2>/dev/null; "
         "echo $(( ($(date +%s%N)-t)/1000000 ))")

HOGS = {
    "cpu": (["sh", "-c", "for i in 1 2; do (while :; do :; done) & done; wait"], {"cpu": "1", "memory": "256Mi"}),
    "disk": (["sh", "-c", "while :; do dd if=/dev/zero of=/workspace/fill bs=1M count=512 conv=fsync 2>/dev/null; done"],
             {"cpu": "500m", "memory": "256Mi"}),
    "memory": (["python3", "-c", "b=[]\nwhile True: b.append(bytearray(64*2**20))"], {"cpu": "250m", "memory": "512Mi"}),
}


def bench_round(label):
    out = parallel(lambda v: sh(v, BENCH, check=False), VICTIMS, 6)
    cpu, io = [], []
    for o in out:
        nums = [int(x) for x in o.split() if x.isdigit()]
        if len(nums) >= 2:
            cpu.append(nums[-2]); io.append(nums[-1])
    sus = parallel(lambda v: timed_call(ax, "suspend", "task", v, quiet=True, check=False), VICTIMS, 6)
    res = parallel(lambda v: timed_call(ax, "resume", "task", v, quiet=True, check=False), VICTIMS, 6)
    r = {"cpu_ms": cpu, "io_ms": io, "suspend_ms": [m for ok, m, _ in sus if ok],
         "resume_ms": [m for ok, m, _ in res if ok]}
    print(f"  {label}:")
    for k in ("cpu_ms", "io_ms", "suspend_ms", "resume_ms"):
        describe(f"  {k:10}", r[k])
    return r


results = {}
with Sampler("noisy", every=3):
    banner("6 victims (cpu 250m, memory 256Mi)")
    parallel(lambda v: create(v, **LIMITS), VICTIMS, 6)
    parallel(lambda v: ax("resume", "task", v, quiet=True), VICTIMS, 6)
    results["baseline"] = bench_round("baseline, nothing else running")

    for kind, (cmd, lim) in HOGS.items():
        banner(f"add 4 {kind} hogs")
        hogs = [f"st-h{kind[0]}-{i}" for i in range(4)]
        parallel(lambda h: create(h, cmd, **lim), hogs, 4)
        parallel(lambda h: ax("resume", "task", h, quiet=True, check=False), hogs, 4)
        time.sleep(20)
        print("  hogs:", {h: status(h)["phase"] for h in hogs})
        results[kind] = bench_round(f"with {kind} hogs")
        if kind == "memory":
            print("  memory hogs after the round:", {h: status(h) for h in hogs})
        delete_all(f"st-h{kind[0]}-")

delete_all("st-v-")
save("noisy", results)
