"""Test 6: maximum duration and termination. Does suspended time count toward
maximumDurationInSeconds? What does a MicroVM report when it hits the cap, and does the
/terminate hook run (on the cap, on an explicit terminate, and when terminated while suspended)?"""

import time

from common import banner, mvm, run, runtime_logs

CAP = 180
LONG_SUSPEND = {"autoResumeEnabled": False, "maxIdleDurationSeconds": 3600, "suspendedDurationSeconds": 3600}


def until_terminated(vm, since: float, limit: float = 600) -> float | None:
    while time.perf_counter() - since < limit:
        if vm.state() == "TERMINATED":
            return time.perf_counter() - since
        time.sleep(2)
    return None


def hooks_in_logs(vm, since_ms):
    lines = [l for l in runtime_logs("sandbox", vm.id, since_ms) if '"hook"' in l or "hook" in l]
    return lines[-6:]


since_ms = int(time.time() * 1000) - 60_000
banner(f"A: maximumDurationInSeconds={CAP}, always running")
a = run("sandbox", "06-cap-running", idle=LONG_SUSPEND, max_duration=CAP)
a.wait("RUNNING")

banner(f"B: maximumDurationInSeconds={CAP}, suspended at ~30 s and left suspended")
b = run("sandbox", "06-cap-suspended", idle=LONG_SUSPEND, max_duration=CAP)
b.wait("RUNNING")
b.ready()
time.sleep(max(0, 30 - (time.perf_counter() - b.t0)))
mvm.suspend_microvm(microvmIdentifier=b.id)
b.wait("SUSPENDED")
print(f"  B suspended at {time.perf_counter() - b.t0:.0f}s")

ta = until_terminated(a, a.t0)
tb = until_terminated(b, b.t0)
for name, vm, t in (("A running", a, ta), ("B suspended", b, tb)):
    d = vm.describe()
    print(f"{name}: TERMINATED {t:.0f}s after run" if t else f"{name}: not terminated within 600 s",
          "|", d.get("terminationMessage") or d.get("stateReason"))

banner("C: explicit terminate of a RUNNING MicroVM")
c = run("sandbox", "06-terminate")
c.wait("RUNNING")
c.ready()
t = time.perf_counter()
c.terminate()
c.wait("TERMINATED", poll=0.2)
print(f"  terminate -> TERMINATED in {time.perf_counter() - t:.2f}s |", c.describe().get("terminationMessage"))

banner("D: terminate while SUSPENDED")
d = run("sandbox", "06-terminate-suspended", idle=LONG_SUSPEND)
d.wait("RUNNING")
d.ready()
mvm.suspend_microvm(microvmIdentifier=d.id)
d.wait("SUSPENDED")
t = time.perf_counter()
d.terminate()
d.wait("TERMINATED", poll=0.2)
print(f"  terminate (suspended) -> TERMINATED in {time.perf_counter() - t:.2f}s |", d.describe().get("terminationMessage"))

banner("Hook calls in CloudWatch (agent stdout)")
time.sleep(20)  # log delivery
for name, vm in (("A cap while running", a), ("B cap while suspended", b), ("C explicit", c), ("D while suspended", d)):
    print(f"-- {name} ({vm.id})")
    for line in hooks_in_logs(vm, since_ms):
        print("   ", line[:160])
