"""Test 6: tag sandboxes with metadata, list and filter them, and connect to one from a separate process."""

import subprocess
import sys

from e2b import Sandbox, SandboxQuery, SandboxState

from common import banner, tagged, timed

if len(sys.argv) == 3 and sys.argv[1] == "--connect":
    # Runs in a separate Python process: no shared objects, only the sandbox ID.
    with timed("Sandbox.connect(id) from second process"):
        sbx = Sandbox.connect(sys.argv[2])
    print(f"  [worker] read: {sbx.files.read('/home/user/owner.txt').strip()}")
    sbx.files.write("/home/user/worker.txt", "the worker was here\n")
    sys.exit(0)

banner("Create three tagged sandboxes")
users = ["alice", "alice", "bob"]
boxes = []
for user in users:
    s = Sandbox.create(timeout=300, metadata=tagged(test="06", userId=user))
    s.files.write("/home/user/owner.txt", f"owned by {user}\n")
    boxes.append(s)
    print(f"  {s.sandbox_id} userId={user}")
boxes[1].pause()
print(f"  paused {boxes[1].sandbox_id}")


def show(label: str, query: SandboxQuery | None) -> None:
    paginator = Sandbox.list(query=query, limit=100)
    items = paginator.next_items()
    while paginator.has_next:
        items.extend(paginator.next_items())
    mine = [i for i in items if i.metadata.get("test") == "06"]
    print(f"{label}: {len(mine)}")
    for i in mine:
        print(f"    {i.sandbox_id} {i.state} userId={i.metadata.get('userId')}")


banner("List")
show("default (no query)", None)
show("state=running", SandboxQuery(state=[SandboxState.RUNNING]))
show("state=paused", SandboxQuery(state=[SandboxState.PAUSED]))
show("userId=alice, any state", SandboxQuery(metadata={"userId": "alice"}, state=[SandboxState.RUNNING, SandboxState.PAUSED]))

banner("Connect from another process (resumes the paused one)")
target = boxes[1].sandbox_id
subprocess.run([sys.executable, __file__, "--connect", target], check=True)
print(f"back in parent, worker file: {boxes[1].files.read('/home/user/worker.txt').strip()}")
print(f"state now: {Sandbox.get_info(target).state}")

for s in boxes:
    s.kill()
