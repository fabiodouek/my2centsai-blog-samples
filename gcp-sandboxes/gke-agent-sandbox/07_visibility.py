#!/usr/bin/env python3
"""Test 7: what can an operator see of a gVisor sandbox, next to the same workload under runc?

Run manifests/burn.yaml first and let it run 5+ minutes (Cloud Monitoring needs a few samples).
Compares, for pod burn-runc and Sandbox pod burn-gvisor:
  kubectl top pod · the kubelet's /stats/summary · Cloud Monitoring (container CPU, memory) · Cloud Logging (stdout lines)
Results: results/t7-visibility.json   (project ID redacted)
"""
import json, os, subprocess, time, urllib.parse, urllib.request
from pathlib import Path

PROJECT = os.environ["PROJECT"]
PODS = {"runc": "burn-runc", "gvisor": "burn-gvisor"}


def sh(*a):
    return subprocess.run(list(a), capture_output=True, text=True).stdout.strip()


def token():
    return sh("gcloud", "auth", "print-access-token")


def monitoring(metric, pod, minutes=8):
    end = time.time(); start = end - minutes * 60
    q = urllib.parse.urlencode({
        "filter": f'metric.type="{metric}" AND resource.labels.pod_name="{pod}"',
        "interval.startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
        "interval.endTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
        "aggregation.alignmentPeriod": "60s", "aggregation.perSeriesAligner": "ALIGN_MEAN" if "used_bytes" in metric else "ALIGN_RATE"})
    req = urllib.request.Request(f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries?{q}",
                                 headers={"Authorization": f"Bearer {token()}"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        return {"error": str(e)}
    series = data.get("timeSeries", [])
    rows = []
    for s in series:
        vals = [p["value"].get("doubleValue", p["value"].get("int64Value")) for p in s.get("points", [])]  # newest first
        rows.append({"memory_type": s["metric"].get("labels", {}).get("memory_type"), "points": len(vals),
                     "latest": vals[0] if vals else None, "oldest": vals[-1] if vals else None})
    return {"series": rows}


def main():
    out = {}
    node = {k: sh("kubectl", "get", "pod", p, "-o", "jsonpath={.spec.nodeName}") for k, p in PODS.items()}
    out["top"] = {k: sh("kubectl", "top", "pod", p, "--containers") or "(no output)" for k, p in PODS.items()}
    for k, p in PODS.items():
        out["top"][k] = subprocess.run(["kubectl", "top", "pod", p, "--containers"], capture_output=True, text=True).stdout.strip() or \
            subprocess.run(["kubectl", "top", "pod", p, "--containers"], capture_output=True, text=True).stderr.strip()
    stats = {}
    for k, p in PODS.items():
        s = json.loads(sh("kubectl", "get", "--raw", f"/api/v1/nodes/{node[k]}/proxy/stats/summary"))
        pod = next((x for x in s["pods"] if x["podRef"]["name"] == p), None)
        stats[k] = None if pod is None else {
            "cpu_usageNanoCores": pod.get("cpu", {}).get("usageNanoCores"), "memory_workingSetBytes": pod.get("memory", {}).get("workingSetBytes"),
            "containers": [{"name": c["name"], "cpu": c.get("cpu", {}).get("usageNanoCores"), "memory": c.get("memory", {}).get("workingSetBytes")}
                           for c in pod.get("containers", [])]}
    out["kubelet_summary"] = stats
    out["monitoring"] = {k: {m.split("/")[-1]: monitoring(m, p) for m in
                             ("kubernetes.io/container/cpu/core_usage_time", "kubernetes.io/container/memory/used_bytes")}
                         for k, p in PODS.items()}
    out["logging"] = {}
    for k, p in PODS.items():
        txt = sh("gcloud", "logging", "read", f'resource.type="k8s_container" AND resource.labels.pod_name="{p}" AND textPayload:"tick"',
                 "--limit", "3", "--format", "value(textPayload)", "--freshness", "15m")
        out["logging"][k] = txt.splitlines()
    text = json.dumps(out, indent=1).replace(PROJECT, "<project>")
    Path(__file__).parent.joinpath("results", "t7-visibility.json").write_text(text)
    print(text)


main()
