"""Test 9: kill every running or paused sandbox created by these scripts and delete demo snapshots.
Paused sandboxes never expire on their own, so this is the step people forget."""

from e2b import Sandbox, SandboxQuery, SandboxState

from common import DEMO_TAG, banner

banner("Sandboxes tagged " + str(DEMO_TAG))
paginator = Sandbox.list(
    query=SandboxQuery(metadata=DEMO_TAG, state=[SandboxState.RUNNING, SandboxState.PAUSED]),
    limit=100,
)
killed = 0
while True:
    for info in paginator.next_items():
        ok = Sandbox.kill(info.sandbox_id)
        killed += ok
        print(f"  kill {info.sandbox_id} ({info.state}): {ok}")
    if not paginator.has_next:
        break
print(f"killed {killed}")

banner("Snapshots named my2cents-*")
snaps = Sandbox.list_snapshots(name="my2cents-lifecycle-demo").next_items()
for s in snaps:
    print(f"  delete {s.snapshot_id}: {Sandbox.delete_snapshot(s.snapshot_id)}")
print(f"deleted {len(snaps)}")
