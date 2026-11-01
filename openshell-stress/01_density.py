"""Test 1: density until something refuses. Create sandboxes in batches (all of a batch at
once), wait for each to be Ready, and stop at the first create that fails or the first sandbox
still not Ready after --deadline seconds. Then name what stopped it, from the stuck sandboxes'
Pods, PVCs, VolumeAttachments and events: Pods per node, EBS attachments per node, memory, or
something else.

Three variants:
  dedicated    the chart default: one supervisor Pod per sandbox (2 Pods + 1 EBS volume each)
  shared       the experimental shared supervisor pool (values/shared-supervisor.yaml)
  shared-1gi   the pool plus 1 GiB workspace volumes (+ values/workspace-1gi.yaml)

The shared variants change the gateway's Helm values for the duration of the run and roll the
release back to its previous revision at the end, also on Ctrl-C. Every sandbox is named
p6-dNNN and deleted at the end.

    uv run python 01_density.py --variant dedicated
    uv run python 01_density.py --variant shared --print-helm     # show the Helm commands only
    uv run python 01_density.py --variant dedicated --batch 3 --max 3 --deadline 120   # smoke
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from lib import (PhaseWatcher, VALUES, add_results_arg, cluster_counts, create_rpc, delete_all_p6,
                 delete_sandboxes, diagnose, gateway_values, handle_signals, helm_commands, kraw,
                 list_p6, node_limits, other_sandboxes, p6_client, require_gateway, results_dir,
                 sampling, say, summarize, wait_ready, write_csv, write_json)

VARIANTS = {
    "dedicated": [],
    "shared": [VALUES / "shared-supervisor.yaml"],
    "shared-1gi": [VALUES / "shared-supervisor.yaml", VALUES / "workspace-1gi.yaml"],
}


def placement() -> dict[str, dict[str, str]]:
    """{sandbox id: {workload: node, supervisor: node}} from one Pod list."""
    out: dict[str, dict[str, str]] = {}
    for p in kraw("/api/v1/namespaces/openshell/pods")["items"]:
        labels = p["metadata"].get("labels") or {}
        sid = labels.get("openshell.ai/boundary-pair")
        if sid:
            out.setdefault(sid, {})[labels.get("openshell.ai/boundary-role", "?")] = p["spec"].get("nodeName") or "(unscheduled)"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", choices=VARIANTS, default="dedicated")
    ap.add_argument("--batch", type=int, default=10, help="sandboxes created at once per batch")
    ap.add_argument("--max", type=int, default=150, help="stop after this many sandboxes")
    ap.add_argument("--deadline", type=float, default=240, help="seconds a sandbox may take to become Ready")
    ap.add_argument("--print-helm", action="store_true", help="print the Helm commands for the variant and exit")
    add_results_arg(ap)
    args = ap.parse_args()
    files = VARIANTS[args.variant]
    if args.print_helm:
        up, down = helm_commands(files)
        print(" ".join(up) if files else "(dedicated: no gateway change)")
        if files:
            print(" ".join(down))
        return

    handle_signals()
    require_gateway()
    out = results_dir(args)
    tag = f"density-{args.variant}"
    with p6_client() as c:
        if n_left := delete_all_p6(c):
            print(f"deleted {n_left} leftover p6 sandbox(es)")
        if others := other_sandboxes(c):
            print(f"note: {len(others)} other sandbox(es) on the gateway; the full run expects an empty cluster")

    limits = node_limits()
    say(f"density, variant {args.variant}: batches of {args.batch}, up to {args.max}, deadline {args.deadline:.0f} s")
    for n, l in limits.items():
        print(f"  {n} ({l['zone']}): max pods {l['max_pods']}, EBS attachments (CSINode) {l['ebs_limit']}")
    base = cluster_counts()
    for n, d in sorted(base["nodes"].items()):
        print(f"  {n}: {d['pods']} pods already scheduled, {d['volume_attachments']} EBS volumes attached")

    rows: list[dict] = []
    batches: list[dict] = []
    stop: dict | None = None
    deleted: dict[str, float] = {}
    t_run = time.time()
    with gateway_values(files, f"density {args.variant}"):
        with p6_client() as c, sampling(out / f"{tag}-sampler.csv", metrics_csv=out / f"{tag}-gateway.csv"):
            watch = PhaseWatcher(c, interval=1.0)
            watch.start()
            try:
                i = b = 0
                while i < args.max and stop is None:
                    b += 1
                    names = [f"p6-d{i + k + 1:03d}" for k in range(min(args.batch, args.max - i))]
                    t0 = time.time()
                    with ThreadPoolExecutor(max_workers=len(names)) as ex:
                        batch = list(ex.map(lambda nm: create_rpc(c, nm), names))
                    wait_ready(watch, batch, args.deadline)
                    where = placement()
                    for r in batch:
                        r.update(variant=args.variant, batch=b, t_rel=round(r["t_submit"] - t_run, 2),
                                 workload_node=where.get(r["id"], {}).get("workload", ""),
                                 supervisor_node=where.get(r["id"], {}).get("supervisor", ""))
                    rows += batch
                    i += len(names)
                    counts = cluster_counts()
                    ready = sum(1 for r in rows if r["final_phase"] == "Ready")
                    lat = summarize([r.get("ready_s") for r in batch])
                    rec = {"variant": args.variant, "batch": b, "requested": i, "ready": ready,
                           "batch_s": round(time.time() - t0, 1), "ready_p50_s": round(lat["p50"], 2),
                           "ready_max_s": round(lat["max"], 2), "pending_pods": counts["pending_pods"], "t_rel": round(t0 - t_run, 1),
                           "t_end": round(time.time(), 3)}
                    for k, (node, d) in enumerate(sorted(counts["nodes"].items())):
                        rec.update({f"node{k}": node, f"node{k}_pods": d["pods"], f"node{k}_sandbox_pods": d["sandbox_pods"],
                                    f"node{k}_ebs": d["volume_attachments"]})
                    batches.append(rec)
                    per_node = ", ".join(f"{n.split('.')[0]}: {d['pods']} pods/{d['volume_attachments']} EBS"
                                         for n, d in sorted(counts["nodes"].items()))
                    print(f"  batch {b:3d}: {ready:4d} Ready of {i:4d}   create→Ready p50 {lat['p50']:5.1f} s "
                          f"max {lat['max']:5.1f} s   {per_node}   unscheduled {counts['pending_pods']}", flush=True)
                    bad = [r for r in batch if r["final_phase"] != "Ready"]
                    if bad:
                        stop = diagnose(bad)
                        stop["first_failed"] = [f"{r['name']}: {r['final_phase']}" for r in bad]
                if stop is None:
                    stop = {"cause": f"reached --max {args.max} (no refusal)", "evidence": []}
            finally:
                watch.stop()
                write_csv(out / f"{tag}-sandboxes.csv", rows)
                write_csv(out / f"{tag}-batches.csv", batches)
                # Every p6 sandbox, not just the rows: a Ctrl-C mid-batch leaves creates without a row.
                names = sorted({r["name"] for r in rows} | {r.name for r in list_p6(c)})
                say(f"deleting {len(names)} sandbox(es)")
                t = time.monotonic()
                deleted = delete_sandboxes(c, names)
                print(f"  {len(deleted)} deleted in {time.monotonic() - t:.0f} s (16 at a time)")

    ready = sum(1 for r in rows if r["final_phase"] == "Ready")
    summary = {"variant": args.variant, "batch": args.batch, "deadline_s": args.deadline, "limits": limits,
               "baseline": base, "requested": len(rows), "max_ready": ready,
               "stopped_by": stop["cause"] if stop else "interrupted", "first_failed": (stop or {}).get("first_failed", []),
               "evidence": (stop or {}).get("evidence", []),
               "ready_s": summarize([r.get("ready_s") for r in rows]),
               "delete_call_s": summarize(list(deleted.values()))}
    write_json(out / f"{tag}-summary.json", summary)
    say(f"stopped by: {summary['stopped_by']}  ({ready} Ready of {len(rows)} requested)")
    for e in summary["evidence"][:12]:
        print(f"  {e[:220]}")
    print(f"results: {out}/{tag}-*.csv, {tag}-summary.json")


if __name__ == "__main__":
    main()
