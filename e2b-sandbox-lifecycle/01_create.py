"""Test 1: create a sandbox with no options and inspect the defaults, then time a few sequential creates."""

import statistics
import sys

from e2b import Sandbox

from common import banner, tagged, timed

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

banner("Create with defaults")
with timed("Sandbox.create()"):
    sbx = Sandbox.create(metadata=tagged(test="01"))

info = sbx.get_info()
print(f"sandbox_id : {info.sandbox_id}")
print(f"template   : {info.name} ({info.template_id})")
print(f"state      : {info.state}")
print(f"cpu / mem  : {info.cpu_count} vCPU / {info.memory_mb} MiB")
print(f"lifetime   : {(info.end_at - info.started_at).total_seconds():.0f}s")
print(f"lifecycle  : {info.lifecycle}")
print(f"envd       : {info.envd_version}")

result = sbx.commands.run("uname -r && nproc && free -m | head -2 && df -h / | tail -1")
print(result.stdout)
sbx.kill()

banner(f"{RUNS} sequential creates")
timings = []
for i in range(RUNS):
    results = {}
    with timed(f"create #{i + 1}", results):
        s = Sandbox.create(metadata=tagged(test="01"))
    timings.append(results[f"create #{i + 1}"])
    s.kill()

print(f"median {statistics.median(timings):.2f}s | min {min(timings):.2f}s | max {max(timings):.2f}s")
