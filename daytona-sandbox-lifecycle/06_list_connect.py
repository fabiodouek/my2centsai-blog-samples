"""Test 6: find sandboxes by label and state, then pick one up by name from a different Python process."""

import subprocess
import sys
import time

from daytona import CreateSandboxFromSnapshotParams, Daytona, ListSandboxesQuery, SandboxState

from common import DEMO_LABELS, SAFETY_TTL, banner, labels, timed

daytona = Daytona()

banner("Create three labelled sandboxes")
users = {"alice": None, "bob": None, "carol": None}
for user in users:
    s = daytona.create(CreateSandboxFromSnapshotParams(
        name=f"my2cents-06-{user}", labels=labels(test="06", userId=user), ttl_minutes=SAFETY_TTL))
    s.fs.upload_file(f"workspace of {user}\n".encode(), f"{s.get_user_home_dir()}/marker.txt")
    users[user] = s
    print(f"  {s.id} name={s.name} userId={user}")
users["bob"].stop()
print(f"stopped bob, get() says: {daytona.get(users['bob'].id).state.value}")

banner("How long until list() agrees?")
q = ListSandboxesQuery(labels={**DEMO_LABELS, "userId": "bob"})
t0 = time.perf_counter()
while (seen := next(iter(daytona.list(q))).state.value) != "stopped":
    print(f"  list() still says {seen} after {time.perf_counter() - t0:.1f}s")
    time.sleep(1)
print(f"  list() says stopped after {time.perf_counter() - t0:.1f}s")

banner("List: all demo sandboxes")
for s in daytona.list(ListSandboxesQuery(labels={**DEMO_LABELS, "test": "06"})):
    print(f"  {s.name:20s} {s.state.value:8s} {s.labels.get('userId')}")

banner("List: userId=alice")
for s in daytona.list(ListSandboxesQuery(labels={**DEMO_LABELS, "userId": "alice"})):
    print(f"  {s.name:20s} {s.state.value}")

banner("List: stopped only")
for s in daytona.list(ListSandboxesQuery(labels={**DEMO_LABELS, "test": "06"}, states=[SandboxState.STOPPED])):
    print(f"  {s.name:20s} {s.state.value}")

banner("Reconnect by name from another process")
child = """
import sys, time
from daytona import Daytona
t = time.perf_counter()
s = Daytona().get(sys.argv[1])
print(f"  get({sys.argv[1]!r}) -> {s.id} state={s.state.value} in {time.perf_counter() - t:.2f}s")
if s.state.value != "started":
    t = time.perf_counter(); s.start(); print(f"  start() in {time.perf_counter() - t:.2f}s")
print("  " + s.process.exec(f"cat {s.get_user_home_dir()}/marker.txt").result.strip())
"""
for name in ("my2cents-06-alice", "my2cents-06-bob"):
    sys.stdout.flush()
    with timed(f"child process for {name}"):
        subprocess.run([sys.executable, "-c", child, name], check=True)

banner("Retag with set_labels")
users["carol"].set_labels(labels(test="06", userId="carol", tier="pro"))
print(f"  carol labels: {daytona.get('my2cents-06-carol').labels}")

for s in users.values():
    s.delete()
