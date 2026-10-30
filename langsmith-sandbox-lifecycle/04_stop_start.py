"""Test 4: stop and start three times, what survives (files in /root and /tmp, a running process),
what a stopped sandbox does when you touch it, and the same cycle with preserve_memory_on_stop (REST only)."""

import statistics
import time

from common import banner, client, create, rest, timed, wait_status

COUNTER = ("printf 'i=0; while true; do i=$((i+1)); echo $i > /root/counter; sleep 1; done' > /root/count.sh; "
           "nohup sh /root/count.sh >/dev/null 2>&1 &")
ALIVE = "pgrep -f '[c]ount.sh' >/dev/null && echo process-alive || echo process-gone"
CHECK = ("cat /root/state.txt; cat /tmp/state.txt 2>/dev/null || echo '/tmp/state.txt missing'; "
         "a=$(cat /root/counter); sleep 2; b=$(cat /root/counter); echo \"counter $a -> $b\"; "
         f"{ALIVE}; echo uptime $(cut -d' ' -f1 /proc/uptime)s")


def cycles(sb, label: str) -> None:
    banner(f"{label}: {sb.name}")
    print(sb.run("df -h /tmp | tail -1; mount | grep ' /tmp ' || echo '/tmp is on the root disk'").stdout.strip())
    sb.write("/root/state.txt", "disk survives\n")
    sb.write("/tmp/state.txt", "tmp survives\n")
    sb.run(COUNTER)
    time.sleep(3)
    stops, starts = [], []
    for cycle in range(1, 4):
        before = sb.run("cat /root/counter").stdout.strip()
        r = {}
        with timed(f"cycle {cycle} stop() until status=stopped", r):
            sb.stop()
            wait_status(sb.name, "stopped", poll=0.2)
        with timed(f"cycle {cycle} start()", r):
            sb.start()
        stops.append(r[f"cycle {cycle} stop() until status=stopped"])
        starts.append(r[f"cycle {cycle} start()"])
        print(f"  counter at stop: {before}")
        print("  " + sb.run(CHECK).stdout.strip().replace("\n", "\n  "))
        if "process-gone" in sb.run(ALIVE).stdout:
            sb.run(COUNTER)
            time.sleep(2)
    print(f"{label}: stop median {statistics.median(stops):.2f}s | start median {statistics.median(starts):.2f}s")


# A: default stop (filesystem only)
sb = create("04a")
try:
    cycles(sb, "Default stop")

    banner("Touching a stopped sandbox")
    sb.stop()
    wait_status(sb.name, "stopped")
    with timed("run('echo hi') on a stopped sandbox"):
        try:
            r = sb.run("echo hi")
            print(f"exit={r.exit_code} {r.stdout.strip()!r}")
        except Exception as e:
            print(f"{type(e).__name__}: {str(e)[:200]}")
    print(f"status after: {client.get_sandbox_status(sb.name).status}")

    banner("stop() twice")
    sb.stop()
    wait_status(sb.name, "stopped")
    try:
        sb.stop()
        print("second stop(): no error")
    except Exception as e:
        print(f"second stop(): {type(e).__name__}: {str(e)[:200]}")
    try:
        sb.start()
        sb.start()
        print("second start(): no error")
    except Exception as e:
        print(f"second start(): {type(e).__name__}: {str(e)[:200]}")
finally:
    sb.delete()

# B: memory-preserving stop, set at create time over REST
try:
    sbm = create("04b", preserve_memory_on_stop=True)
except RuntimeError as e:
    print(f"preserve_memory_on_stop rejected: {e}")
else:
    try:
        print(f"preserve_memory_on_stop: {rest('GET', f'/boxes/{sbm.name}')['preserve_memory_on_stop']}")
        cycles(sbm, "preserve_memory_on_stop")
    finally:
        sbm.delete()
