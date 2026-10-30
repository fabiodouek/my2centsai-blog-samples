"""Test 6: labels, listing (SDK vs REST filters and paging), and reattaching to a sandbox and to a
running command from a second process that only knows the sandbox name and the command id."""

import subprocess
import sys
import time

from common import DEMO_LABELS, banner, client, create, rest, timed, wait_status

CHILD = r"""
import sys, time
t0 = time.perf_counter()
from common import client
name, cmd_id = sys.argv[1], sys.argv[2]
sb = client.get_sandbox(name)
print(f"child: get_sandbox -> status {sb.status} [{time.perf_counter() - t0:.2f}s]")
t = time.perf_counter()
print(f"child: run() on it -> {sb.run('cat /root/state.txt').stdout.strip()!r} [{time.perf_counter() - t:.2f}s]")
print(f"child: status now {client.get_sandbox_status(name).status}")
t = time.perf_counter()
try:
    h = sb.reconnect(cmd_id)
    chunks = [c.data.strip() for c in h]
    print(f"child: reconnect({cmd_id[:8]}...) replayed {len(chunks)} chunks, first {chunks[:1]} last {chunks[-1:]}"
          f" exit={h.result.exit_code} [{time.perf_counter() - t:.2f}s]")
except Exception as e:
    print(f"child: reconnect({cmd_id[:8]}...) -> {type(e).__name__}: {e}")
print(f"child: total {time.perf_counter() - t0:.2f}s")
"""

boxes = [create("06", labels={**DEMO_LABELS, "test": "06", "user": u}) for u in ("alice", "bob", "carol")]
try:
    banner("Listing")
    print(f"SDK list_sandboxes() (no filters): {len(client.list_sandboxes())} sandboxes in the workspace")
    for params in [{"label": "demo=my2cents-lifecycle"}, {"label": "user=alice"}, {"label": "user"},
                   {"created_by": "me", "name_contains": "lc-06"}]:
        items = rest("GET", "/boxes", params=params)["items"]
        print(f"GET /boxes {params}: {sorted(b['labels'].get('user', '-') for b in items)}")
    page = rest("GET", "/boxes", params={"label": "test=06", "page_size": 2, "sort_by": "name", "sort_order": "asc"})
    nxt = rest("GET", "/boxes", params={"label": "test=06", "page_size": 2, "sort_by": "name", "sort_order": "asc",
                                        "cursor": page["next_cursor"]})
    print(f"page_size=2: {len(page['items'])} items + next_cursor={bool(page['next_cursor'])}, "
          f"page 2: {len(nxt['items'])} items, next_cursor={nxt['next_cursor']}")

    banner("Does list() lag get() after a stop?")
    bob = boxes[1]
    bob.stop()
    t0 = time.perf_counter()
    for _ in range(20):
        get_st = client.get_sandbox_status(bob.name).status
        listed = rest("GET", "/boxes", params={"label": "user=bob"})["items"][0]["status"]
        stopped = [b["name"] for b in rest("GET", "/boxes", params={"label": "test=06", "status": "stopped"})["items"]]
        print(f"  [{time.perf_counter() - t0:5.2f}s] get={get_st} list={listed} status=stopped filter={len(stopped)}")
        if get_st == listed == "stopped" and stopped:
            break
        time.sleep(0.5)

    banner("Labels on update?")
    try:
        rest("PATCH", f"/boxes/{bob.name}", json={"labels": {**DEMO_LABELS, "test": "06", "user": "robert"}})
        print(f"PATCH labels -> {rest('GET', f'/boxes/{bob.name}')['labels']}")
    except RuntimeError as e:
        print(f"PATCH labels: {e}")

    banner("Second process: wake a stopped sandbox and reattach to a command")
    alice = boxes[0]
    alice.write("/root/state.txt", "written by the parent\n")
    h = alice.run("for i in $(seq 1 15); do echo line $i; sleep 1; done", wait=False)
    first = next(iter(h)).data.strip()
    cmd_id = h.command_id
    print(f"parent: started command {cmd_id[:8]}..., saw {first!r}, then disconnected")
    del h
    with timed("child process (python startup + get + run + reconnect)"):
        out = subprocess.run([sys.executable, "-c", CHILD, alice.name, cmd_id], capture_output=True, text=True)
    print(out.stdout.strip(), out.stderr.strip()[-400:])

    alice.stop()
    wait_status(alice.name, "stopped")
    print(f"\nparent: stopped {alice.name}; child again, with the old command id")
    out = subprocess.run([sys.executable, "-c", CHILD, alice.name, cmd_id], capture_output=True, text=True)
    print(out.stdout.strip(), out.stderr.strip()[-400:])
finally:
    for b in boxes:
        b.delete()
