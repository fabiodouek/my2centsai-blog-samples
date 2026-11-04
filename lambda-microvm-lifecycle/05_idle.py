"""Test 5: idle auto-suspend and auto-resume. With maxIdleDurationSeconds=60: how long until an
idle MicroVM suspends, what auto-resume costs the first request, whether CPU work or outbound-only
traffic counts as activity (it shouldn't, per the docs), whether a held-open stream does, and what
suspendedDurationSeconds does. About 15 minutes, mostly waiting."""

import threading
import time

from common import banner, err, run

IDLE = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 60, "suspendedDurationSeconds": 1800}


def watch_until(vm, want: str, limit: float, since: float) -> float | None:
    """Poll the control plane (which doesn't count as endpoint traffic) until `want`."""
    while time.perf_counter() - since < limit:
        if vm.state() == want:
            return time.perf_counter() - since
        time.sleep(2)
    return None


def auto_resume(vm) -> None:
    t = time.perf_counter()
    r = vm.request("GET", "/state", timeout=120)
    print(f"  request to SUSPENDED MicroVM: HTTP {r.status_code} after {time.perf_counter() - t:.2f}s (auto-resume), "
          f"state now {vm.state()}")


vm = run("sandbox", "05-idle", idle=IDLE)
vm.wait("RUNNING")
vm.ready()

banner("A: no traffic at all")
last = time.perf_counter()
vm.request("GET", "/health")
print(f"  SUSPENDED after {watch_until(vm, 'SUSPENDED', 300, last):.0f}s without endpoint traffic")
auto_resume(vm)
for i in range(3):
    time.sleep(70)
    print(f"  resume #{i + 2}:", end="")
    auto_resume(vm)

banner("B: CPU-bound work, no requests")
vm.exec("nohup python -c \"import time\nwhile True: pass\" >/dev/null 2>&1 &")
last = time.perf_counter()
s = watch_until(vm, "SUSPENDED", 300, last)
print(f"  busy MicroVM SUSPENDED after {s:.0f}s" if s else "  still RUNNING after 300 s")
auto_resume(vm)
vm.exec("pkill -f 'while True: pass' || true")

banner("C: outbound-only traffic (curl every 5 s), no requests")
vm.exec("nohup bash -c 'while true; do curl -s -o /dev/null https://example.com; echo $(date +%s) >> /root/out.log; sleep 5; done' >/dev/null 2>&1 &")
last = time.perf_counter()
s = watch_until(vm, "SUSPENDED", 300, last)
print(f"  outbound-only MicroVM SUSPENDED after {s:.0f}s" if s else "  still RUNNING after 300 s")
auto_resume(vm)
print("  outbound loop lines written:", vm.sh("wc -l < /root/out.log"))
vm.exec("pkill -f 'curl -s -o /dev/null' ; pkill -f out.log || true")

banner("D: one held-open SSE stream for 150 s, no other requests")
states = []
stop = threading.Event()


def poll():
    while not stop.is_set():
        states.append(vm.state())
        time.sleep(5)

threading.Thread(target=poll, daemon=True).start()
n = 0
try:
    for kind, _ in vm.stream("for i in $(seq 1 150); do echo $i; sleep 1; done"):
        n += kind == "line"
except Exception as e:
    print("  stream:", err(e))
stop.set()
print(f"  {n} lines received; states seen while streaming: {sorted(set(states))}")

banner("E: autoResumeEnabled=false, then suspendedDurationSeconds=90")
vm.terminate()
vm2 = run("sandbox", "05-noresume", idle={"autoResumeEnabled": False, "maxIdleDurationSeconds": 60,
                                          "suspendedDurationSeconds": 90})
vm2.wait("RUNNING")
vm2.ready()
last = time.perf_counter()
print(f"  SUSPENDED after {watch_until(vm2, 'SUSPENDED', 300, last):.0f}s")
suspended_at = time.perf_counter()
t = time.perf_counter()
try:
    r = vm2.request("GET", "/health", timeout=60)
    print(f"  request while SUSPENDED: HTTP {r.status_code} after {time.perf_counter() - t:.2f}s, state {vm2.state()}")
except Exception as e:
    print(f"  request while SUSPENDED: {err(e)}")
s = watch_until(vm2, "TERMINATED", 400, suspended_at)
d = vm2.describe()
print(f"  TERMINATED {s:.0f}s after suspending" if s else "  not terminated after 400 s", "|", d.get("terminationMessage") or d.get("stateReason"))
vm2.terminate()
