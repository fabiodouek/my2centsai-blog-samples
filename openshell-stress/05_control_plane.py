"""Test 5: control-plane load. A few target sandboxes, then a ramp of parallel API clients
(default 1, 2, 4, 8, 16, 32, 64), each step running for --step-s seconds, every client cycling
through ListSandboxes (every page), GetSandbox and ExecSandbox (`true`) (probes/api_load.py).
Every call is timed; the sampler records the gateway Pod's CPU and memory, its SQLite volume,
and the NetworkPolicy and PolicyEndpoint counts; the gateway's /metrics are scraped per step.

  --from cluster   (default) the clients run in a Pod on the node without the gateway
                   (manifests/load-client.yaml), straight to the gateway's Service with the
                   CLI's client certificate
  --from laptop    the clients run here, through the CLI's port-forward, which goes through
                   the Kubernetes API server: every call pays that round trip (the Health
                   calls measured first show it), so this mostly measures the tunnel

    uv run python 05_control_plane.py
    uv run python 05_control_plane.py --targets 2 --levels 1,4 --step-s 5     # smoke
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import time

from concurrent.futures import ThreadPoolExecutor

from lib import (GatewayMetrics, HERE, NAMESPACE, PhaseWatcher, PROBES, add_results_arg,
                 best_effort, create_rpc, delete_all_p6, handle_signals, kubectl, metric_sum,
                 other_sandboxes, p6_client, require_gateway, results_dir, run, sampling, say,
                 summarize, wait_ready, workload_image, write_csv, write_json)

POD = "p6-load"
ENDPOINT = f"openshell.{NAMESPACE}.svc.cluster.local:8080"
_spec = importlib.util.spec_from_file_location("api_load", PROBES / "api_load.py")
api_load = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_load)


def start_load_pod(node: str) -> None:
    manifest = (HERE / "manifests" / "load-client.yaml").read_text()
    manifest = manifest.replace("${NODE}", node).replace("${IMAGE}", workload_image())
    kubectl("delete", "pod", "-n", NAMESPACE, POD, "--ignore-not-found", "--wait=true", timeout=120)
    kubectl("apply", "-f", "-", input=manifest)
    for _ in range(150):
        pod = json.loads(kubectl("-n", NAMESPACE, "get", "pod", POD, "-o", "json"))
        if pod["status"].get("phase") in ("Failed", "Succeeded"):
            logs = kubectl("-n", NAMESPACE, "logs", POD, check=False)[-400:]
            raise RuntimeError(f"load Pod {POD} exited: {logs}")
        if any(cnd["type"] == "Ready" and cnd["status"] == "True" for cnd in pod["status"].get("conditions", [])):
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"load Pod {POD} not Ready after 300 s")
    kubectl("exec", "-i", "-n", NAMESPACE, POD, "--", "sh", "-c", "cat > /tmp/api_load.py",
            input=(PROBES / "api_load.py").read_text())


def in_pod(cfg: dict) -> dict:
    cfg = {"endpoint": ENDPOINT, "tls_dir": "/etc/p6-client", **cfg}
    timeout = float(cfg.get("step_s", 0)) + 600
    r = run(["kubectl", "--context", "osh-eks", "exec", "-n", NAMESPACE, POD, "--", "env", "PYTHONPATH=/tmp/sdk",
             "python3", "/tmp/api_load.py", json.dumps(cfg)], timeout=timeout)
    return json.loads(r.stdout.strip().splitlines()[-1])


def gateway_window(sampler_csv, t0: float, t1: float) -> dict:
    """Gateway CPU over a step from the cumulative CPU counter (delta of CPU seconds over the
    delta of the counter's own timestamps), and its peak memory. The kubelet refreshes the
    counter every 10-15 s, so steps should be 30 s or longer for a clean number."""
    pts, mem = [], []
    with open(sampler_csv) as f:
        for r in csv.DictReader(f):
            if r.get("gateway_cpu_total_s") and r.get("gateway_cpu_stat_ts"):
                pts.append((float(r["gateway_cpu_stat_ts"]), float(r["gateway_cpu_total_s"])))
            if r.get("gateway_mem_mib") and t0 <= float(r["ts"]) <= t1:
                mem.append(float(r["gateway_mem_mib"]))
    pts = sorted(set(pts))
    inside = [p for p in pts if t0 - 2 <= p[0] <= t1 + 2]
    cpu = ""
    if len(inside) >= 2 and inside[-1][0] > inside[0][0]:
        cpu = round((inside[-1][1] - inside[0][1]) / (inside[-1][0] - inside[0][0]), 3)
    return {"gw_cpu_cores": cpu, "gw_cpu_window_s": round(inside[-1][0] - inside[0][0], 1) if len(inside) >= 2 else "",
            "gw_mem_max_mib": max(mem) if mem else ""}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="origin", choices=("cluster", "laptop"), default="cluster")
    ap.add_argument("--targets", type=int, default=5, help="sandboxes the get and exec calls go to")
    ap.add_argument("--levels", default="1,2,4,8,16,32,64", help="parallel clients per step")
    ap.add_argument("--step-s", type=float, default=30)
    ap.add_argument("--ops", default="list,get,exec")
    ap.add_argument("--health", type=int, default=50, help="sequential Health calls first (transport floor)")
    add_results_arg(ap)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]
    ops = args.ops.split(",")
    handle_signals()
    require_gateway()
    out = results_dir(args)
    tag = f"control-{args.origin}"
    names = [f"p6-cp{k + 1:02d}" for k in range(args.targets)]
    calls: list[dict] = []
    steps: list[dict] = []
    sampler_csv = out / f"{tag}-sampler.csv"
    gw_node = json.loads(kubectl("-n", NAMESPACE, "get", "pod", "openshell-0", "-o", "json"))["spec"]["nodeName"]
    other = next(n["metadata"]["name"] for n in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
                 if n["metadata"]["name"] != gw_node)
    with p6_client() as c:
        if n_left := best_effort("delete", delete_all_p6, c):
            print(f"  deleted {n_left} p6 sandbox(es)")
        if others := other_sandboxes(c):
            print(f"note: {len(others)} other sandbox(es) on the gateway; the full run expects an empty cluster")
        try:
            say(f"creating {args.targets} target sandboxes" + (f"; load client Pod {POD} on {other}" if args.origin == "cluster" else ""))
            watch = PhaseWatcher(c, interval=1.0)
            watch.start()
            with ThreadPoolExecutor(max_workers=args.targets) as ex:
                made = list(ex.map(lambda n: create_rpc(c, n), names))
            if args.origin == "cluster":
                start_load_pod(other)
            wait_ready(watch, made, 300)
            watch.stop()
            if bad := [r["name"] for r in made if r["final_phase"] != "Ready"]:
                raise RuntimeError(f"not Ready: {bad}")

            def level_run(level: int) -> dict:
                if args.origin == "cluster":
                    return in_pod({"level": level, "step_s": args.step_s, "ops": ops, "targets": names})
                return api_load.run_level(p6_client, level, args.step_s, ops, names)

            with sampling(sampler_csv, metrics_csv=out / f"{tag}-gateway.csv"), GatewayMetrics(port=19091) as gm:
                time.sleep(4)  # an idle sample or two first
                say(f"transport floor: {args.health} Health calls from the {args.origin}")
                h = in_pod({"health": args.health})["health_ms"] if args.origin == "cluster" \
                    else api_load.health(p6_client, args.health)
                hs = summarize(h)
                print(f"  Health p50 {hs['p50']:.1f} ms, p95 {hs['p95']:.1f} ms")
                calls += [{"level": 0, "op": "health", "ms": x, "ok": True, "error": ""} for x in h]

                for level in levels:
                    say(f"{level} parallel client(s), {args.step_s:.0f} s")
                    m0 = gm.scrape()
                    res = level_run(level)
                    m1 = gm.scrape()
                    mine = [{"level": level, "op": op, "ms": ms, "ok": ok, "error": err} for op, ms, ok, err in res["calls"]]
                    calls += mine
                    t0, t1 = res["t0"], res["t1"]
                    rec = {"origin": args.origin, "level": level, "t0": round(t0, 3), "t1": round(t1, 3),
                           "calls": len(mine), "calls_per_s": round(len(mine) / (t1 - t0), 1),
                           "errors": sum(not r["ok"] for r in mine),
                           "gw_grpc_calls": metric_sum(m1, "openshell_server_grpc_requests_total")
                           - metric_sum(m0, "openshell_server_grpc_requests_total"),
                           "gw_relay_pending_max": metric_sum(m1, "openshell_server_relay_pending")}
                    for op in ops:
                        s = summarize([r["ms"] for r in mine if r["op"] == op and r["ok"]])
                        rec.update({f"{op}_n": s["n"], f"{op}_p50_ms": round(s["p50"], 1), f"{op}_p95_ms": round(s["p95"], 1)})
                    if rec["errors"]:
                        rec["first_error"] = next(r["error"] for r in mine if not r["ok"])
                    steps.append(rec)
                    print(f"  {rec['calls_per_s']} calls/s, errors {rec['errors']}   " +
                          "   ".join(f"{op} p50 {rec[f'{op}_p50_ms']} ms p95 {rec[f'{op}_p95_ms']} ms" for op in ops), flush=True)
                time.sleep(4)
        finally:
            write_csv(out / f"{tag}-calls.csv", calls)
            if args.origin == "cluster":
                best_effort("load Pod", kubectl, "delete", "pod", "-n", NAMESPACE, POD, "--ignore-not-found",
                            "--wait=false", check=False)
            if n_left := best_effort("delete", delete_all_p6, c):
                print(f"  deleted {n_left} p6 sandbox(es)")
    if sampler_csv.exists():
        for rec in steps:
            rec.update(gateway_window(sampler_csv, rec["t0"], rec["t1"]))
    write_csv(out / f"{tag}-steps.csv", steps)
    write_json(out / f"{tag}-summary.json", {"origin": args.origin,
                                             "health_ms": summarize([r["ms"] for r in calls if r["op"] == "health"]),
                                             "steps": steps})
    for rec in steps:
        print(f"  {rec['level']:3d} clients: gateway CPU {rec.get('gw_cpu_cores')} cores "
              f"(over {rec.get('gw_cpu_window_s')} s of counter), memory max {rec.get('gw_mem_max_mib')} MiB")
    print(f"results: {out}/{tag}-*.csv")


if __name__ == "__main__":
    main()
