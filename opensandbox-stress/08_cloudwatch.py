"""Pull the test window's Container Insights and NAT gateway metrics from CloudWatch (read-only).

The window starts when 01_metrics.sh turned Container Insights on (results/metrics-window.json)
and ends when it was turned off, or now. One-minute points:
  - per node: node_cpu_utilization, node_memory_utilization, node_network_total_bytes
  - the lifecycle server and the controller: pod_cpu_utilization, pod_memory_working_set
  - the sandbox namespace: namespace_number_of_running_pods
  - the NAT gateway of the cluster's VPC: BytesOutToDestination, BytesInFromDestination,
    ActiveConnectionCount (AWS/NATGateway)
and, for the pause storms in results/pause_storm.json, the bytes the NAT gateway sent out during
their pause windows (the pushes to ECR; resume pulls either use the VPC's S3 gateway endpoint or find the image still on the node).
A storm's window is its pause window padded by one point (60 s) on each side. Storms closer than
that share one window, whose points are counted once (pause_storm_nat_groups); a storm gets a
figure of its own only when each storm in the group has its own busy minute (measure.storm_nat).

--resummarize recomputes the storm figures from the series already in results/cloudwatch.json,
with no AWS calls.

CloudWatch takes a few minutes to publish a minute's point: run this a few minutes after the
last test. Nodes appear as node-a and node-b, the NAT gateway as nat-1; no instance ID, gateway ID
or account ID is written. Writes results/cloudwatch.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import (
    AWS_REGION,
    CLUSTER_NAME,
    RESULTS,
    NodeAliases,
    Plan,
    cli,
    env_sh,
    heading,
    save,
    say,
    show,
)
from measure import storm_nat

PERIOD = 60
NODE_METRICS = ("node_cpu_utilization", "node_memory_utilization", "node_network_total_bytes")
POD_METRICS = ("pod_cpu_utilization", "pod_memory_working_set")
CONTROL_PODS = ("opensandbox-server", "opensandbox-controller-manager")
NAT_METRICS = ("BytesOutToDestination", "BytesInFromDestination", "ActiveConnectionCount")


def arguments(parser) -> None:
    parser.add_argument("--start", help="ISO time (UTC); default: results/metrics-window.json")
    parser.add_argument("--end", help="ISO time (UTC); default: the window's end, else now")
    parser.add_argument("--resummarize", action="store_true",
                        help="recompute the storm figures from results/cloudwatch.json's series; no AWS calls")


def aws(*args: str) -> dict:
    done = subprocess.run(["aws", *args, "--region", AWS_REGION, "--output", "json"], capture_output=True,
                          text=True, timeout=180)
    if done.returncode != 0:
        raise RuntimeError(f"aws {args[0]} {args[1]} failed: {show(done.stderr.strip()[:200])}")
    return json.loads(done.stdout or "{}")


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metric_data(queries: list[dict], start: datetime, end: datetime) -> dict[str, dict]:
    """{query id: {t: [...], v: [...]}} sorted by time, all pages."""
    out: dict[str, dict] = {}
    for first in range(0, len(queries), 400):
        batch = queries[first:first + 400]
        token = None
        while True:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(batch, handle)
                path = handle.name
            args = ["cloudwatch", "get-metric-data", "--metric-data-queries", f"file://{path}",
                    "--start-time", iso(start), "--end-time", iso(end), "--scan-by", "TimestampAscending"]
            if token:
                args += ["--next-token", token]
            try:
                reply = aws(*args)
            finally:
                Path(path).unlink(missing_ok=True)
            for result in reply.get("MetricDataResults", []):
                slot = out.setdefault(result["Id"], {"t": [], "v": []})
                slot["t"] += result.get("Timestamps", [])
                slot["v"] += result.get("Values", [])
            token = reply.get("NextToken")
            if not token:
                break
    for slot in out.values():
        pairs = sorted(zip(slot["t"], slot["v"]))
        slot["t"] = [iso(parse(t)) for t, _ in pairs]
        slot["v"] = [v for _, v in pairs]
    return out


def query(qid: str, namespace: str, name: str, dims: list[dict], stat: str) -> dict:
    return {"Id": qid, "ReturnData": True,
            "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": name, "Dimensions": dims},
                           "Period": PERIOD, "Stat": stat}}


def window(args) -> tuple[datetime, datetime]:
    path = RESULTS / "metrics-window.json"
    saved = json.loads(path.read_text()) if path.is_file() else {}
    start = args.start or saved.get("start")
    if not start:
        sys.exit("no metrics window: run ./01_metrics.sh first (or pass --start)")
    end = args.end or saved.get("end") or iso(datetime.now(timezone.utc))
    return parse(start), parse(end)


NAT_NOTE = ("A storm's pushes carry the data its sandboxes wrote (fresh_bytes: size x mb MiB of random bytes, "
            "which do not compress) plus the transfer's overhead. ECR's image sizes (snapshot_bytes) also count "
            "each image's base layers, which the registry already holds, so a push skips them. Resumes move next to nothing "
            "through the NAT gateway: their pulls either use the VPC's S3 gateway endpoint or find the image still on the node "
            "(the VPC has no ECR endpoint). A storm's bytes "
            "show in the one-minute point after the one its pause window falls in (the NAT gateway's own timing; "
            "the window is padded by one point on each side for that reason).")


def storms() -> list[dict]:
    """The pause windows of results/pause_storm.json, with each storm's image bytes and fresh data."""
    path = RESULTS / "pause_storm.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    out = []
    for run in data.get("runs", []):
        w = run.get("windows", {})
        if not w.get("pause_start"):
            continue
        made = (data.get("size") or 0) - (run.get("create_failed") or 0)
        out.append({"run": run.get("run"), "start": w["pause_start"], "end": w.get("pause_end"),
                    "snapshot_bytes": (run.get("images") or {}).get("bytes"),
                    "fresh_bytes": made * (data.get("mb") or 0) * 2**20})
    return out


def add_storms(results: dict) -> None:
    """pause_storm_nat (per storm), pause_storm_nat_groups and nat_note, from the NAT series."""
    found = storms()
    if not found or not results.get("nat"):
        return
    gateway = max(results["nat"].values(), key=lambda m: len(m.get("BytesOutToDestination", {}).get("v", [])))
    per_storm, groups = storm_nat(found, gateway.get("BytesOutToDestination", {"t": [], "v": []}), PERIOD)
    for group in groups:
        sent = group["nat_bytes_out"]
        group["vs_fresh_pct"] = round(100 * (sent / group["fresh_bytes"] - 1), 1) if group["fresh_bytes"] else None
        group["vs_images_pct"] = round(100 * (sent / group["snapshot_bytes"] - 1), 1) if group["snapshot_bytes"] else None
    results["pause_storm_nat"] = per_storm
    results["pause_storm_nat_groups"] = groups
    results["nat_note"] = NAT_NOTE


def report(results: dict) -> None:
    heading("CloudWatch")
    for label, metrics in sorted(results["nodes"].items()):
        cpu = metrics.get("node_cpu_utilization", {}).get("v", [])
        print(f"  {label}: CPU {max(cpu):.0f}% peak, {sum(cpu) / len(cpu):.0f}% mean over {len(cpu)} min" if cpu
              else f"  {label}: no points yet (CloudWatch lags a few minutes)")
    for group in results.get("pause_storm_nat_groups", []):
        runs = ", ".join(str(r) for r in group["runs"])
        print(f"  pause storm{'s' if len(group['runs']) > 1 else ''} {runs}: the NAT gateway sent "
              f"{group['nat_bytes_out'] / 1e9:.2f} GB in {group['points']} one-minute points; the storms wrote "
              f"{group['fresh_bytes'] / 1e9:.2f} GB of fresh data ({group['vs_fresh_pct']:+.1f}%); ECR counts "
              f"{group['snapshot_bytes'] / 1e9:.2f} GB of images ({group['vs_images_pct']:+.1f}%)")
        for item in results["pause_storm_nat"]:
            if item["group"] != results["pause_storm_nat_groups"].index(group):
                continue
            sent = item["nat_bytes_out"]
            how = {"window": "its own window", "minute": "its own busy minute"}.get(item["attributed"])
            print(f"      storm {item['run']}: " + (f"{sent / 1e9:.2f} GB ({how})" if sent is not None
                                                     else "not told apart from its neighbours: see the total"))
    if results.get("nat_note"):
        print(f"  {results['nat_note']}")


def resummarize(dry_run: bool) -> int:
    """--resummarize: the storm figures again from results/cloudwatch.json's NAT series; no AWS calls.
    With --dry-run, print them and write nothing."""
    path = RESULTS / "cloudwatch.json"
    if not path.is_file():
        sys.exit("results/cloudwatch.json is not there: run 08_cloudwatch.py first")
    results = json.loads(path.read_text())
    add_storms(results)
    if dry_run:
        print("  --dry-run: the storm figures recomputed from results/cloudwatch.json's series; nothing written")
    else:
        save("cloudwatch", results)
        print("  results/cloudwatch.json: storm figures recomputed from its NAT series")
    report(results)
    return 0


def main() -> int:
    args = cli(__doc__, arguments)
    if args.resummarize:
        return resummarize(args.dry_run)
    plan = Plan("08_cloudwatch.py: the test window's Container Insights and NAT gateway metrics (read-only)")
    plan.checks.append("results/metrics-window.json from ./01_metrics.sh (or --start)")
    plan.step(f"aws cloudwatch list-metrics (ContainerInsights, ClusterName={CLUSTER_NAME}): the nodes' dimensions", 0.2)
    plan.step("aws ec2 describe-nat-gateways for the cluster's VPC", 0.1)
    plan.step(f"aws cloudwatch get-metric-data, {PERIOD} s points: {', '.join(NODE_METRICS)} per node; "
              f"{', '.join(POD_METRICS)} for {', '.join(CONTROL_PODS)}; namespace_number_of_running_pods; "
              f"{', '.join(NAT_METRICS)}", 0.5)
    plan.step("sum the NAT gateway's BytesOutToDestination over the pause storms' windows (results/pause_storm.json), "
              "each one-minute point once")
    plan.writes.append("results/cloudwatch.json")
    plan.print()
    if args.dry_run:
        return 0

    start, end = window(args)
    if end - start > timedelta(days=14):
        sys.exit("the window is longer than 14 days: pass --start/--end")
    heading(f"window {iso(start)} to {iso(end)}")
    listed = aws("cloudwatch", "list-metrics", "--namespace", "ContainerInsights", "--metric-name", NODE_METRICS[0],
                 "--dimensions", f"Name=ClusterName,Value={CLUSTER_NAME}")
    node_dims = [m["Dimensions"] for m in listed.get("Metrics", []) if any(d["Name"] == "NodeName" for d in m["Dimensions"])]
    names = sorted({d["Value"] for dims in node_dims for d in dims if d["Name"] == "NodeName"})
    alias = NodeAliases(names)
    say(f"{len(names)} node(s) in Container Insights for {CLUSTER_NAME}")
    queries, labels = [], {}
    for i, dims in enumerate(node_dims):
        node = next(d["Value"] for d in dims if d["Name"] == "NodeName")
        for j, metric in enumerate(NODE_METRICS):
            qid = f"n{i}m{j}"
            queries.append(query(qid, "ContainerInsights", metric, dims, "Average"))
            labels[qid] = ("node", metric, alias(node))
    for i, pod in enumerate(CONTROL_PODS):
        dims = [{"Name": "ClusterName", "Value": CLUSTER_NAME}, {"Name": "Namespace", "Value": "opensandbox-system"},
                {"Name": "PodName", "Value": pod}]
        for j, metric in enumerate(POD_METRICS):
            qid = f"p{i}m{j}"
            queries.append(query(qid, "ContainerInsights", metric, dims, "Average"))
            labels[qid] = ("pod", metric, pod)
    queries.append(query("ns0", "ContainerInsights", "namespace_number_of_running_pods",
                         [{"Name": "ClusterName", "Value": CLUSTER_NAME}, {"Name": "Namespace", "Value": "opensandbox"}],
                         "Average"))
    labels["ns0"] = ("namespace", "namespace_number_of_running_pods", "opensandbox")
    nat = aws("ec2", "describe-nat-gateways", "--filter", f"Name=vpc-id,Values={env_sh('VPC_ID')}")
    nat_ids = [g["NatGatewayId"] for g in nat.get("NatGateways", []) if g.get("State") in ("available", "deleting", "deleted")]
    for i, nat_id in enumerate(nat_ids):
        for j, metric in enumerate(NAT_METRICS):
            qid = f"g{i}m{j}"
            queries.append(query(qid, "AWS/NATGateway", metric, [{"Name": "NatGatewayId", "Value": nat_id}],
                                 "Maximum" if metric == "ActiveConnectionCount" else "Sum"))
            labels[qid] = ("nat", metric, f"nat-{i + 1}")
    data = metric_data(queries, start, end)

    results: dict = {"window": {"start": iso(start), "end": iso(end)}, "period_s": PERIOD, "nodes": {}, "pods": {},
                     "namespace": {}, "nat": {}}
    for qid, (kind, metric, label) in labels.items():
        series = data.get(qid, {"t": [], "v": []})
        target = {"node": results["nodes"], "pod": results["pods"], "namespace": results["namespace"],
                  "nat": results["nat"]}[kind]
        target.setdefault(label, {})[metric] = series
    add_storms(results)
    save("cloudwatch", results)
    report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
