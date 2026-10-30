"""Test 5: when does a snapshot reach S3, and what does a resume from S3 cost?

Substrate's suspend is asynchronous: the checkpoint is written on the node,
atelet uploads it in the background after --snapshot-upload-delay (5 min by
default) and deletes the local copy after --snapshot-detach-delay (30 min).
This script watches the actor's own record in Substrate (kubectl-ate) while
both happen. It takes about 35 minutes.
"""

import time

from common import (REGION, actor, ax, banner, create, delete, run, sh, status, timed, worker_node)

NAME = "lc-s3"


def snap() -> dict:
    snaps = actor(NAME).get("status", {}).get("snapshots") or [{}]
    s = snaps[-1]  # the latest snapshot
    durable = s.get("durableSnapshot", {})
    return {"durable": durable.get("status", "-").replace("SNAPSHOT_STORAGE_STATUS_", ""),
            "uri": durable.get("object", {}).get("snapshotUri", ""),
            "locality": s.get("locality") or "none (detached)"}


banner(f"{NAME}: 100 MB random + 100 MB zeros in /workspace")
create(NAME)
ax("resume", "task", NAME, quiet=True)
print(sh(NAME, "dd if=/dev/urandom of=/workspace/random bs=1M count=100 2>/dev/null; "
               "dd if=/dev/zero of=/workspace/zeros bs=1M count=100 2>/dev/null; "
               "sha256sum /workspace/random /workspace/zeros | tee /workspace/sums; du -sh /workspace"))
print(f"  running on {worker_node(status(NAME)['worker'])}")

banner("suspend, then watch the actor's snapshot status every 30 s")
timed("ax suspend", ax, "suspend", "task", NAME, quiet=True)
t0 = time.time()
last = None
while True:
    s = snap()
    state = (s["durable"], s["locality"])
    if state != last:
        print(f"  +{time.time() - t0:5.0f}s  durable copy: {s['durable']:9}  local copy on: {s['locality']}", flush=True)
        last = state
    if s["locality"].startswith("none") or time.time() - t0 > 34 * 60:
        break
    time.sleep(30)

banner("the snapshot in S3")
uri = snap()["uri"]
listing = run(["aws", "s3", "ls", "--recursive", uri + "/", "--region", REGION], quiet=True).stdout
total = 0
for line in listing.splitlines():
    size, key = int(line.split()[2]), line.split()[3]
    total += size
    print(f"  {size / 1e6:8.1f} MB  {key.split('/snapshots/')[1]}")
print(f"  {total / 1e6:8.1f} MB  in S3 for 200 MB of files (100 MB random, 100 MB zeros), zstd-compressed")

banner("resume, now that the only copy is in S3")
timed("ax resume", ax, "resume", "task", NAME, quiet=True)
print(f"  now on {worker_node(status(NAME)['worker'])}")
print(sh(NAME, "sha256sum -c /workspace/sums"))

delete(NAME)
