"""Test 7: snapshot a sandbox's filesystem, start new sandboxes from it, and see what fork() does on a container."""

import time

from daytona import CreateSandboxFromSnapshotParams, Daytona

from common import SAFETY_TTL, banner, labels, timed

SNAP = "my2cents-lifecycle-07"
daytona = Daytona()
sbx = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="07"), ttl_minutes=SAFETY_TTL))
home = sbx.get_user_home_dir()
print(f"parent: {sbx.id}")
sbx.process.exec(f"pip install -q cowsay && echo 'state from the parent' > {home}/state.txt", timeout=120)

banner("fork() on a container")
try:
    sbx.fork(name="my2cents-07-fork")
except Exception as e:
    print(f"{type(e).__name__}: {str(e)[:160]}")

banner("create_snapshot() while running")
try:
    with timed("create_snapshot (running)"):
        sbx.create_snapshot(SNAP, timeout=600)
except Exception as e:
    print(f"{type(e).__name__}: {str(e)[:160]}")
    banner("create_snapshot() after stop()")
    sbx.stop()
    with timed("create_snapshot (stopped)"):
        sbx.create_snapshot(SNAP, timeout=600)

snap = daytona.snapshot.get(SNAP)
print(f"snapshot {snap.name}: state={snap.state} size={getattr(snap, 'size', None)} GB")

banner("Three sandboxes from the snapshot")
children = []
for i in range(3):
    with timed(f"create child {i + 1}"):
        c = daytona.create(CreateSandboxFromSnapshotParams(snapshot=SNAP, labels=labels(test="07", child=str(i + 1)),
                                                            ttl_minutes=SAFETY_TTL))
    children.append(c)
    out = c.process.exec(f"cat {home}/state.txt; python3 -c 'import cowsay; print(\"cowsay\", cowsay.__version__)'").result
    print("  " + out.strip().replace("\n", " | "))

banner("Children diverge")
children[0].process.exec(f"echo 'only child 1' >> {home}/state.txt")
for i, c in enumerate(children, 1):
    print(f"  child {i}: {c.process.exec(f'wc -l < {home}/state.txt').result.strip()} line(s)")

banner("Delete the parent: children keep running")
sbx.delete(wait=True)
print(f"  child 1 still: {daytona.get(children[0].id).state.value}")

for c in children:
    c.delete()
time.sleep(5)
daytona.snapshot.delete(daytona.snapshot.get(SNAP))
print(f"deleted snapshot {SNAP}")
