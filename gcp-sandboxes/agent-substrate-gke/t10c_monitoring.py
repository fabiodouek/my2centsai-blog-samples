"""Test 10c: pull the test window's Cloud Monitoring series for the charts and the network-path question.

GKE system metrics are on by default and free: node CPU/memory utilisation and container restarts.
Cloud NAT (router.googleapis.com/nat/sent_bytes_count) and Cloud Storage (storage.googleapis.com/network/
received_bytes_count) show whether snapshot uploads leave the VPC through the NAT gateway or reach GCS
over Private Google Access. Uses the REST API with a token from `gcloud auth print-access-token`."""

import datetime as dt
import json
import urllib.parse
import urllib.request

from common import PROJECT_ID, RESULTS, banner, run, save

window = json.loads((RESULTS / "window.json").read_text())
start = dt.datetime.fromtimestamp(window["start"] - 300, dt.timezone.utc)
end = dt.datetime.now(dt.timezone.utc)
print(f"  window {start:%H:%M} to {end:%H:%M} UTC")
token = run(["gcloud", "auth", "print-access-token"], quiet=True).stdout.strip()

QUERIES = {   # name: (metric type, extra filter, aligner, reducer group)
    "node_cpu": ("kubernetes.io/node/cpu/allocatable_utilization", "", "ALIGN_MEAN"),
    "node_mem": ("kubernetes.io/node/memory/allocatable_utilization", "", "ALIGN_MEAN"),
    "restarts": ("kubernetes.io/container/restart_count", "", "ALIGN_DELTA"),
    "nat_sent": ("router.googleapis.com/nat/sent_bytes_count", "", "ALIGN_DELTA"),
    "nat_received": ("router.googleapis.com/nat/received_bytes_count", "", "ALIGN_DELTA"),
    "gcs_received": ("storage.googleapis.com/network/received_bytes_count", "", "ALIGN_DELTA"),
}


def query(metric, aligner, period=60):
    params = {"filter": f'metric.type="{metric}"', "interval.startTime": start.isoformat(),
              "interval.endTime": end.isoformat(), "aggregation.alignmentPeriod": f"{period}s",
              "aggregation.perSeriesAligner": aligner}
    url = f"https://monitoring.googleapis.com/v3/projects/{PROJECT_ID}/timeSeries?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    series = []
    while True:
        with urllib.request.urlopen(req) as r:
            body = json.load(r)
        series += body.get("timeSeries", [])
        if not body.get("nextPageToken"):
            return series
        req = urllib.request.Request(url + "&pageToken=" + body["nextPageToken"], headers={"Authorization": f"Bearer {token}"})


out = {}
for key, (metric, _, aligner) in QUERIES.items():
    banner(key)
    out[key] = []
    for s in query(metric, aligner):
        vals = [(p["interval"]["endTime"], p["value"].get("doubleValue", float(p["value"].get("int64Value", 0))))
                for p in s["points"]]
        label = s["resource"]["labels"].get("node_name") or s["resource"]["labels"].get("gateway_name") \
            or s["resource"]["labels"].get("bucket_name") or s["resource"]["labels"].get("pod_name", "?")
        out[key].append({"label": label, "t": [t for t, _ in vals], "v": [v for _, v in vals]})
        if vals:
            print(f"  {label[:60]:60} n={len(vals):3d} max {max(v for _, v in vals):.3g} sum {sum(v for _, v in vals):.4g}")
save("monitoring", out)
