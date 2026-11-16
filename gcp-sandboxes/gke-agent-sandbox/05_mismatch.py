#!/usr/bin/env python3
"""Test 5: restore a snapshot taken on a C3 node onto an N2 node. Error, or a silent cold start?

Prereqs: 04_pod_snapshots.py ran (Sandbox snap-sandbox exists and has a snapshot taken on the c3-standard-4 gVisor node), and a second
gVisor node pool of N2 machines exists:
  gcloud container node-pools create mismatch --cluster $CLUSTER --zone $ZONE --num-nodes 1 --machine-type n2-standard-2 \
      --image-type cos_containerd --sandbox=type=gvisor --workload-metadata=GKE_METADATA
The script cordons the C3 node, suspends and resumes, and records where the Pod landed, what the events say and whether the
process state came back.  Results: results/t5-mismatch.json
"""
import json, subprocess, time, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import importlib
t4 = importlib.import_module("04_pod_snapshots")

def kubectl(*a):
    return subprocess.run(["kubectl", *a], capture_output=True, text=True).stdout.strip()

NS = t4.NS
c3 = kubectl("get", "nodes", "-l", "cloud.google.com/gke-nodepool=gvisor", "-o", "jsonpath={.items[0].metadata.name}")
n2 = kubectl("get", "nodes", "-l", "cloud.google.com/gke-nodepool=mismatch", "-o", "jsonpath={.items[0].metadata.name}")
print("c3:", c3, " n2:", n2)
before = t4.summarize("before", t4.state())
t_trig, t_snap, snap = t4.snapshot(99)
print(f"snapshot {snap} ready after {t_snap:.1f}s")
t4.write("emptydir", "post-mismatch")
kubectl("cordon", c3)
try:
    t4.set_mode("Suspended"); t4.wait_gone()
    tm, st, pod = t4.resume_timed(timeout=300)
    landed = pod.spec.node_name
    after = t4.summarize("resumed", st)
    events = kubectl("get", "events", "-n", NS, "--sort-by=.lastTimestamp", "-o",
                     "custom-columns=TYPE:.type,REASON:.reason,OBJECT:.involvedObject.name,MESSAGE:.message")
    sb = json.loads(kubectl("get", "sandbox", t4.NAME, "-n", NS, "-o", "json"))
    snaps = kubectl("get", "podsnapshots.podsnapshot.gke.io", "-n", NS, "-o", "wide")
    res = {"landed_on_n2": landed == n2, "timing": tm, "before": before, "after": after, "events": events.splitlines()[-25:],
           "sandbox_conditions": sb.get("status", {}).get("conditions"), "snapshots": snaps.splitlines(),
           "pod_annotations": {k: v for k, v in (pod.metadata.annotations or {}).items() if "snapshot" in k.lower()}}
    print("landed on the N2 node:", res["landed_on_n2"])
    print("\n".join(res["events"][-12:]))
    (Path(__file__).parent / "results" / "t5-mismatch.json").write_text(json.dumps(res, indent=1, default=str))
finally:
    kubectl("uncordon", c3)
