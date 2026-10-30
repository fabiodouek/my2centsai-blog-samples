"""Test 3: lifetime settings. TTL validation, changing TTLs on a live sandbox, resizing (applies on
next start), the delete_after_stop cap, and delete_after_stop actually deleting a stopped sandbox."""

import time

from langsmith.sandbox import ResourceNotFoundError

from common import GiB, banner, client, create, name, rest, timed, wait_status

banner("TTL validation")
for ttl in (90, 30):
    try:
        client.create_sandbox(name=name("03x"), idle_ttl_seconds=ttl)
        print(f"idle_ttl_seconds={ttl}: accepted?!")
    except ValueError as e:
        print(f"idle_ttl_seconds={ttl}: ValueError (client side, nothing created): {e}")
try:
    rest("POST", "/boxes", json={"name": name("03x"), "idle_ttl_seconds": 90, "labels": {"demo": "my2cents-lifecycle"}})
    print("REST idle_ttl_seconds=90: accepted?!")
except RuntimeError as e:
    print(f"REST idle_ttl_seconds=90: {e}")

sb = create("03", idle_ttl_seconds=120, delete_after_stop_seconds=3600)
print(f"\nsandbox: {sb.name}")
try:
    banner("Update TTLs on a running sandbox")
    print(f"before: idle {sb.idle_ttl_seconds}s, delete_after_stop {sb.delete_after_stop_seconds}s")
    up = client.update_sandbox(sb.name, idle_ttl_seconds=0, delete_after_stop_seconds=600)
    print(f"after update_sandbox(idle_ttl_seconds=0, ...): idle {up.idle_ttl_seconds}s, "
          f"delete_after_stop {up.delete_after_stop_seconds}s")
    for v in (2592000, 2592000 + 60):
        try:
            rest("PATCH", f"/boxes/{sb.name}", json={"delete_after_stop_seconds": v})
            print(f"delete_after_stop_seconds={v} ({v / 86400:.2f} d): accepted")
        except RuntimeError as e:
            print(f"delete_after_stop_seconds={v} ({v / 86400:.2f} d): {e}")
    rest("PATCH", f"/boxes/{sb.name}", json={"delete_after_stop_seconds": 600, "idle_ttl_seconds": 300})

    banner("Resize: PATCH vcpus/mem_bytes (REST only)")
    print("before :", sb.run("nproc; free -m | sed -n 2p").stdout.replace("\n", " | "))
    body = rest("PATCH", f"/boxes/{sb.name}", json={"vcpus": 2, "mem_bytes": 4 * GiB})
    print(f"PATCH response: vcpus {body['vcpus']} cpu_millicores {body['cpu_millicores']} "
          f"mem {body['mem_bytes'] / GiB:.0f} GiB status {body['status']}")
    print("running:", sb.run("nproc; free -m | sed -n 2p").stdout.replace("\n", " | "))
    with timed("stop() + start()"):
        sb.stop()
        wait_status(sb.name, "stopped")
        sb.start()
    print("restart:", sb.run("nproc; free -m | sed -n 2p").stdout.replace("\n", " | "))
    print("cpu.max:", sb.run("cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo none").stdout.strip())
    try:
        body = rest("PATCH", f"/boxes/{sb.name}", json={"vcpus": 1, "mem_bytes": 16 * GiB})
        print(f"1 vCPU + 16 GiB: accepted as vcpus {body['vcpus']} cpu_millicores {body['cpu_millicores']} "
              f"mem {body['mem_bytes'] / GiB:.0f} GiB")
    except RuntimeError as e:
        print(f"1 vCPU + 16 GiB (outside 4 GiB/vCPU +-50%): {e}")
finally:
    sb.delete()

banner("delete_after_stop_seconds=60 really deletes")
sb = create("03d", idle_ttl_seconds=60, delete_after_stop_seconds=60)
t0 = time.perf_counter()
sb.stop()
wait_status(sb.name, "stopped")
print(f"[{time.perf_counter() - t0:6.1f}s] stopped")
last = None
while True:
    try:
        st = client.get_sandbox_status(sb.name).status
    except ResourceNotFoundError:
        print(f"[{time.perf_counter() - t0:6.1f}s] gone (ResourceNotFoundError)")
        break
    if st != last:
        print(f"[{time.perf_counter() - t0:6.1f}s] {st}")
        last = st
    time.sleep(5)
