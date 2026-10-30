"""Test 7: capture a snapshot from a running sandbox and "fork" it into three sandboxes (there is no
fork() API), sequentially and concurrently; deleting a snapshot that sandboxes still use; a memory snapshot (REST only)."""

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from common import PREFIX, banner, client, create, rest, timed

SNAP = f"{PREFIX}-fork"
src = create("07")
made = []
try:
    banner(f"Source: {src.name}")
    src.run("mkdir -p /root/work && echo base > /root/work/state.txt && python3 -m venv /root/v "
            "&& /root/v/bin/pip install -q cowsay", timeout=180)
    stream = src.run("for i in $(seq 1 20); do echo $i; sleep 1; done", wait=False)
    with timed("capture_snapshot() while a command streams"):
        snap = src.capture_snapshot(SNAP, timeout=300)
    print(f"snapshot {snap.id} status={snap.status} fs_capacity {snap.fs_capacity_bytes / 1024**3:.0f} GiB "
          f"fs_used {(snap.fs_used_bytes or 0) / 1024**3:.2f} GiB source={snap.source_sandbox_id} tags={snap.tags}")
    lines = [c.data.strip() for c in stream]
    print(f"stream on the source after capture: last line {lines[-1:]} exit={stream.result.exit_code}")
    print(f"source status: {client.get_sandbox_status(src.name).status}")

    banner("3 sandboxes from the snapshot, one after another")
    seq = []
    for i in range(3):
        r = {}
        with timed(f"fork {i}", r):
            f = create(f"07f{i}", snapshot_id=snap.id)
        seq.append(r[f"fork {i}"])
        made.append(f)
    print(f"sequential median {statistics.median(seq):.2f}s")
    for i, f in enumerate(made):
        f.run(f"echo fork-{i} >> /root/work/state.txt")
    for i, f in enumerate(made):
        out = f.run("cat /root/work/state.txt | tr '\\n' ' '; /root/v/bin/pip list 2>/dev/null | grep -i cowsay; "
                    "pgrep -f '[s]eq 1 20' || echo no-stream-process").stdout
        print(f"  fork {i}: {out.strip()!r}")
    print(f"  source : {src.run('cat /root/work/state.txt').stdout.strip()!r}")

    banner("Delete the snapshot while forks use it")
    try:
        client.delete_snapshot(snap.id)
        print("delete_snapshot: deleted?!")
    except Exception as e:
        print(f"delete_snapshot: {type(e).__name__}: {e}")
    for f in made:
        f.delete()
    made.clear()

    banner("3 sandboxes from the snapshot, concurrently")
    t0 = time.perf_counter()

    def one(i):
        t = time.perf_counter()
        sb = create(f"07c{i}", snapshot_id=snap.id)
        return sb, time.perf_counter() - t

    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(one, range(3)))
    made.extend(sb for sb, _ in res)
    print(f"each: {', '.join(f'{d:.2f}s' for _, d in res)} | wall {time.perf_counter() - t0:.2f}s")
    for f in made:
        f.delete()
    made.clear()

    banner("Memory snapshot (REST only)")
    src.run("nohup sh -c 'i=0; while true; do i=$((i+1)); echo $i > /root/counter; sleep 1; done' >/dev/null 2>&1 &")
    time.sleep(3)
    try:
        with timed("POST /boxes/{name}/snapshot include_memory=true"):
            mem = rest("POST", f"/boxes/{src.name}/snapshot", json={"name": f"{PREFIX}-warm", "include_memory": True})
            mem = client.wait_for_snapshot(mem["id"], timeout=300)
        raw = rest("GET", f"/snapshots/{mem.id}")
        print(f"memory snapshot {mem.id}: memory_snapshot_size_bytes={raw.get('memory_snapshot_size_bytes')}")
        with timed("create from memory snapshot (restore_memory=true)"):
            m = create("07m", snapshot_id=mem.id, restore_memory=True)
        made.append(m)
        a = m.run("cat /root/counter").stdout.strip()
        time.sleep(2)
        b = m.run("cat /root/counter; cut -d' ' -f1 /proc/uptime").stdout.split()
        print(f"restored counter {a} -> {b[0]} two seconds later, uptime {b[1]}s "
              f"({'process resumed' if a != b[0] else 'process not resumed'})")
    except RuntimeError as e:
        print(f"memory snapshot: {e}")
finally:
    for f in made:
        f.delete()
    src.delete()
