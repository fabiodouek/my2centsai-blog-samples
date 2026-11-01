"""Every 2 s, one CSV row per node from the kubelet summary API (through the API server's node
proxy) plus the Kubernetes objects that bound density: Pods per node, OpenShell sandbox Pods,
unscheduled Pods, attached EBS volumes (VolumeAttachment objects), NetworkPolicies and VPC CNI
PolicyEndpoints, and the gateway Pod's and shared supervisor Pods' CPU and memory.

Runs until Ctrl-C / SIGTERM / --duration, finishing the sample in progress and closing its
files. The tests start it in the background (lib.sampling); it also runs on its own:

    uv run python sampler.py --out results/idle.csv --duration 60
    uv run python sampler.py --out results/x.csv --metrics-out results/x-gateway.csv   # + gateway /metrics
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lib import NAMESPACE, PREFIX, WORKSPACE, GatewayMetrics, kraw, metric_sum

FIELDS = ["ts", "node", "node_cpu_cores", "node_mem_ws_mib", "node_mem_avail_mib",
          "cpu_psi_some_avg10", "mem_psi_some_avg10", "io_psi_some_avg10",
          "pods_running", "pods_scheduled", "pods_unscheduled", "sandbox_pods", "p6_pods",
          "volume_attachments", "sandbox_cpu_cores", "sandbox_mem_mib",
          "gateway_cpu_cores", "gateway_cpu_total_s", "gateway_cpu_stat_ts", "gateway_mem_mib", "gateway_db_used_mib",
          "pool_cpu_cores", "pool_mem_mib", "networkpolicies", "policyendpoints", "sample_s"]
METRIC_FIELDS = ["ts", "supervisor_sessions", "relay_pending", "lock_wait_sum_s", "lock_wait_count",
                 "lock_hold_sum_s", "lock_hold_count", "lock_timeouts", "grpc_requests",
                 "create_calls", "delete_calls", "get_calls", "list_calls", "exec_calls",
                 "update_config_calls", "get_config_calls", "policy_loaded_poll", "policy_loaded_push"]

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


def _psi(block: dict | None) -> float | str:
    try:
        return block["psi"]["some"]["avg10"]
    except (KeyError, TypeError):
        return ""


def _epoch(iso: str) -> float | str:
    from datetime import datetime
    try:
        return round(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp(), 3)
    except ValueError:
        return ""


def _count(path: str) -> int | str:
    try:
        return len(kraw(path, timeout=10)["items"])
    except Exception:
        return ""


def sample(node_names: list[str], pool: ThreadPoolExecutor) -> list[dict]:
    t0 = time.time()
    summaries = {n: pool.submit(kraw, f"/api/v1/nodes/{n}/proxy/stats/summary", 20) for n in node_names}
    pods_f = pool.submit(kraw, "/api/v1/pods", 20)
    vas_f = pool.submit(kraw, "/apis/storage.k8s.io/v1/volumeattachments", 20)
    np_f = pool.submit(_count, f"/apis/networking.k8s.io/v1/namespaces/{NAMESPACE}/networkpolicies")
    pe_f = pool.submit(_count, f"/apis/networking.k8s.aws/v1alpha1/namespaces/{NAMESPACE}/policyendpoints")

    pods = pods_f.result()["items"]
    live = [p for p in pods if p["status"].get("phase") not in ("Succeeded", "Failed")]
    unscheduled = sum(1 for p in live if not p["spec"].get("nodeName"))
    sandbox_uids, p6_uids, pool_uids, per_node = set(), set(), set(), {}
    for p in live:
        node = p["spec"].get("nodeName")
        labels = p["metadata"].get("labels") or {}
        uid = p["metadata"]["uid"]
        if labels.get("app.kubernetes.io/name") == "openshell-supervisor":
            pool_uids.add(uid)
        if "openshell.ai/boundary-pair" in labels:
            sandbox_uids.add(uid)
            owners = [o.get("name", "") for o in p["metadata"].get("ownerReferences") or []]
            if p["metadata"]["name"].startswith(f"{WORKSPACE}--{PREFIX}") or any(
                    o.startswith(f"{WORKSPACE}--{PREFIX}") for o in owners):
                p6_uids.add(uid)
        if node:
            d = per_node.setdefault(node, {"scheduled": 0, "sandbox": 0, "p6": 0})
            d["scheduled"] += 1
            d["sandbox"] += uid in sandbox_uids
            d["p6"] += uid in p6_uids
    attached: dict[str, int] = {}
    for va in vas_f.result()["items"]:
        if (va.get("status") or {}).get("attached"):
            attached[va["spec"]["nodeName"]] = attached.get(va["spec"]["nodeName"], 0) + 1
    nps, pes = np_f.result(), pe_f.result()

    rows = []
    for n in node_names:
        try:
            s = summaries[n].result()
        except Exception as e:  # a slow or failed summary leaves a gap, not a crash
            print(f"summary {n}: {e}", file=sys.stderr)
            continue
        cpu = mem = 0.0
        gw_cpu = gw_total = gw_stat_ts = gw_mem = gw_db = pool_cpu = pool_mem = ""
        for p in s.get("pods", []):
            uid = p["podRef"]["uid"]
            c = (p.get("cpu") or {}).get("usageNanoCores", 0) / 1e9
            m = (p.get("memory") or {}).get("workingSetBytes", 0) / 2 ** 20
            if uid in sandbox_uids:
                cpu += c
                mem += m
            if p["podRef"]["namespace"] == NAMESPACE and p["podRef"]["name"] == "openshell-0":
                gw_cpu, gw_mem = round(c, 4), round(m, 1)
                # usageNanoCores is a rate over cAdvisor's last housekeeping window (10-15 s), so it
                # lags short steps; the cumulative counter and its own timestamp give exact rates.
                cpu_stats = p.get("cpu") or {}
                gw_total = round(cpu_stats.get("usageCoreNanoSeconds", 0) / 1e9, 3)
                gw_stat_ts = _epoch(cpu_stats.get("time", ""))
                for v in p.get("volume") or []:
                    if v.get("name") == "openshell-data" or v.get("pvcRef", {}).get("name", "").startswith("openshell-data"):
                        gw_db = round(v.get("usedBytes", 0) / 2 ** 20, 2)
            if uid in pool_uids:
                pool_cpu = round((pool_cpu or 0) + c, 4)
                pool_mem = round((pool_mem or 0) + m, 1)
        node = s["node"]
        rows.append({
            "ts": round(t0, 3), "node": n,
            "node_cpu_cores": round(node["cpu"].get("usageNanoCores", 0) / 1e9, 4),
            "node_mem_ws_mib": round(node["memory"].get("workingSetBytes", 0) / 2 ** 20, 1),
            "node_mem_avail_mib": round(node["memory"].get("availableBytes", 0) / 2 ** 20, 1),
            "cpu_psi_some_avg10": _psi(node.get("cpu")), "mem_psi_some_avg10": _psi(node.get("memory")),
            "io_psi_some_avg10": _psi(node.get("io")),
            "pods_running": len(s.get("pods", [])),
            "pods_scheduled": per_node.get(n, {}).get("scheduled", 0), "pods_unscheduled": unscheduled,
            "sandbox_pods": per_node.get(n, {}).get("sandbox", 0), "p6_pods": per_node.get(n, {}).get("p6", 0),
            "volume_attachments": attached.get(n, 0),
            "sandbox_cpu_cores": round(cpu, 4), "sandbox_mem_mib": round(mem, 1),
            "gateway_cpu_cores": gw_cpu, "gateway_cpu_total_s": gw_total, "gateway_cpu_stat_ts": gw_stat_ts,
            "gateway_mem_mib": gw_mem, "gateway_db_used_mib": gw_db,
            "pool_cpu_cores": pool_cpu, "pool_mem_mib": pool_mem,
            "networkpolicies": nps, "policyendpoints": pes, "sample_s": round(time.time() - t0, 2),
        })
    return rows


def metrics_row(gm: GatewayMetrics) -> dict | None:
    m = gm.scrape()
    if not m:
        return None
    calls = lambda method: metric_sum(m, "openshell_server_grpc_requests_total", method=method)  # noqa: E731
    return {
        "ts": round(time.time(), 3),
        "supervisor_sessions": metric_sum(m, "openshell_server_supervisor_sessions"),
        "relay_pending": metric_sum(m, "openshell_server_relay_pending"),
        "lock_wait_sum_s": round(metric_sum(m, "openshell_server_mutation_lock_wait_seconds_sum"), 4),
        "lock_wait_count": metric_sum(m, "openshell_server_mutation_lock_wait_seconds_count"),
        "lock_hold_sum_s": round(metric_sum(m, "openshell_server_mutation_lock_hold_seconds_sum"), 4),
        "lock_hold_count": metric_sum(m, "openshell_server_mutation_lock_hold_seconds_count"),
        "lock_timeouts": metric_sum(m, "openshell_server_mutation_lock_timeouts_total"),
        "grpc_requests": metric_sum(m, "openshell_server_grpc_requests_total"),
        "create_calls": calls("CreateSandbox"), "delete_calls": calls("DeleteSandbox"),
        "get_calls": calls("GetSandbox"), "list_calls": calls("ListSandboxes"),
        "exec_calls": calls("ExecSandbox"), "update_config_calls": calls("UpdateConfig"),
        "get_config_calls": calls("GetSandboxConfig"),
        "policy_loaded_poll": metric_sum(m, "openshell_supervisor_policy_apply_results_total", source="polling", outcome="loaded"),
        "policy_loaded_push": metric_sum(m, "openshell_supervisor_policy_apply_results_total", source="push", outcome="loaded"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=0, help="seconds; 0 = until stopped")
    ap.add_argument("--metrics-out", type=Path, help="also scrape the gateway's /metrics into this CSV")
    ap.add_argument("--metrics-port", type=int, default=19090)
    args = ap.parse_args()
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    node_names = sorted(n["metadata"]["name"] for n in kraw("/api/v1/nodes")["items"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    gm = GatewayMetrics(args.metrics_port) if args.metrics_out else None
    start = time.time()
    with args.out.open("w", newline="") as f, ThreadPoolExecutor(max_workers=8) as pool:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        mf = args.metrics_out.open("w", newline="") if args.metrics_out else None
        mw = csv.DictWriter(mf, fieldnames=METRIC_FIELDS) if mf else None
        if mw:
            mw.writeheader()
        try:
            while not _stop:
                tick = time.time()
                try:
                    w.writerows(sample(node_names, pool))
                    f.flush()
                except Exception as e:
                    print(f"sample failed: {e}", file=sys.stderr)
                if mw and (row := metrics_row(gm)):
                    mw.writerow(row)
                    mf.flush()
                if args.duration and time.time() - start >= args.duration:
                    break
                while not _stop and time.time() - tick < args.interval:
                    time.sleep(0.05)
        finally:
            if mf:
                mf.close()
            if gm:
                gm.close()
    print(f"sampler: wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
