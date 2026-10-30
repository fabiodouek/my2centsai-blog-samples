"""Step 8: pull the test window's CloudWatch metrics for the charts.

Container Insights (1-minute resolution): node CPU and memory utilization, and
CPU/memory for the pods that matter. Free AWS metrics: the NAT gateway's
traffic, which is where snapshot uploads to S3 go without a VPC endpoint."""

import datetime as dt
import json
import time

from common import CONTEXT, REGION, RESULTS, banner, run, save

window = json.loads((RESULTS / "window.json").read_text())
start = dt.datetime.fromtimestamp(window["start"] - 300, dt.timezone.utc)
end = dt.datetime.now(dt.timezone.utc)
print(f"  window {start:%H:%M} to {end:%H:%M} UTC")

QUERIES = {
    "node_cpu": f"SEARCH('{{ContainerInsights,ClusterName,InstanceId,NodeName}} MetricName=\"node_cpu_utilization\" ClusterName=\"{CONTEXT}\"', 'Average', 60)",
    "node_mem": f"SEARCH('{{ContainerInsights,ClusterName,InstanceId,NodeName}} MetricName=\"node_memory_utilization\" ClusterName=\"{CONTEXT}\"', 'Average', 60)",
    "pod_cpu": f"SEARCH('{{ContainerInsights,ClusterName,Namespace,PodName}} MetricName=\"pod_cpu_utilization\" ClusterName=\"{CONTEXT}\"', 'Average', 60)",
    "pod_mem": f"SEARCH('{{ContainerInsights,ClusterName,Namespace,PodName}} MetricName=\"pod_memory_working_set\" ClusterName=\"{CONTEXT}\"', 'Average', 60)",
    "nat_out": "SEARCH('{AWS/NATGateway,NatGatewayId} MetricName=\"BytesOutToDestination\"', 'Sum', 60)",
}

out = {}
for key, expr in QUERIES.items():
    q = [{"Id": "q", "Expression": expr, "ReturnData": True}]
    p = run(["aws", "cloudwatch", "get-metric-data", "--region", REGION, "--metric-data-queries", json.dumps(q),
             "--start-time", start.isoformat(), "--end-time", end.isoformat(), "--output", "json"], quiet=True)
    series = json.loads(p.stdout)["MetricDataResults"]
    out[key] = [{"label": s["Label"], "t": s["Timestamps"], "v": s["Values"]} for s in series]
    banner(key)
    for s in out[key]:
        if s["v"]:
            print(f"  {s['label'][:60]:60} n={len(s['v']):3d} max {max(s['v']):.1f}")

save("cloudwatch", out)
