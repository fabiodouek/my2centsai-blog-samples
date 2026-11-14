#!/usr/bin/env python3
"""Test 4: Pod Snapshots. Suspend and resume a sandbox with and without a snapshot, five times, and see what survives.

What it does (namespace pod-snapshots-ns, Sandbox snap-sandbox from manifests/sandbox-snapshot.yaml):
  baseline  suspend with NO snapshot, resume: everything restarts (the counter goes back to 0)
  cycle 1-5 write markers to the emptyDir and the PVC, trigger a PodSnapshotManualTrigger and wait for the PodSnapshot to be
            Ready, write a second marker ("post") to both volumes, set operatingMode Suspended, wait until the Pod is gone,
            set Running, and time: Pod Running, Pod Ready, first reply from the app (through the API server's pod proxy)
Per cycle it records the counter, the process start time, the held TCP connection and the content of both volumes.
Results: results/t4-cycles.json
"""
import json, statistics, subprocess, sys, time
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NS, NAME = "pod-snapshots-ns", "snap-sandbox"
HERE = Path(__file__).parent
config.load_kube_config()
core = client.CoreV1Api()
custom = client.CustomObjectsApi()


def kubectl(*a, check=True, inp=None):
    return subprocess.run(["kubectl", *a], capture_output=True, text=True, check=check, input=inp).stdout.strip()


def proxy(path, timeout=3):
    """GET through the API server's pod proxy (works for gVisor pods, unlike port-forward)."""
    r = core.connect_get_namespaced_pod_proxy_with_path(NAME + ":8000", NS, path=path, _request_timeout=timeout, _preload_content=False)
    return json.loads(r.data)


def state():
    return proxy("state", timeout=8)


def write(kind, tag):
    proxy(f"write?kind={kind}&tag={tag}")


def pod_or_none():
    try:
        return core.read_namespaced_pod(NAME, NS)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def set_mode(mode):
    custom.patch_namespaced_custom_object("agents.x-k8s.io", "v1beta1", NS, "sandboxes", NAME, {"spec": {"operatingMode": mode}})


def wait_gone(timeout=120):
    t0 = time.perf_counter()
    while pod_or_none() is not None:
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError("pod still present")
        time.sleep(0.1)
    return time.perf_counter() - t0


def resume_timed(timeout=180):
    """Set Running; return seconds to Pod Running, Pod Ready and the first successful reply."""
    t0 = time.perf_counter()
    set_mode("Running")
    t_running = t_ready = t_reply = None
    first = None
    while time.perf_counter() - t0 < timeout:
        p = pod_or_none()
        if p is not None:
            if first is None:
                first = time.perf_counter() - t0
            if t_running is None and p.status.phase == "Running":
                t_running = time.perf_counter() - t0
            if t_ready is None and any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or [])):
                t_ready = time.perf_counter() - t0
            if t_running is not None:
                try:
                    st = state()
                    t_reply = time.perf_counter() - t0
                    return {"pod_created_s": first, "running_s": t_running, "ready_s": t_ready, "reply_s": t_reply}, st, p
                except Exception:
                    pass
        time.sleep(0.05)
    raise TimeoutError("no reply")


def snapshot(i):
    """Create a manual trigger; return (seconds to Triggered, seconds to PodSnapshot Ready, snapshot name)."""
    name = f"trigger-{int(time.time())}-{i}"
    t0 = time.perf_counter()
    custom.create_namespaced_custom_object("podsnapshot.gke.io", "v1", NS, "podsnapshotmanualtriggers", {
        "apiVersion": "podsnapshot.gke.io/v1", "kind": "PodSnapshotManualTrigger",
        "metadata": {"name": name, "namespace": NS}, "spec": {"targetPod": NAME}})
    snap, t_trig = None, None
    while time.perf_counter() - t0 < 180:
        o = custom.get_namespaced_custom_object("podsnapshot.gke.io", "v1", NS, "podsnapshotmanualtriggers", name)
        st = o.get("status", {})
        snap = (st.get("snapshotCreated") or {}).get("name")
        if snap and t_trig is None:
            t_trig = time.perf_counter() - t0
        if snap:
            ps = custom.get_namespaced_custom_object("podsnapshot.gke.io", "v1", NS, "podsnapshots", snap)
            if any(c["type"] == "Ready" and c["status"] == "True" for c in ps.get("status", {}).get("conditions", [])):
                return t_trig, time.perf_counter() - t0, snap
        time.sleep(0.1)
    raise TimeoutError("snapshot never became Ready")


def summarize(label, st, extra=None):
    row = {"label": label, "counter": st["counter"], "boot": st["boot"], "uptime_s": st["uptime_s"], "pid": st["pid"],
           "conn": st["conn"], "emptydir": st["emptydir"], "pvc": st["pvc"]}
    row.update(extra or {})

    def last(v):
        return "none" if v is None else f"{len(v)} lines, last {v[-1].split('@')[0]}"
    print(f"  {label:10s} counter={st['counter']:>5} boot={st['boot'] % 100000} conn={st['conn']:<16} emptyDir: {last(st['emptydir']):<22} PVC: {last(st['pvc'])}")
    return row


if __name__ == "__main__":
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    no_pvc = "--no-pvc" in sys.argv   # same sandbox without the PVC: separates the volume re-attach from the restore itself
    tag = "t4-nopvc" if no_pvc else "t4-cycles"
    cm = subprocess.run(["kubectl", "create", "configmap", "state-app", "-n", NS, f"--from-file={HERE/'app'/'state_app.py'}",
                         "--dry-run=client", "-o", "yaml"], capture_output=True, text=True, check=True).stdout
    subprocess.run(["kubectl", "apply", "-f", "-"], input=cm, text=True, check=True, capture_output=True)
    manifest = (HERE / "manifests" / "sandbox-snapshot.yaml").read_text()
    if no_pvc:
        import yaml
        doc = yaml.safe_load(manifest)
        doc["spec"].pop("volumeClaimTemplates")
        pt = doc["spec"]["podTemplate"]["spec"]
        pt["containers"][0]["volumeMounts"] = [m for m in pt["containers"][0]["volumeMounts"] if m["name"] != "data"]
        manifest = yaml.safe_dump(doc)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, check=True)
    t0 = time.perf_counter()
    while True:
        try:
            st = state(); break
        except Exception:
            time.sleep(0.5)
    print(f"sandbox up, first reply after {time.perf_counter()-t0:.1f}s")
    out = {"cycles": []}

    print("== baseline: suspend with NO snapshot")
    time.sleep(3)
    write("emptydir", "base"); write("pvc", "base")
    before = summarize("before", state())
    set_mode("Suspended"); gone = wait_gone()
    tm, st, _ = resume_timed()
    out["baseline"] = {"before": before, "after": summarize("after", st), "suspend_to_gone_s": gone, "timing": tm}
    print("  timing", {k: round(v, 2) for k, v in tm.items()})

    for i in range(1, cycles + 1):
        print(f"== cycle {i}")
        time.sleep(3)
        write("emptydir", f"pre{i}"); write("pvc", f"pre{i}")
        pre = state()
        t_trig, t_snap, snap = snapshot(i)
        # snapshot taken with postCheckpoint=resume: the sandbox keeps running; write after it
        write("emptydir", f"post{i}"); write("pvc", f"post{i}")
        at_snapshot = summarize("pre-snap", pre)
        after_post = summarize("post-write", state())
        set_mode("Suspended"); gone = wait_gone()
        tm, st, pod = resume_timed()
        row = {"cycle": i, "snapshot": snap, "trigger_s": t_trig, "snapshot_ready_s": t_snap, "suspend_to_gone_s": gone,
               "timing": tm, "at_snapshot": at_snapshot, "before_suspend": after_post,
               "after_resume": summarize("resumed", st)}
        row["annotations"] = {k: v for k, v in (pod.metadata.annotations or {}).items() if "snapshot" in k.lower()}
        row["labels"] = {k: v for k, v in (pod.metadata.labels or {}).items() if "snapshot" in k.lower()}
        out["cycles"].append(row)
        print(f"  trigger {t_trig:.2f}s, snapshot Ready {t_snap:.2f}s, gone {gone:.2f}s, "
              f"Running {tm['running_s']:.2f}s, Ready {tm['ready_s']:.2f}s, first reply {tm['reply_s']:.2f}s")
    rs = [c["timing"]["reply_s"] for c in out["cycles"]]
    out["reply_s"] = {"min": min(rs), "median": statistics.median(rs), "max": max(rs)}
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"{tag}.json").write_text(json.dumps(out, indent=1, default=str))
    print("first reply after resume (s):", out["reply_s"])
