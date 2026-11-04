"""Test 8: many MicroVMs from one snapshot (the closest thing to a fork), and a custom image vs
installing at runtime. What every MicroVM from one image shares (process memory, Python's random
state, build-time UUIDs) and what's unique, then a data-science image vs `pip install`."""

import time

from common import banner, build_image, run, timed

N = 3

banner(f"{N} MicroVMs from one image, back to back")
t0 = time.perf_counter()
vms = [run("sandbox", f"08-clone-{i}") for i in range(N)]
for vm in vms:
    vm.wait("RUNNING")
    vm.ready()
print(f"all {N} answering after {time.perf_counter() - t0:.2f}s (RunMicrovm quota on this account: 1/s)")
states = [vm.agent_state() for vm in vms]
fields = ["pid", "build_pid", "build_uuid", "run_uuid", "random_random", "uuid4_now", "boot_id", "counter", "uptime_s"]
print(f"{'field':14}" + "".join(f"{'vm' + str(i):>40}" for i in range(N)))
for f in fields:
    vals = [s[f] for s in states]
    same = "  <- same in every VM" if len({str(v) for v in vals}) == 1 else ""
    print(f"{f:14}" + "".join(f"{str(v)[:38]:>40}" for v in vals) + same)
print("secs since the build:", [round(s["now"] - s["build_time"]) for s in states])
for vm in vms:
    vm.terminate()
for vm in vms:
    vm.wait("TERMINATED", timeout=120)

banner("Custom image vs installing at runtime")
b = build_image("datasci", environmentVariables={"VALIDATE_CMD": ""})
cold_version = b["image"].get("latestActiveImageVersion")
print(f"datasci image build: {b['seconds']}s, states {b['states']}, version {cold_version}")
IMPORT = "python -c 'import time; t=time.time(); import pandas, numpy, sklearn, matplotlib; print(round(time.time()-t,2))'"

ds = run("datasci", "08-datasci")
ds.wait("RUNNING")
with timed("datasci MicroVM: run -> first response"):
    ds.ready()
print("first import in datasci MicroVM (s):", ds.sh(IMPORT))
print("second import (s):", ds.sh(IMPORT))
ds.terminate()

plain = run("sandbox", "08-runtime")
plain.wait("RUNNING")
plain.ready()
with timed("pip install pandas numpy scikit-learn matplotlib at runtime"):
    out = plain.exec("pip install -q pandas==2.3.3 numpy==2.3.4 scikit-learn==1.7.2 matplotlib==3.10.7 2>&1 | tail -2", timeout=600)
print(out["stdout"][-300:], out["stderr"][-300:])
print("first import after install (s):", plain.sh(IMPORT))
plain.terminate()

banner("Same image, but /validate imports the stack (a prefetch hint)")
# The snapshot's disk is fetched lazily; Lambda prefetches what the /validate request touches.
# The agent runs VALIDATE_CMD (an image environment variable) inside /validate.
b = build_image("datasci", environmentVariables={"VALIDATE_CMD": "python -c 'import pandas, numpy, sklearn, matplotlib'"})
print(f"rebuild with VALIDATE_CMD: {b['seconds']}s, version {b['image'].get('latestActiveImageVersion')}")
for i in range(3):
    warm = run("datasci", f"08-warm-{i}")
    warm.wait("RUNNING")
    warm.ready()
    print(f"warm run {i}: first import {warm.sh(IMPORT)} s | second {warm.sh(IMPORT)} s")
    warm.terminate()
    warm.wait("TERMINATED", timeout=120)

banner(f"The version without the warm-up again ({cold_version}, pinned), for comparison")
for i in range(2):
    cold = run("datasci", f"08-cold-{i}", version=cold_version)
    cold.wait("RUNNING")
    cold.ready()
    print(f"cold run {i}: first import {cold.sh(IMPORT)} s")
    cold.terminate()
    cold.wait("TERMINATED", timeout=120)
