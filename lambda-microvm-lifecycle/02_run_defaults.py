"""Test 2: run a MicroVM and inspect the defaults. What you get when you omit every optional
parameter, time to RUNNING and to the first response over N sequential runs, what the guest
reports, and whether the 4x burst above the 2 GB / 1 vCPU baseline is real."""

import statistics
import sys
import time

from common import VM, banner, image_arn, mvm, redact, run, show, timed

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5

banner("run-microvm with only --image-identifier")
with timed("run_microvm (API call)"):
    r = mvm.run_microvm(imageIdentifier=image_arn("sandbox"))
vm = VM(r["microvmId"], r["endpoint"], 0)
try:
    print("response:", redact({k: v for k, v in r.items() if k != "ResponseMetadata"}))
    vm.wait("RUNNING")
    show(vm.describe())
finally:
    # This run has Lambda's defaults (8-hour cap, no idle policy) instead of common.run()'s safety
    # nets, so terminate it even if the script fails here.
    vm.terminate()

banner(f"{N} sequential runs: time to RUNNING and to first 200")
to_running, to_first = [], []
for i in range(N):
    vm = run("sandbox", "02-seq")
    vm.wait("RUNNING", poll=0.1)
    running = time.perf_counter() - vm.t0  # measured from just before the run_microvm call
    to_running.append(running)
    first = vm.ready()
    to_first.append(first)
    print(f"run {i + 1}: {vm.id}  RUNNING after {running:.2f}s  first 200 after {first:.2f}s")
    if i < N - 1:
        vm.terminate()
print(f"median: RUNNING {statistics.median(to_running):.2f}s | first 200 {statistics.median(to_first):.2f}s "
      f"(min {min(to_first):.2f}, max {max(to_first):.2f})")

banner("Inside the guest")
probes = {
    "user": "id",
    "kernel": "uname -srm",
    "container os": ". /etc/os-release; echo $PRETTY_NAME",
    "cpu": "nproc; grep -m1 -E 'CPU part|model name' /proc/cpuinfo; lscpu 2>/dev/null | grep -E 'Model name|Vendor' || true",
    "memory": "grep -E 'MemTotal|MemAvailable' /proc/meminfo",
    "cgroup limits": "cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max 2>/dev/null || echo 'no cgroup v2 limits visible'",
    "disk": "df -h / /tmp | tail -n +2",
    "pid 1": "ps -o pid,user,cmd -p 1; ps aux | wc -l",
    "capabilities": "grep -E 'CapEff|CapBnd' /proc/self/status",
    "network": "ip -4 -o addr | awk '{print $2, $4}'; cat /etc/resolv.conf | grep nameserver",
    "env (names)": "env | cut -d= -f1 | grep -E '^AWS|^LAMBDA' | sort",
}
for label, cmd in probes.items():
    print(f"-- {label}\n{vm.sh(cmd)}")

banner("Burst: 4x the baseline")
print(vm.sh("python -c \"import os; n=os.cpu_count(); print('cpu_count', n)\""))
with timed("allocate and touch 6 GiB (baseline is 2 GB, peak 8 GB)"):
    print(vm.sh("python -c \"b=bytearray(6*1024**3); b[::4096]=b'x'*(len(b)//4096); print('ok', len(b)//1024**3, 'GiB')\" 2>&1 | tail -1", timeout=180))
print(vm.sh("grep -E 'MemTotal|MemAvailable' /proc/meminfo"))
with timed("4 CPU-bound workers for 10 s"):
    print(vm.sh("python - <<'EOF'\nimport multiprocessing as mp, time\n"
                "def spin(_):\n    t=time.time(); n=0\n    while time.time()-t<10: n+=1\n    return n\n"
                "with mp.Pool(4) as p: r=p.map(spin, range(4))\n"
                "print('loops per worker (millions):', [round(x/1e6,1) for x in r])\nEOF", timeout=60))
with timed("1 CPU-bound worker for 10 s (reference)"):
    print(vm.sh("python - <<'EOF'\nimport time\ndef spin():\n    t=time.time(); n=0\n    while time.time()-t<10: n+=1\n    return n\n"
                "print('loops (millions):', round(spin()/1e6,1))\nEOF", timeout=60))
vm.terminate()
