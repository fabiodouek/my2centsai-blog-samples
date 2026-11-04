"""Test 4: suspend and resume three times. Latency of each step, what survives (files on disk and
in /tmp, a background process, the agent's in-memory state, an open TCP connection), what the
clock does, which hooks run, and what suspend/resume do in the wrong state."""

import statistics
import time

from common import banner, err, mvm, run, timed

COUNTER = "nohup bash -c 'i=0; while true; do i=$((i+1)); echo $i > /root/counter; sleep 1; done' >/dev/null 2>&1 &"
CHECK = ("cat /root/state.txt; cat /tmp/state.txt 2>/dev/null || echo '/tmp/state.txt missing'; "
         "pgrep -f 'echo \\$i > /root/counter' >/dev/null && echo process-alive || echo process-gone; "
         "echo uptime $(cut -d' ' -f1 /proc/uptime)s")

vm = run("sandbox", "04-suspend", idle={"autoResumeEnabled": False, "maxIdleDurationSeconds": 3600,
                                        "suspendedDurationSeconds": 1800})
vm.wait("RUNNING")
vm.ready()
vm.exec("echo disk survives > /root/state.txt; echo tmp survives > /tmp/state.txt")
vm.exec(COUNTER)
print("tcp/open:", vm.request("POST", "/tcp/open", json={"host": "example.com"}).json())
time.sleep(3)

suspends, resumes, firsts = [], [], []
for cycle in range(1, 4):
    banner(f"Cycle {cycle}")
    before = vm.agent_state()
    file_before = vm.sh("cat /root/counter")
    guest_before = float(vm.sh("date +%s.%N"))
    r = {}
    with timed("suspend_microvm until SUSPENDED", r):
        mvm.suspend_microvm(microvmIdentifier=vm.id)
        vm.wait("SUSPENDED", poll=0.1)
    suspends.append(r["suspend_microvm until SUSPENDED"])
    time.sleep(20)  # stay suspended for a while
    with timed("resume_microvm until RUNNING", r):
        mvm.resume_microvm(microvmIdentifier=vm.id)
        vm.wait("RUNNING", poll=0.1)
    resumes.append(r["resume_microvm until RUNNING"])
    t = time.perf_counter()
    vm.request("GET", "/health")
    firsts.append(time.perf_counter() - t)
    after = vm.agent_state()
    file_after = vm.sh("cat /root/counter")
    guest_after, local_now = float(vm.sh("date +%s.%N")), time.time()
    print(f"agent pid {before['pid']} -> {after['pid']} | in-memory counter {before['counter']} -> {after['counter']} "
          f"| file counter {file_before} -> {file_after}")
    print(f"guest clock advanced {guest_after - guest_before:.1f}s; guest vs laptop now: {guest_after - local_now:+.2f}s")
    print(vm.sh(CHECK).replace("\n", " | "))
    print("held TCP connection:", vm.request("GET", "/tcp/check").json())
    print("new outbound request:", vm.sh("curl -s -o /dev/null -w '%{http_code}' -m 8 https://example.com"))
    vm.request("POST", "/tcp/open", json={"host": "example.com"})  # re-open for the next cycle

print(f"\nmedian: suspend {statistics.median(suspends):.2f}s | resume {statistics.median(resumes):.2f}s | "
      f"first request after RUNNING {statistics.median(firsts) * 1000:.0f} ms")
print("hooks seen:", [e["hook"] for e in vm.events()])

banner("Wrong-state calls")
for label, fn in (("resume a RUNNING MicroVM", mvm.resume_microvm), ("suspend twice", None)):
    try:
        if fn:
            r = fn(microvmIdentifier=vm.id)
            print(f"{label}: HTTP {r['ResponseMetadata']['HTTPStatusCode']}, state {vm.state()}")
        else:
            mvm.suspend_microvm(microvmIdentifier=vm.id)
            r = mvm.suspend_microvm(microvmIdentifier=vm.id)
            print(f"{label}: HTTP {r['ResponseMetadata']['HTTPStatusCode']}, state {vm.state()}")
    except Exception as e:
        print(f"{label}: {err(e)}")
vm.wait("SUSPENDED")
banner("A request to a SUSPENDED MicroVM with autoResumeEnabled=false")
t = time.perf_counter()
try:
    r = vm.request("GET", "/health", timeout=60)
    print(f"status {r.status_code} after {time.perf_counter() - t:.2f}s: {r.text[:120]!r}; state now {vm.state()}")
except Exception as e:
    print(f"{err(e)} after {time.perf_counter() - t:.2f}s; state now {vm.state()}")
vm.terminate()
