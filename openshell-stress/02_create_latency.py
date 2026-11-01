"""Test 2: create latency under concurrency. For each level (default 1, 5, 10, 20, 40) start
that many CreateSandbox calls at once, time each sandbox from the call to Ready, count the ones
still not Ready after --deadline ("stuck"), then delete them all and start the next level from
zero. The gateway's fleet-wide mutation lock shows up in its own metrics, scraped before and
after each level.

    uv run python 02_create_latency.py                            # dedicated supervisors
    uv run python 02_create_latency.py --topology shared          # shared pool (Helm change, rolled back)
    uv run python 02_create_latency.py --levels 1,3 --repeats 1   # smoke
"""

from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from lib import (GatewayMetrics, PhaseWatcher, VALUES, add_results_arg, best_effort, create_rpc,
                 delete_all_p6, delete_sandboxes, gateway_values, handle_signals, helm_commands,
                 metric_sum, other_sandboxes, p6_client, require_gateway, results_dir, sampling,
                 say, summarize, wait_ready, write_csv, write_json)

TOPOLOGY = {"dedicated": [], "shared": [VALUES / "shared-supervisor.yaml"]}


def lock_totals(gm: GatewayMetrics) -> dict:
    m = gm.scrape()
    return {"wait_sum": metric_sum(m, "openshell_server_mutation_lock_wait_seconds_sum"),
            "wait_count": metric_sum(m, "openshell_server_mutation_lock_wait_seconds_count"),
            "hold_sum": metric_sum(m, "openshell_server_mutation_lock_hold_seconds_sum"),
            "hold_count": metric_sum(m, "openshell_server_mutation_lock_hold_seconds_count"),
            "global_wait_sum": metric_sum(m, "openshell_server_mutation_lock_wait_seconds_sum", scope="global"),
            "global_wait_count": metric_sum(m, "openshell_server_mutation_lock_wait_seconds_count", scope="global"),
            "timeouts": metric_sum(m, "openshell_server_mutation_lock_timeouts_total")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--levels", default="1,5,10,20,40")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--topology", choices=TOPOLOGY, default="dedicated")
    ap.add_argument("--deadline", type=float, default=300, help="seconds before a sandbox counts as stuck")
    ap.add_argument("--print-helm", action="store_true")
    add_results_arg(ap)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]
    files = TOPOLOGY[args.topology]
    if args.print_helm:
        up, down = helm_commands(files)
        print(" ".join(up) if files else "(dedicated: no gateway change)")
        if files:
            print(" ".join(down))
        return

    handle_signals()
    require_gateway()
    out = results_dir(args)
    tag = f"create-{args.topology}"
    rows: list[dict] = []
    per_level: list[dict] = []
    with p6_client() as c:
        if n_left := best_effort("delete", delete_all_p6, c):
            print(f"  deleted {n_left} p6 sandbox(es)")
        if others := other_sandboxes(c):
            print(f"note: {len(others)} other sandbox(es) on the gateway; the full run expects an empty cluster")

    with gateway_values(files, f"create latency {args.topology}"):
        with p6_client() as c, sampling(out / f"{tag}-sampler.csv", metrics_csv=out / f"{tag}-gateway.csv"), \
                GatewayMetrics(port=19091) as gm:
            watch = PhaseWatcher(c, interval=0.25)
            watch.start()
            try:
                for rep in range(1, args.repeats + 1):
                    for level in levels:
                        say(f"level {level} (repeat {rep}): {level} creates at once")
                        names = [f"p6-c{level:02d}-{rep}-{k + 1:02d}" for k in range(level)]
                        before = lock_totals(gm)
                        go = threading.Barrier(level)

                        def submit(nm: str) -> dict:
                            go.wait()  # every call leaves at the same moment
                            return create_rpc(c, nm)

                        t0 = time.time()
                        with ThreadPoolExecutor(max_workers=level) as ex:
                            batch = list(ex.map(submit, names))
                        wait_ready(watch, batch, args.deadline)
                        after = lock_totals(gm)
                        for r in batch:
                            r.update(topology=args.topology, level=level, repeat=rep, t_rel=round(r["t_submit"] - t0, 3))
                        rows += batch
                        ready = summarize([r.get("ready_s") for r in batch])
                        rpc = summarize([r["rpc_s"] for r in batch])
                        waits = after["wait_count"] - before["wait_count"]
                        rec = {"topology": args.topology, "level": level, "repeat": rep, "n": level,
                               "ready": sum(r["final_phase"] == "Ready" for r in batch),
                               "stuck": sum(bool(r.get("stuck")) for r in batch),
                               "failed": sum(r["final_phase"] in ("Error", "CreateFailed") for r in batch),
                               "ready_p50_s": round(ready["p50"], 3), "ready_p95_s": round(ready["p95"], 3),
                               "ready_max_s": round(ready["max"], 3), "ready_min_s": round(ready["min"], 3),
                               "rpc_p50_s": round(rpc["p50"], 3), "rpc_p95_s": round(rpc["p95"], 3),
                               "rpc_max_s": round(rpc["max"], 3),
                               "lock_waits": waits,
                               "lock_wait_mean_ms": round(1000 * (after["wait_sum"] - before["wait_sum"]) / waits, 2) if waits else 0,
                               "lock_hold_mean_ms": round(1000 * (after["hold_sum"] - before["hold_sum"]) /
                                                          max(1, after["hold_count"] - before["hold_count"]), 2),
                               "global_lock_wait_s": round(after["global_wait_sum"] - before["global_wait_sum"], 3),
                               "lock_timeouts": after["timeouts"] - before["timeouts"]}
                        print(f"  Ready {rec['ready']}/{level}, stuck {rec['stuck']}, failed {rec['failed']}   "
                              f"create→Ready p50 {rec['ready_p50_s']:.1f} s p95 {rec['ready_p95_s']:.1f} s max {rec['ready_max_s']:.1f} s   "
                              f"CreateSandbox call p50 {rec['rpc_p50_s']:.2f} s max {rec['rpc_max_s']:.2f} s   "
                              f"lock wait mean {rec['lock_wait_mean_ms']} ms", flush=True)
                        t = time.monotonic()
                        took = delete_sandboxes(c, names)
                        rec["delete_all_s"] = round(time.monotonic() - t, 1)
                        rec["delete_call_p50_s"] = round(summarize(list(took.values()))["p50"], 2)
                        print(f"  deleted in {rec['delete_all_s']} s")
                        per_level.append(rec)
            finally:
                watch.stop()
                write_csv(out / f"{tag}-sandboxes.csv", rows)
                write_csv(out / f"{tag}-levels.csv", per_level)
                if n_left := best_effort("delete", delete_all_p6, c):
                    print(f"  deleted {n_left} p6 sandbox(es)")
    write_json(out / f"{tag}-summary.json", {"topology": args.topology, "levels": per_level})
    print(f"results: {out}/{tag}-*.csv")


if __name__ == "__main__":
    main()
