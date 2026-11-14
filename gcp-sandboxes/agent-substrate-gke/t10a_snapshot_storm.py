"""Test 10a: snapshot storm. 20 tasks each write 100 MB of random data to
/workspace and suspend at the same moment. How long do the suspends take, and
how long until every snapshot is durable in Cloud Storage (atelet uploads in the
background, after a 5-minute delay)? Cloud NAT and GCS byte counts for the same window come from t10c_monitoring.py."""

import time

from common import (delete_all, Sampler, actor, ax, banner, create, describe, parallel, save, sh, timed_call)

delete_all()  # start from an empty atespace

N = 20
LIMITS = {"cpu": "100m", "memory": "256Mi"}
names = [f"st-s-{i:02d}" for i in range(N)]


def durable(name):
    snaps = actor(name).get("status", {}).get("snapshots") or [{}]
    return snaps[-1].get("durableSnapshot", {}).get("state", "-").replace("SNAPSHOT_STORAGE_STATE_", "")


with Sampler("snapshot_storm", every=10) as s:
    banner(f"{N} tasks, 100 MB of random data each")
    parallel(lambda n: create(n, **LIMITS), names, 10)
    parallel(lambda n: ax("resume", "task", n, quiet=True), names, 10)
    parallel(lambda n: sh(n, "dd if=/dev/urandom of=/workspace/r bs=1M count=100 2>/dev/null"), names, 10)

    banner(f"suspend all {N} at once")
    t_susp = time.time() - s.t0
    out = parallel(lambda n: timed_call(ax, "suspend", "task", n, quiet=True, check=False), names, N)
    susp = describe("suspend", [ms for ok, ms, _ in out if ok])

    banner("wait for every snapshot to be durable in GCS")
    t0, timeline = time.time(), []
    while True:
        states = parallel(durable, names, 10)
        done = states.count("COMPLETED")
        timeline.append((round(time.time() - t0), done))
        print(f"  +{time.time() - t0:4.0f}s  {done:2d}/{N} durable", flush=True)
        if done == N or time.time() - t0 > 20 * 60:
            break
        time.sleep(20)

banner("what is in the bucket for these 20 snapshots")
from common import SNAPSHOT_LOCATION, ATESPACE, run
du = run(["gcloud", "storage", "du", "--summarize", "--readable-sizes", f"{SNAPSHOT_LOCATION}atespaces/{ATESPACE}/"], quiet=True, check=False)
print("  ", du.stdout.strip() or du.stderr.strip())
save("snapshot_storm", {"suspend": susp, "suspend_at": t_susp, "timeline": timeline, "bucket_du": du.stdout.strip()})
delete_all("st-s-")
