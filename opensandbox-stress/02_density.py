"""Density: how many idle sandboxes the two m7i.2xlarge nodes take, three ways, and what stops them.

Modes (--modes, default runc,runc-egress,kata):
  runc         no network policy: one container per sandbox
  runc-egress  a network policy (deny, allow the echo host) and the credential proxy: every sandbox
               gets the egress sidecar with its DNS proxy, nftables and transparent proxy, as parts
               4 and 5 run it. The sidecar asks the scheduler for nothing (the server sets no
               [egress] requests), so what changes is the memory the nodes really use.
  kata         the server on kata-qemu (server_config.py --runtime kata-qemu): a VM per sandbox.
               The RuntimeClass reserves 250m CPU and 160Mi per pod on top of the request. The
               nodes run Kata with sandbox_cgroup_only = false, so part of a VM's processes (QEMU,
               virtiofsd, the shim) may be accounted outside the pod's cgroup. Rather than rely on
               that mechanism, the script measures what the kubelet's pod number leaves out: for
               these runs a sampler pod on each node (manifests/vmm-sampler.yaml, privileged,
               hostPID) reads each VM's RSS from /proc, and the results set it beside the kubelet's
               number for the same pod, with where that number came from (the pod cgroup or the
               containers' sum): results/density-vmm.csv and node_side in results/density.json.
               That sum of RSS is an upper bound: a page several processes map (the guest's RAM,
               which QEMU and virtiofsd both map, shared libraries) counts once in each.
  runc-policy  (not in the default set) a network policy without the credential proxy

Each run starts from zero sandboxes and creates them --batch at a time (all of a batch at once)
until a create fails or --max are running. When a create fails, the script reads why from the
scheduler (the PodScheduled message of the pod it could not place, or the FailedScheduling event:
the server deletes a sandbox whose create timed out) and reports the resource that ran out: pod
slots, CPU or memory requests, IP addresses, or "timeout" for a placed pod that did not get ready
in time. After every batch it records the running count per node, the nodes' working set, the
share of pod slots, CPU and memory requested, and the memory of the sandboxes themselves. The
egress sidecar's own RSS comes from the per-container numbers (results/density-containers.csv).

What one more sandbox costs is how much the nodes' working set grows per sandbox added
(node_growth_mib: from the first batch to the last one before a create failed; measure.node_growth).
For the egress sidecar, egress_node_cost_mib is that growth with the sidecar minus without it;
egress_memory_ratio compares the pods' own working sets.

Every mode runs RUNS times (default 3) from zero; the results give the median ceiling and the
spread. The sandboxes are idle (shape --cpu 100m --memory 128Mi, requests = limits), so the
numbers are the platform's own cost per sandbox.

The server is switched to kata-qemu for the kata runs and always put back to runc, the base
egress image and dns+nft (with the gateway), checked with server_config.py --show --json.

Writes results/density.json, results/density.csv (t,node,pod,cpu_m,mem_mib),
results/density-containers.csv and, for Kata, results/density-vmm.csv (t,node,pod,kind,rss_mib).
Deletes every sandbox it made (metadata example=opensandbox-stress) and the sampler pods.

--resummarize recomputes the summaries in results/density.json from the runs stored in it and
prints the report, with no cluster calls (for a summary added after the runs).
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from collections import Counter

from checks import allocation, node_problems, refusal
from common import (
    ECHO_HOST,
    RESULTS,
    RUNS,
    SANDBOX_NAMESPACE,
    VMM_IMAGE,
    CsvSink,
    Failures,
    Plan,
    Sampler,
    VmmSampler,
    cli,
    create_sandbox,
    delete_run,
    deny_policy,
    heading,
    kube_json,
    new_run_id,
    own_metadata,
    parallel,
    pods_by_sandbox,
    preconditions,
    restore_failed,
    save,
    say,
    server_config,
    server_session,
    show,
    spread,
    wait_no_pods,
)
from measure import mem_mib_of, node_growth, node_growth_span, vmm_by_pod, vmm_gap

SCRIPT = "02_density"
MODES = ("runc", "runc-egress", "kata", "runc-policy")
DEFAULT_MODES = "runc,runc-egress,kata"
RUNTIME = {"runc": "runc", "runc-egress": "runc", "runc-policy": "runc", "kata": "kata-qemu"}
SETTLE = 7          # seconds after a batch, so the 5-second sampler has seen it
FINAL_SETTLE = 20   # at the ceiling: the newest pods' memory and CPU settle
# Minutes per run, for the plan: creates, the failed batch (it waits for the server's 60 s create
# timeout) and the teardown (a sandbox pod takes up to its 30 s grace period to go).
ESTIMATE = {"runc": 6, "runc-egress": 7, "kata": 5, "runc-policy": 6}


def arguments(parser) -> None:
    parser.add_argument("--modes", default=DEFAULT_MODES, help=f"comma-separated, of {', '.join(MODES)}")
    parser.add_argument("--batch", type=int, default=10, help="sandboxes created at once (default 10)")
    parser.add_argument("--max", type=int, default=200, help="stop at this many even if the cluster takes more")
    parser.add_argument("--cpu", default="100m", help="CPU request and limit of each sandbox (default 100m)")
    parser.add_argument("--memory", default="128Mi", help="memory request and limit of each sandbox (default 128Mi)")
    parser.add_argument("--resummarize", action="store_true",
                        help="recompute the summaries of results/density.json from its runs; no cluster calls")


class Watcher:
    """Polls the run's pods every 2 s and keeps every pod it saw that the scheduler could not place
    (the server deletes those when their create times out, so they are gone by the time we look)."""

    def __init__(self, run_id: str):
        self.selector = f"run={run_id}"
        self.stuck: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pods = kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", self.selector, timeout=30)
                for pod in pods.get("items", []):
                    conditions = pod.get("status", {}).get("conditions", [])
                    if any(c.get("type") == "PodScheduled" and c.get("status") == "False" for c in conditions):
                        self.stuck[pod["metadata"]["name"]] = pod
            except Exception:  # noqa: BLE001 - a missed poll is fine
                pass
            self._stop.wait(2)

    def __enter__(self) -> "Watcher":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=35)


def median_or_none(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def kubelet_view(sampler: Sampler, run_id: str, ids: list[str]) -> dict[str, dict]:
    """{pod name: the kubelet's latest numbers} for this run's sandboxes, found by their
    opensandbox.io/id label (pods_by_sandbox), not by an assumed pod name."""
    pods = sampler.pods_now()
    names = pods_by_sandbox(f"run={run_id}")
    return {names[sid]["pod"]: pods[names[sid]["pod"]] for sid in ids if sid in names and names[sid]["pod"] in pods}


def snapshot(sampler: Sampler, run_id: str, ids: list[str], mode: str) -> dict:
    """The state after a batch: running sandboxes per node, node totals, requests, and per-sandbox
    memory (pod level; the main container and the egress sidecar on their own)."""
    mine = list(kubelet_view(sampler, run_id, ids).values())
    per_node = Counter(p["node"] for p in mine)
    sidecar = [c.get("rss_mib") for p in mine for name, c in p.get("containers", {}).items() if "egress" in name]
    main = [c.get("mem_mib") for p in mine for name, c in p.get("containers", {}).items() if "egress" not in name]
    container_sum = [sum((c.get("mem_mib") or 0) for c in p.get("containers", {}).values()) if p.get("containers") else None
                     for p in mine]
    outside = [round(p["mem_mib"] - s, 1) for p, s in zip(mine, container_sum) if p.get("mem_mib") is not None and s]
    return {
        "running": len(ids), "seen_by_kubelet": len(mine), "per_node": dict(sorted(per_node.items())),
        "node_totals": sampler.node_totals(),
        "requested": allocation(kube_json("get", "pods", "-A", timeout=60), kube_json("get", "nodes")),
        "mem_source": dict(Counter(p.get("mem_source") for p in mine)),
        "sandbox_mem_mib": median_or_none([p.get("mem_mib") for p in mine]),
        "sandbox_rss_mib": median_or_none([p.get("rss_mib") for p in mine]),
        "main_mem_mib": median_or_none(main),
        "sidecar_rss_mib": median_or_none(sidecar) if mode in ("runc-egress", "runc-policy") else None,
        "outside_containers_mib": median_or_none(outside),
    }


def node_side(vmm: VmmSampler, sampler: Sampler, run_id: str, ids: list[str]) -> dict:
    """The node's view of this run's VMs (the last half minute of the VM sampler) against the
    kubelet's view of the same pods (the Sampler's latest sample). Also says whether the kubelet's
    pod number came from the pod cgroup or stands for the containers' sum (mem_source)."""
    kubelet = kubelet_view(sampler, run_id, ids)
    vms = {k: v for k, v in vmm_by_pod(vmm.now()).items() if k in kubelet or k.startswith("sandbox:")}
    out = vmm_gap(vms, kubelet)
    out["kubelet_mem_source"] = dict(Counter(p.get("mem_source") for p in kubelet.values()))
    return out


def one_run(mode: str, n: int, sampler: Sampler, args, failures: Failures, vmm: VmmSampler | None = None) -> dict:
    run_id = new_run_id()
    meta = {**own_metadata(SCRIPT, run_id), "mode": mode}
    policy = deny_policy(ECHO_HOST) if mode in ("runc-egress", "runc-policy") else None
    proxy = mode == "runc-egress"
    shape = {"cpu": args.cpu, "memory": args.memory}
    ids: list[str] = []
    batches: list[dict] = []
    stop = {"limit": "cap", "detail": f"--max {args.max} reached"}
    heading(f"{mode}, run {n} of {RUNS} (sandboxes tagged run={run_id})")

    def create(_: int) -> str:
        sb = create_sandbox(meta, resource=shape, policy=policy, proxy=proxy, ttl_minutes=90, ready_seconds=120,
                            retry=False)
        sandbox_id = sb.id
        sb.close()
        return sandbox_id

    try:
        with sampler.phase(f"{mode} run {n}", mode=mode, run=n), Watcher(run_id) as watcher:
            while len(ids) < args.max:
                size = min(args.batch, args.max - len(ids))
                batch_start = time.time()
                results = parallel(create, range(size), workers=size, together=True)
                ids += [r.value for r in results if r.ok]
                failed = [r for r in results if not r.ok]
                time.sleep(SETTLE)
                state = snapshot(sampler, run_id, ids, mode)
                state.update({"batch": len(batches) + 1, "created_ms": [round(r.ms) for r in results if r.ok],
                              "failed": len(failed)})
                batches.append(state)
                nodes_mem = ", ".join(f"{k} {v['mem_mib'] / 1024:.1f} GiB" for k, v in state["node_totals"].items()
                                      if v.get("mem_mib"))
                say(f"batch {state['batch']:2}: {len(ids):3} running {state['per_node']}, {len(failed)} failed; "
                    f"nodes {nodes_mem}; sandbox {state['sandbox_mem_mib']} MiB")
                if failed:
                    events = kube_json("-n", SANDBOX_NAMESPACE, "get", "events", timeout=60)
                    events["items"] = [e for e in events.get("items", [])
                                       if (e.get("lastTimestamp") or e.get("eventTime") or "") >= time.strftime(
                                           "%Y-%m-%dT%H:%M:%S", time.gmtime(batch_start - 5))]
                    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(batch_start - 2))
                    stuck = [pod for pod in watcher.stuck.values()
                             if pod["metadata"].get("creationTimestamp", "") >= since]   # this batch's pods only
                    limit, detail = refusal({"items": stuck}, events)
                    stop = {"limit": limit, "detail": show(detail),
                            "errors": sorted({show((r.error_type or "") + ": " + (r.error or "")[:160]) for r in failed})[:3]}
                    say(f"refused: {limit} ({detail[:150]})")
                    break
                pressure = [p for p in node_problems(kube_json("get", "nodes")) if "Pressure" in p]
                if pressure:
                    stop = {"limit": "node-pressure", "detail": "; ".join(pressure)}
                    say(f"stopping: {stop['detail']}")
                    break
            time.sleep(FINAL_SETTLE)
            final = snapshot(sampler, run_id, ids, mode)
            if final["seen_by_kubelet"] < 0.9 * final["running"]:
                failures.add(f"{mode} run {n}: the kubelet reports {final['seen_by_kubelet']} of {final['running']} "
                             "running sandboxes: the memory figures cover only those")
            if vmm is not None:
                final["vmm"] = node_side(vmm, sampler, run_id, ids)
                v = final["vmm"]
                if v["vms"] < 0.9 * final["running"]:
                    failures.add(f"{mode} run {n}: the node-side sampler saw {v['vms']} VM(s) for {final['running']} "
                                 "running sandboxes: the node-side figures cover only those (see logs/vmm-*.log)")
                say(f"node side: a VM's processes' summed RSS {v['vmm_total_mib']} MiB (QEMU {v['qemu_mib']}; an "
                    f"upper bound), the kubelet {v['kubelet_mem_mib']} MiB for its pod ({v['joined']} of {v['vms']} "
                    "VMs paired by pod)")
    except Exception as error:  # noqa: BLE001 - one line, then clean up
        failures.add(f"{mode} run {n}: {type(error).__name__}: {str(error)[:200]}")
        # Not a ceiling: the run stopped on an error, not on the cluster refusing a sandbox.
        stop = {"limit": "error", "detail": show(f"{type(error).__name__}: {str(error)[:200]}")}
        final = {}
    finally:
        say(f"deleting {len(ids)} sandboxes of this run")
        delete_run(run_id)
        if not wait_no_pods(f"run={run_id}", timeout=900):
            failures.add(f"{mode} run {n}: pods of the run still exist 15 minutes after the delete")
    return {"mode": mode, "run": n, "ceiling": len(ids), "stop": stop, "batches": batches, "final": final}


INVALID_STOPS = ("error", "unknown")   # the run did not end on a capacity limit: not a ceiling


def summarize(runs: list[dict], mode: str, runtimeclass: dict | None, failures: Failures) -> dict:
    valid = [r for r in runs if r["stop"]["limit"] not in INVALID_STOPS]
    invalid = [r for r in runs if r["stop"]["limit"] in INVALID_STOPS]
    if len(valid) < len(runs):
        failures.add(f"{mode}: {len(invalid)} of {len(runs)} run(s) ended on an error or an unknown refusal: "
                     f"left out of the ceiling ({', '.join(r['stop']['limit'] for r in invalid)})")
    finals = [r["final"] for r in valid if r.get("final")]
    out = {
        "ceiling": spread([r["ceiling"] for r in valid]),
        "valid_runs": len(valid),
        "invalid_runs": [{"run": r["run"], "ceiling": r["ceiling"], "stop": r["stop"]} for r in invalid],
        "limit": Counter(r["stop"]["limit"] for r in valid).most_common(1)[0][0] if valid else None,
        "limits": [r["stop"]["limit"] for r in runs],
        "sandbox_mem_mib": spread([f.get("sandbox_mem_mib") for f in finals]),
        "sandbox_rss_mib": spread([f.get("sandbox_rss_mib") for f in finals]),
        "sidecar_rss_mib": spread([f.get("sidecar_rss_mib") for f in finals]),
        "outside_containers_mib": spread([f.get("outside_containers_mib") for f in finals]),
        "node_growth_mib": spread([node_growth(r["batches"]) for r in valid]),
        "node_growth_span": [node_growth_span(r["batches"]) for r in valid],
        "runs": runs,
    }
    if mode == "kata" and runtimeclass:
        out["runtimeclass_overhead"] = runtimeclass
    vms = [f["vmm"] for f in finals if f.get("vmm")]
    if vms:
        out["node_side"] = {key: spread([v.get(key) for v in vms])
                            for key in ("vmm_total_mib", "qemu_mib", "virtiofsd_mib", "shim_mib", "kubelet_mem_mib",
                                        "kubelet_rss_mib", "gap_mib")}
        out["node_side"]["paired_by"] = [v.get("paired_by") for v in vms]
    return out


def write_vmm_csv(samples: list[dict], t0: float) -> None:
    """results/density-vmm.csv: t (seconds on the Sampler's clock), node, pod, kind, rss_mib."""
    sink = CsvSink(RESULTS / "density-vmm.csv", ["t", "node", "pod", "kind", "rss_mib"])
    sink.write([[round(s["t"] - t0, 1), s["node"], s["pod"] or f"sandbox:{(s['sandbox'] or '-')[:16]}", s["kind"],
                 s["rss_mib"]] for s in sorted(samples, key=lambda s: (s["t"], s["node"], s["pid"]))])
    sink.close()
    print(f"  results/density-vmm.csv: {sink.rows} rows")


def add_comparisons(results: dict) -> None:
    """The egress sidecar against plain runc: the pods' working sets (a ratio) and what a sandbox
    costs the nodes (a difference of node_growth_mib medians)."""
    modes = results["modes"]
    if "runc" in modes and "runc-egress" in modes:
        base, egress = modes["runc"]["sandbox_mem_mib"]["median"], modes["runc-egress"]["sandbox_mem_mib"]["median"]
        results["egress_memory_ratio"] = round(egress / base, 2) if base and egress else None
        base, egress = ((modes[m].get("node_growth_mib") or {}).get("median") for m in ("runc", "runc-egress"))
        results["egress_node_cost_mib"] = round(egress - base, 1) if base is not None and egress is not None else None


def report(results: dict) -> None:
    heading("density")
    print(f"  {'mode':<12} {'ceiling (median, spread)':>26}  {'limit':<14} {'pod MiB':>8} {'node MiB':>9} "
          f"{'sidecar RSS':>12}")
    for mode, s in results["modes"].items():
        side = s["sidecar_rss_mib"]["median"]
        growth = (s.get("node_growth_mib") or {}).get("median")
        print(f"  {mode:<12} {str(s['ceiling']['median']) + ' (' + str(s['ceiling']['spread']) + ')':>26}  "
              f"{str(s['limit']):<14} {str(s['sandbox_mem_mib']['median']):>8} {str(growth):>9} "
              f"{str(side) if side else '-':>12}")
    print("  pod MiB: a sandbox pod's working set; node MiB: how much the nodes' working set grew per sandbox "
          "added (the cost)")
    if results.get("egress_memory_ratio"):
        print(f"  the egress sidecar: the pod's working set x{results['egress_memory_ratio']}; on the nodes "
              f"+{results.get('egress_node_cost_mib')} MiB per sandbox")
    if "kata" in results["modes"]:
        k = results["modes"]["kata"]
        node_view = k.get("node_side") or {}
        overhead = (k.get("runtimeclass_overhead") or {}).get("memory")
        reserved = mem_mib_of(results["shape"]["memory"]) + mem_mib_of(overhead)
        growth = k.get("node_growth_mib") or {}
        print(f"  kata: the scheduler reserves {reserved:.0f} MiB per sandbox ({results['shape']['memory']} request "
              f"+ the RuntimeClass's {overhead or '?'} overhead)")
        span = k.get("node_growth_span") or []
        print(f"        the nodes' working set grows {growth.get('median')} MiB per sandbox (runs: "
              f"{', '.join(str(v) for v in growth.get('runs', [])) or '-'}; each over "
              f"{', '.join(str(v) for v in span) or '?'} sandboxes added)")
        if node_view:
            print(f"        the kubelet counts {node_view['kubelet_mem_mib']['median']} MiB for the pod; the summed RSS "
                  f"of a VM's processes is {node_view['vmm_total_mib']['median']} MiB (QEMU "
                  f"{node_view['qemu_mib']['median']}), an upper bound: a page that QEMU and virtiofsd both map "
                  "(the guest's RAM) counts in each (medians of the runs)")


def resummarize(dry_run: bool) -> int:
    """--resummarize: the summaries again from results/density.json's own runs, no cluster calls.
    With --dry-run, print them and write nothing."""
    path = RESULTS / "density.json"
    if not path.is_file():
        sys.exit("results/density.json is not there: nothing to resummarize")
    results = json.loads(path.read_text())
    failures = Failures()
    for mode, old in results["modes"].items():
        results["modes"][mode] = summarize(old["runs"], mode, old.get("runtimeclass_overhead"), failures)
    add_comparisons(results)
    if dry_run:
        print("  --dry-run: the summaries recomputed from results/density.json's runs; nothing written")
    else:
        save("density", results)
        print("  results/density.json: summaries recomputed from its runs")
    report(results)
    return failures.report()


def main() -> int:
    args = cli(__doc__, arguments)
    if args.resummarize:
        return resummarize(args.dry_run)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODES]
    if unknown or not modes:
        sys.exit(f"unknown mode(s) {unknown}: choose from {', '.join(MODES)}")
    modes.sort(key=lambda m: RUNTIME[m] != "runc")  # the runc modes first: one switch to Kata, one back
    plan = Plan(f"02_density.py: idle sandboxes until refusal, {', '.join(modes)}, {RUNS} run(s) each")
    plan.checks.append("no sandboxes and no Pools on the cluster; server runc + base egress + dns+nft + gateway;"
                       " 2 nodes Ready")
    for mode in modes:
        if RUNTIME[mode] != "runc" and "server_config.py --runtime kata-qemu" not in plan.server_changes:
            plan.step("server_config.py --runtime kata-qemu (the server pod is replaced)", 1)
            plan.server_changes.append("server_config.py --runtime kata-qemu")
            plan.step(f"node-side VM sampler: one privileged hostPID pod per node ({VMM_IMAGE}), reading each VM's "
                      "RSS from /proc for the Kata runs; deleted after them (also on failure)", 0.5)
        for n in range(1, RUNS + 1):
            plan.step(f"{mode} run {n}: batches of {args.batch} ({args.cpu}/{args.memory}) until a create fails "
                      f"or {args.max} run; then delete them and wait for the pods to go", ESTIMATE[mode])
    if plan.server_changes:
        plan.restores.append("server_config.py --baseline, checked with --show --json")
        plan.step("server back to runc (in a finally, also after a failure)", 1)
    plan.writes += ["results/density.json", "results/density.csv", "results/density-containers.csv"]
    if "kata" in modes:
        plan.writes.append("results/density-vmm.csv")
    plan.print()
    if args.dry_run:
        return 0

    preconditions()
    failures = Failures()
    results: dict = {"shape": {"cpu": args.cpu, "memory": args.memory}, "batch": args.batch, "max": args.max,
                     "runs_per_mode": RUNS, "modes": {}}
    runtimeclass = None
    with server_session(), Sampler("density", every=5.0, containers=True) as sampler:
        current = "runc"
        for mode in modes:
            if RUNTIME[mode] != current:
                server_config("--runtime", RUNTIME[mode])
                current = RUNTIME[mode]
                if current.startswith("kata"):
                    rc = kube_json("get", "runtimeclass", current)
                    runtimeclass = rc.get("overhead", {}).get("podFixed")
            if mode == "kata":
                with VmmSampler(every=5) as vmm:
                    runs = [one_run(mode, n, sampler, args, failures, vmm) for n in range(1, RUNS + 1)]
                write_vmm_csv(vmm.samples, sampler.t0)
            else:
                runs = [one_run(mode, n, sampler, args, failures) for n in range(1, RUNS + 1)]
            results["modes"][mode] = summarize(runs, mode, runtimeclass, failures)
            s = results["modes"][mode]
            say(f"{mode}: ceiling median {s['ceiling']['median']} (spread {s['ceiling']['spread']}), "
                f"limit {s['limit']}; sandbox {s['sandbox_mem_mib']['median']} MiB")
        results["phases"] = sampler.phase_list()
        results["sampler"] = {"every_s": sampler.every, "started_at": sampler.started_at}
    add_comparisons(results)
    save("density", results)
    report(results)
    if restore_failed():
        failures.add("the server was not put back to the baseline")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
