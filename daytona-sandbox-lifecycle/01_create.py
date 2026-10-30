"""Test 1: create a sandbox with no options and inspect the defaults, then time a few sequential creates."""

import statistics
import sys

from daytona import CreateSandboxFromSnapshotParams, Daytona

from common import SAFETY_TTL, banner, labels, timed

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
daytona = Daytona()

banner("Create with defaults")
with timed("daytona.create()"):
    sbx = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="01"), ttl_minutes=SAFETY_TTL))

print(f"id         : {sbx.id}")
print(f"snapshot   : {sbx.snapshot} ({sbx.sandbox_class})")
print(f"region     : {sbx.target}")
print(f"state      : {sbx.state}")
print(f"cpu/mem/disk: {sbx.cpu} vCPU / {sbx.memory} GiB / {sbx.disk} GiB")
print(f"auto-stop  : {sbx.auto_stop_interval} min | auto-archive: {sbx.auto_archive_interval} min "
      f"| auto-delete: {sbx.auto_delete_interval} | auto-pause: {sbx.auto_pause_interval}")

r = sbx.process.exec(
    "uname -r; whoami; nproc; cat /sys/fs/cgroup/cpu.max; "
    "free -m | sed -n 2p; cat /sys/fs/cgroup/memory.max; df -h / | tail -1"
)
print(r.result)
sbx.delete()

banner(f"{RUNS} sequential creates")
timings = []
for i in range(RUNS):
    results = {}
    with timed(f"create #{i + 1}", results):
        s = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="01"), ttl_minutes=SAFETY_TTL))
    timings.append(results[f"create #{i + 1}"])
    s.delete()

print(f"median {statistics.median(timings):.2f}s | min {min(timings):.2f}s | max {max(timings):.2f}s")
