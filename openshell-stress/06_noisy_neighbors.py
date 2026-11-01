"""Test 6: noisy neighbours (runc). A probe sandbox (probes/noisy_probe.py) measures, once a
second, a fixed piece of CPU work, a 4 KiB write + fsync on its own EBS workspace volume and on
the node's root volume (/tmp), and one request to the echo server through its supervisor.
Hog sandboxes on the same node then run, one scenario at a time:

  cpu    busy loops, --cpu-threads per hog (8 = every vCPU of an m7i.2xlarge)
  disk   dd with fsync, --disk-jobs per hog, alternating between the hog's own EBS volume
         (/sandbox) and the node's root volume (/tmp)

with a baseline before and a rest after each scenario. By default nothing has CPU or memory
requests or limits (the chart's default for sandboxes); --hog-cpu sets a CPU limit on the hogs
(openshell sandbox create --cpu).

Every workload Pod is pinned to one node (default: the one the gateway is not on). Phase
boundaries come from the hogs' own clock, which is the probe's clock (same node). The laptop
also times an exec of `true` in the probe every 2 s, the path an agent's commands take.

    uv run python 06_noisy_neighbors.py
    uv run python 06_noisy_neighbors.py --hog-cpu 1               # hogs limited to one CPU each
    uv run python 06_noisy_neighbors.py --hogs 1 --baseline-s 15 --hog-s 20 --rest-s 10   # smoke
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from lib import (POLICIES, PROBES, WORKSPACE, add_results_arg, best_effort, create_cli,
                 delete_all_p6, handle_signals, kubectl, node_selector, p6_client, pods_of,
                 put_file, require_gateway, results_dir, sampling, say, sh_in, summarize, write_csv,
                 write_json)

PROBE = "p6-nn-probe"
CPU_HOG = """i=0; while [ $i -lt {n} ]; do (while :; do :; done) & i=$((i+1)); done; wait"""
DISK_HOG = """i=0
while [ $i -lt {n} ]; do
  ( while :; do
      for d in /sandbox /tmp; do
        dd if=/dev/zero of=$d/p6-hog-$i.bin bs=1M count=256 oflag=direct conv=fsync 2>>/tmp/p6-hog.err \\
          || {{ echo "dd to $d failed" >>/tmp/p6-hog.err; sleep 1; }}
      done
    done ) &
  i=$((i+1))
done
wait"""
METRICS = ("cpu_ms", "fsync_ws_ms", "fsync_tmp_ms", "net_ms")


def node_clock(c, name: str) -> float:
    return float(sh_in(c, name, "date +%s.%N").stdout.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", help="node for every workload Pod (default: the node without the gateway)")
    ap.add_argument("--hogs", type=int, default=3)
    ap.add_argument("--cpu-threads", type=int, default=8)
    ap.add_argument("--disk-jobs", type=int, default=2)
    ap.add_argument("--hog-cpu", help="CPU limit per hog sandbox, e.g. 1 (default: none)")
    ap.add_argument("--scenarios", default="cpu,disk")
    ap.add_argument("--baseline-s", type=float, default=60)
    ap.add_argument("--hog-s", type=float, default=120)
    ap.add_argument("--rest-s", type=float, default=30)
    add_results_arg(ap)
    args = ap.parse_args()
    handle_signals()
    require_gateway()
    out = results_dir(args)
    tag = "noisy" + (f"-limit{args.hog_cpu}" if args.hog_cpu else "")
    scenarios = args.scenarios.split(",")
    gw_node = json.loads(kubectl("-n", "openshell", "get", "pod", "openshell-0", "-o", "json"))["spec"]["nodeName"]
    node = args.node or next(n for n in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
                             if n["metadata"]["name"] != gw_node)["metadata"]["name"]
    hogs = [f"p6-nn-hog{k + 1}" for k in range(args.hogs)]
    pin = ["--driver-config-json", node_selector(node)]
    markers: list[dict] = []
    rtt: list[dict] = []
    samples: list[dict] = []
    with p6_client() as c:
        if n_left := best_effort("delete", delete_all_p6, c):
            print(f"  deleted {n_left} p6 sandbox(es)")
        try:
            say(f"probe and {args.hogs} hog sandbox(es), workloads pinned to {node} (gateway on {gw_node})")
            create_cli(PROBE, "--policy", str(POLICIES / "echo-l4.yaml"), *pin)
            limit = ["--cpu", args.hog_cpu] if args.hog_cpu else []
            with ThreadPoolExecutor(max_workers=args.hogs) as ex:
                # policies/hog.yaml: no network, and /dev/zero readable (Landlock denies it by default)
                list(ex.map(lambda h: create_cli(h, "--policy", str(POLICIES / "hog.yaml"), *pin, *limit), hogs))
            for n in [PROBE, *hogs]:
                where = pods_of(c.get(n, workspace=WORKSPACE).id)
                print(f"  {n}: workload {where.get('workload')}, supervisor {where.get('supervisor')}")
            put_file(c, PROBE, "/tmp/p6-noisy-probe.py", (PROBES / "noisy_probe.py").read_bytes())
            for h in hogs:
                put_file(c, h, "/tmp/p6-hog-cpu.sh", CPU_HOG.format(n=args.cpu_threads).encode())
                put_file(c, h, "/tmp/p6-hog-disk.sh", DISK_HOG.format(n=args.disk_jobs).encode())

            total = args.baseline_s + len(scenarios) * (args.hog_s + args.rest_s) + 30
            cfg = json.dumps({"interval": 1, "duration": total})
            sh_in(c, PROBE, f"rm -f /tmp/p6-noisy.jsonl; nohup setsid python3 /tmp/p6-noisy-probe.py '{cfg}' "
                            "/tmp/p6-noisy.jsonl >/dev/null 2>&1 < /dev/null &")

            stop_rtt = threading.Event()
            phase = {"name": "baseline"}

            def exec_rtt() -> None:
                while not stop_rtt.is_set():
                    t = time.perf_counter()
                    try:
                        ok = c.exec(PROBE, ["true"], workspace=WORKSPACE, no_login_shell=True, timeout_seconds=30).exit_code == 0
                    except Exception:
                        ok = False
                    rtt.append({"t": round(time.time(), 3), "phase": phase["name"], "ms": round((time.perf_counter() - t) * 1000, 2), "ok": ok})
                    stop_rtt.wait(2)
            rtt_thread = threading.Thread(target=exec_rtt, daemon=True)

            with sampling(out / f"{tag}-sampler.csv"):
                markers.append({"phase": "baseline", "t_on": node_clock(c, hogs[0])})
                rtt_thread.start()
                say(f"baseline, {args.baseline_s:.0f} s")
                time.sleep(args.baseline_s)
                for sc in scenarios:
                    say(f"{sc} hogs on, {args.hog_s:.0f} s")
                    phase["name"] = sc
                    with ThreadPoolExecutor(max_workers=len(hogs)) as ex:
                        starts = list(ex.map(lambda h: float(sh_in(
                            c, h, f"nohup setsid sh /tmp/p6-hog-{sc}.sh >/dev/null 2>&1 < /dev/null & date +%s.%N"
                        ).stdout.strip()), hogs))
                    markers.append({"phase": sc, "t_on": min(starts)})
                    if sc == "disk":  # a hog that can't write would only burn CPU: check, once
                        time.sleep(3)
                        chk = sh_in(c, hogs[0], "ls /sandbox/p6-hog-*.bin /tmp/p6-hog-*.bin 2>/dev/null | wc -l; "
                                                "tail -n 3 /tmp/p6-hog.err 2>/dev/null").stdout.split("\n", 1)
                        if chk[0].strip() == "0" or "failed" in (chk[1] if len(chk) > 1 else ""):
                            print(f"  WARNING: the disk hog isn't writing: {chk[-1].strip()[:300]}")
                        time.sleep(max(0.0, args.hog_s - 3))
                    else:
                        time.sleep(args.hog_s)
                    with ThreadPoolExecutor(max_workers=len(hogs)) as ex:
                        ends = list(ex.map(lambda h: float(sh_in(
                            c, h, "date +%s.%N; pkill -f '[p]6-hog-'; true").stdout.split()[0]), hogs))
                    markers[-1]["t_off"] = max(ends)
                    phase["name"] = f"after-{sc}"
                    say(f"{sc} hogs off, rest {args.rest_s:.0f} s")
                    markers.append({"phase": f"after-{sc}", "t_on": max(ends)})
                    time.sleep(args.rest_s)
                stop_rtt.set()
                rtt_thread.join(timeout=35)

            lines = sh_in(c, PROBE, "cat /tmp/p6-noisy.jsonl").stdout.splitlines()
            samples = [json.loads(x) for x in lines if x.startswith("{")]
        finally:
            for h in hogs:
                best_effort(f"stop {h}", sh_in, c, h, "pkill -f '[p]6-hog-'; true")
            best_effort("stop probe", sh_in, c, PROBE, "pkill -f '[p]6-noisy-probe'; true")
            if n_left := best_effort("delete", delete_all_p6, c):
                print(f"  deleted {n_left} p6 sandbox(es)")

    # Label each probe sample with the phase it fell in (hog clock = probe clock, same node).
    bounds = sorted(markers, key=lambda m: m["t_on"])   # each phase runs until the next one starts
    for s in samples:
        s["phase"] = next((m["phase"] for m in reversed(bounds) if s["t"] >= m["t_on"]), "before")
    t0 = bounds[0]["t_on"] if bounds else 0
    for s in samples:
        s["t_rel"] = round(s["t"] - t0, 2)
    write_csv(out / f"{tag}-probe.csv", samples, ["t", "t_rel", "phase", *METRICS,
                                                   *[m + "_err" for m in METRICS]])
    write_csv(out / f"{tag}-exec.csv", rtt)
    write_csv(out / f"{tag}-phases.csv", [{**m, "t_on_rel": round(m["t_on"] - t0, 2),
                                           "t_off_rel": round(m["t_off"] - t0, 2) if "t_off" in m else ""} for m in markers])
    summary = {"node": node, "hogs": args.hogs, "hog_cpu_limit": args.hog_cpu, "cpu_threads": args.cpu_threads,
               "disk_jobs": args.disk_jobs, "phases": {}}
    for ph in ["baseline", *[x for sc in scenarios for x in (sc, f"after-{sc}")]]:
        rows = [s for s in samples if s["phase"] == ph]
        summary["phases"][ph] = {m: summarize([s.get(m) for s in rows]) for m in METRICS}
        summary["phases"][ph]["exec_ms"] = summarize([r["ms"] for r in rtt if r["phase"] == ph and r["ok"]])
        p = summary["phases"][ph]
        print(f"  {ph:12} cpu p50 {p['cpu_ms']['p50']:7.1f} ms   fsync EBS p50 {p['fsync_ws_ms']['p50']:6.2f} ms "
              f"p95 {p['fsync_ws_ms']['p95']:6.2f}   fsync root p50 {p['fsync_tmp_ms']['p50']:6.2f} ms p95 {p['fsync_tmp_ms']['p95']:6.2f}   "
              f"echo p50 {p['net_ms']['p50']:5.2f} ms   exec p50 {p['exec_ms']['p50']:6.1f} ms")
    write_json(out / f"{tag}-summary.json", summary)
    print(f"results: {out}/{tag}-*.csv")


if __name__ == "__main__":
    main()
