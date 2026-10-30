"""Test 1: create a sandbox with no options and inspect the defaults, then time a few sequential creates."""

import statistics
import sys
import time

from common import client, create, name, rest, banner, timed, SAFETY_DELETE_AFTER_STOP, SAFETY_IDLE_TTL

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

banner("Quota before we start (GET /usage)")
u = rest("GET", "/usage")
print(f"sandboxes {u['sandboxes_used']}/{u['sandboxes_limit']} | cpu {u['cpu_used_millicores']}/{u['cpu_limit_millicores']} m"
      f" | memory {u['memory_used_bytes'] / 1024**3:.0f}/{u['memory_limit_bytes'] / 1024**3:.0f} GiB | snapshots {u['snapshots_count']}")

banner("Create with defaults (SDK create_sandbox)")
with timed("client.create_sandbox()"):
    sb = client.create_sandbox(name=name("01"), idle_ttl_seconds=SAFETY_IDLE_TTL,
                               delete_after_stop_seconds=SAFETY_DELETE_AFTER_STOP)
try:
    raw = rest("GET", f"/boxes/{sb.name}")
    print(f"name       : {sb.name} (id {sb.id})")
    print(f"status     : {sb.status}")
    print(f"vcpus      : {sb.vcpus} (cpu_millicores {raw['cpu_millicores']}, REST only)")
    print(f"mem / disk : {sb.mem_bytes / 1024**3:.0f} GiB / {sb.fs_capacity_bytes / 1024**3:.0f} GiB")
    print(f"snapshot   : {sb.snapshot_id}")
    print(f"run_config : user={sb.run_config.user} work_dir={sb.run_config.work_dir}")
    print(f"lifecycle  : idle_ttl {sb.idle_ttl_seconds}s | delete_after_stop {sb.delete_after_stop_seconds}s"
          f" | preserve_memory_on_stop {raw['preserve_memory_on_stop']} | persistent {raw.get('persistent')}")
    print(f"egress     : {len(raw['proxy_config']['access_control']['allow_list'])} allow-listed host patterns")

    with timed("first run()"):
        r = sb.run("uname -r; whoami; nproc; cat /sys/fs/cgroup/cpu.max 2>/dev/null; free -m | sed -n 2p; "
                   "df -h / | tail -1; grep PRETTY /etc/os-release; systemd-detect-virt 2>/dev/null; "
                   "cat /sys/class/dmi/id/sys_vendor 2>/dev/null; dmesg 2>/dev/null | grep -m1 -i -E 'hypervisor|kvm'; "
                   "for t in python3 pip3 uv node npm git gh curl docker langsmith; do "
                   "printf '%s=%s ' $t \"$(command -v $t >/dev/null && echo yes || echo no)\"; done; echo; python3 --version")
    print(r.stdout)
finally:
    sb.delete()

banner("Sandbox without options (server defaults)")
sb = client.create_sandbox(name=name("01d"))
print(f"idle_ttl {sb.idle_ttl_seconds}s | delete_after_stop {sb.delete_after_stop_seconds}s")
sb.delete()

banner(f"{RUNS} sequential creates (create, first run, delete)")
creates, firsts = [], []
for i in range(RUNS):
    t = time.perf_counter()
    s = client.create_sandbox(name=name("01"), idle_ttl_seconds=SAFETY_IDLE_TTL,
                              delete_after_stop_seconds=SAFETY_DELETE_AFTER_STOP)
    c = time.perf_counter() - t
    s.run("true")
    f = time.perf_counter() - t
    creates.append(c)
    firsts.append(f)
    print(f"#{i + 1}: create {c:.2f}s | create + first run {f:.2f}s")
    s.delete()

print(f"create            median {statistics.median(creates):.2f}s | min {min(creates):.2f}s | max {max(creates):.2f}s")
print(f"create + first run median {statistics.median(firsts):.2f}s | min {min(firsts):.2f}s | max {max(firsts):.2f}s")

banner("Same thing over REST with labels")
rs = []
for i in range(3):
    with timed(f"REST create #{i + 1}", res := {}):
        s = create("01")
    rs.append(res[f"REST create #{i + 1}"])
    s.delete()
print(f"REST create median {statistics.median(rs):.2f}s")
