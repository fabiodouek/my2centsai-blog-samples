#!/usr/bin/env python3
"""Test 6: gVisor vs microVM on one cluster.

Prereq: a microVM node pool (gcloud only, the console has no option for it):
  gcloud container node-pools create microvm --cluster $CLUSTER --zone $ZONE --num-nodes 1 --machine-type c3-standard-4 \
      --image-type cos_containerd --sandbox=type=microvm --enable-nested-virtualization --workload-metadata=GKE_METADATA
The recommended secure-sandbox-policy only allows gVisor, so this script removes its binding while it runs and puts it back at the end.

  probes     kernel, dmesg, CPU count, memory, hypervisor flag and the metadata server from a gVisor and a microVM Sandbox
  latency    N cold Sandboxes per runtime (create -> Pod Ready), same pod spec, image already on both nodes
  overhead   what the node's "Allocated resources" shows for 5 pods of each runtime
  snapshot   a Pod Snapshot trigger against a microVM Sandbox
Results: results/t6-microvm.json
"""
import json, statistics, subprocess, sys, time
from pathlib import Path

from kubernetes import client, config

HERE = Path(__file__).parent
RES = HERE / "results"
config.load_kube_config()
core = client.CoreV1Api()
custom = client.CustomObjectsApi()
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def sh(*a, inp=None, check=False):
    return subprocess.run(list(a), capture_output=True, text=True, input=inp, check=check).stdout.strip()


def sandbox(name, runtime, ns="default", extra_labels=None, sa=None, cpu="100m", mem="128Mi"):
    pod = {
        "runtimeClassName": runtime, "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000},
        "containers": [{"name": "probe", "image": "python:3.12-slim", "command": ["sleep", "infinity"],
                        "securityContext": {"capabilities": {"drop": ["ALL"]}, "allowPrivilegeEscalation": False},
                        "resources": {"requests": {"cpu": cpu, "memory": mem}, "limits": {"cpu": "500m", "memory": "512Mi"}}}]}
    if sa:
        pod["serviceAccountName"] = sa
    return {"apiVersion": "agents.x-k8s.io/v1beta1", "kind": "Sandbox", "metadata": {"name": name, "namespace": ns},
            "spec": {"podTemplate": {"metadata": {"labels": extra_labels or {"sandbox": name}}, "spec": pod}}}


def create(obj):
    custom.create_namespaced_custom_object("agents.x-k8s.io", "v1beta1", obj["metadata"]["namespace"], "sandboxes", obj)


def delete(name, ns="default"):
    try:
        custom.delete_namespaced_custom_object("agents.x-k8s.io", "v1beta1", ns, "sandboxes", name)
    except client.ApiException:
        return
    while True:
        try:
            core.read_namespaced_pod(name, ns)
        except client.ApiException:
            return
        time.sleep(0.2)


def wait_ready(name, ns="default", timeout=300):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        try:
            p = core.read_namespaced_pod(name, ns)
            if any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or [])):
                return time.perf_counter() - t0
        except client.ApiException:
            pass
        time.sleep(0.05)
    raise TimeoutError(name)


PROBE = r'''
import os, subprocess, urllib.request, json
def run(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
print("kernel:    ", run("uname -r"))
print("dmesg:     ", " | ".join(l[:70] for l in run("dmesg 2>&1 | head -2").splitlines()))
print("cpus:      ", os.cpu_count(), "  memory MiB:", run("awk '/MemTotal/{print int($2/1024)}' /proc/meminfo"))
print("hypervisor:", run("grep -c -m1 hypervisor /proc/cpuinfo"), "(1 = the CPU reports it runs under a hypervisor)")
def md(p):
    r = urllib.request.Request("http://169.254.169.254/computeMetadata/v1/" + p, headers={"Metadata-Flavor": "Google"})
    try: return urllib.request.urlopen(r, timeout=4).read().decode()
    except Exception as e: return "ERR " + str(getattr(e, "code", getattr(e, "reason", e)))
node = md("instance/name")
print("metadata:   node name ->", node if node.startswith("ERR") else node.split("-")[0] + "-...")
t = md("instance/service-accounts/default/token")
print("metadata:   token ->", t if t.startswith("ERR") else "token returned (%d bytes)" % len(json.loads(t)["access_token"]))
'''


def probe(pod):
    return sh("kubectl", "exec", "-i", pod, "--", "python3", "-", inp=PROBE)


def allocated(node):
    out = sh("kubectl", "describe", "node", node)
    seg = out.split("Allocated resources:")[1].split("Events:")[0]
    row = {}
    for line in seg.splitlines():
        parts = line.split()
        if parts and parts[0] in ("cpu", "memory"):
            row[parts[0]] = " ".join(parts[1:3])
    return row


def node_of(label):
    return sh("kubectl", "get", "nodes", "-l", f"cloud.google.com/gke-nodepool={label}", "-o", "jsonpath={.items[0].metadata.name}")


if __name__ == "__main__":
    out = {"runtimeclass": json.loads(sh("kubectl", "get", "runtimeclass", "microvm", "-o", "json"))["overhead"]}
    binding = sh("kubectl", "get", "validatingadmissionpolicybinding", "secure-sandbox-binding", "-o", "json")
    if binding:
        b = json.loads(binding)
        for k in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields"):
            b["metadata"].pop(k, None)
        sh("kubectl", "delete", "validatingadmissionpolicybinding", "secure-sandbox-binding")
        time.sleep(3)
    try:
        print("== probes")
        for rt in ("gvisor", "microvm"):
            n = f"probe-{rt}"
            create(sandbox(n, rt)); out.setdefault("startup_first", {})[rt] = wait_ready(n)
            text = probe(n)
            out.setdefault("probe", {})[rt] = text
            print(f"--- {rt} (first start {out['startup_first'][rt]:.1f}s, includes image pull)\n{text}")
        print("== latency, cold Sandboxes, create -> Pod Ready")
        for rt in ("gvisor", "microvm"):
            times = []
            for i in range(N):
                n = f"lat-{rt}-{i}"
                t0 = time.perf_counter(); create(sandbox(n, rt)); t = wait_ready(n); times.append(t)
                delete(n)
                print(f"  {rt} #{i:02d} {t:5.2f}s")
            out.setdefault("latency_s", {})[rt] = {"all": times, "min": min(times), "median": statistics.median(times), "max": max(times)}
        print("== overhead: 5 pods each, 100m / 128Mi requested")
        for rt, pool in (("gvisor", "gvisor"), ("microvm", "microvm")):
            node = node_of(pool)
            before = allocated(node)
            names = [f"ov-{rt}-{i}" for i in range(5)]
            for n in names:
                create(sandbox(n, rt))
            for n in names:
                wait_ready(n)
            after = allocated(node)
            out.setdefault("allocated", {})[rt] = {"before": before, "after_5_pods": after}
            print(f"  {rt}: before {before} after {after}")
            for n in names:
                delete(n)
        print("== snapshot attempt on a microVM Sandbox")
        create(sandbox("snap-microvm", "microvm", ns="pod-snapshots-ns", extra_labels={"app": "agent-sandbox-workload"}, sa="pod-snapshot-sa"))
        wait_ready("snap-microvm", "pod-snapshots-ns")
        custom.create_namespaced_custom_object("podsnapshot.gke.io", "v1", "pod-snapshots-ns", "podsnapshotmanualtriggers", {
            "apiVersion": "podsnapshot.gke.io/v1", "kind": "PodSnapshotManualTrigger",
            "metadata": {"name": "mvm-trigger", "namespace": "pod-snapshots-ns"}, "spec": {"targetPod": "snap-microvm"}})
        time.sleep(30)
        trig = custom.get_namespaced_custom_object("podsnapshot.gke.io", "v1", "pod-snapshots-ns", "podsnapshotmanualtriggers", "mvm-trigger")
        events = sh("kubectl", "get", "events", "-n", "pod-snapshots-ns", "--sort-by=.lastTimestamp", "-o",
                    "custom-columns=TYPE:.type,REASON:.reason,OBJECT:.involvedObject.name,MESSAGE:.message")
        out["snapshot_attempt"] = {"trigger_status": trig.get("status"), "events": events.splitlines()[-8:],
                                   "podsnapshots": sh("kubectl", "get", "podsnapshots.podsnapshot.gke.io", "-n", "pod-snapshots-ns").splitlines()}
        print(json.dumps(out["snapshot_attempt"], indent=1, default=str)[:1500])
    finally:
        for n, ns in (("probe-gvisor", "default"), ("probe-microvm", "default"), ("snap-microvm", "pod-snapshots-ns")):
            delete(n, ns)
        sh("kubectl", "delete", "podsnapshotmanualtriggers.podsnapshot.gke.io", "--all", "-n", "pod-snapshots-ns")
        if binding:
            sh("kubectl", "apply", "-f", "-", inp=json.dumps(b))
    RES.mkdir(exist_ok=True)
    (RES / "t6-microvm.json").write_text(json.dumps(out, indent=1, default=str))
