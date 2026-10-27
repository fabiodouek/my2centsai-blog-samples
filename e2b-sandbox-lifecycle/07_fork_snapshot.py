"""Test 7: fork a running sandbox into copies that inherit files, processes and memory; then snapshot and restore."""

import time

from e2b import Sandbox

from common import banner, tagged, timed

sbx = Sandbox.create(timeout=600, metadata=tagged(test="07"))
print(f"original: {sbx.sandbox_id}")
sbx.files.write("/home/user/state.txt", "shared state\n")
counter = sbx.commands.run(
    "python3 -u -c \"import time\ni=0\nwhile True:\n    i+=1; open('/tmp/counter','w').write(str(i)); time.sleep(1)\"",
    background=True,
    timeout=0,
)
time.sleep(5)

banner("Fork x3")
with timed("fork(count=3, timeout=120)"):
    results = sbx.fork(count=3, timeout=120)  # always pass timeout: the SDK default is 15s
forks = [r for r in results if isinstance(r, Sandbox)]
errors = [r for r in results if not isinstance(r, Sandbox)]
print(f"forks: {len(forks)}, errors: {len(errors)} {errors}")

for n, f in enumerate(forks, 1):
    f.files.write("/home/user/whoami.txt", f"fork {n}\n")
    alive = f.commands.run(f"kill -0 {counter.pid} && echo alive || echo dead").stdout.strip()
    print(
        f"  fork {n} {f.sandbox_id}: state.txt={f.files.read('/home/user/state.txt').strip()!r} "
        f"counter={f.files.read('/tmp/counter')} pid {counter.pid} {alive}"
    )

exists = sbx.commands.run("test -f /home/user/whoami.txt && echo yes || echo no").stdout.strip()
print(f"original sees fork writes? {exists}  (forks diverge)")
print(f"original counter still running: {sbx.files.read('/tmp/counter')}")

banner("Snapshot -> new sandbox")
with timed("create_snapshot()"):
    snap = sbx.create_snapshot(name="my2cents-lifecycle-demo")
print(f"snapshot: {snap.snapshot_id}")
with timed("Sandbox.create(snapshot_id)"):
    restored = Sandbox.create(snap.snapshot_id, timeout=120, metadata=tagged(test="07"))
print(f"restored {restored.sandbox_id}: {restored.files.read('/home/user/state.txt').strip()!r}")

listed = Sandbox.list_snapshots(sandbox_id=sbx.sandbox_id).next_items()
print(f"snapshots for original: {[s.snapshot_id for s in listed]}")

for s in [*forks, restored, sbx]:
    s.kill()
print(f"deleted snapshot: {Sandbox.delete_snapshot(snap.snapshot_id)}")
