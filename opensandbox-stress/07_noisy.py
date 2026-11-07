"""Noisy neighbors: what CPU and disk hogs do to the sandboxes next to them, under runc and Kata.

Per runtime (--runtimes, default runc,kata; the server is switched to kata-qemu for the second
and put back to runc at the end), RUNS runs, each:
  - 4 victims: 1 CPU / 1 GiB, requests = limits (Guaranteed QoS), spread over the two nodes by
    the scheduler. Each runs loadgen/victim.py: 5 iterations of a fixed CPU job (sha256 over
    300 MiB), a 64 MiB write with fsync, and 100 small appends with an fsync each.
  - three scenarios, one after the other, with the same victims:
      baseline  nothing else running
      cpu       8 hogs, each spinning 2 processes (requests 100m, limit 2 CPU: the scheduler packs
                them, and together they want all 16 vCPUs of the two nodes)
      disk      8 hogs, each writing 64 MiB files with fsync round and round (the node's root
                EBS volume, where every container's writable layer lives under runc, and Kata's
                shared root filesystem too)
    The hogs start 15 s before the victims measure, and are deleted (and their pods gone) before
    the next scenario.
After the warm-up and again after the victims measured, the script checks that every hog is
running, from inside its sandbox (loadgen/hog_probe.py): its hog.py processes in /proc; for a CPU
hog, the CPU they used over 2 s as the sandbox's own kernel counts it (inside a Kata VM, the
guest's: the kubelet reports 0 for a Kata pod); for a disk hog, its latest write. A scenario with
a hog that is not running is invalid, reported with the reason and left out of the summary. The
kubelet's CPU for each hog's pod is recorded beside it (kubelet_cpu_m) but decides nothing.
A hog is started as an execd background command with a timeout longer than its own --seconds:
execd kills a background command's process group when its timeout runs out.
The results give each victim's node and how many hogs shared it, so a victim the scheduler put
away from the hogs is visible as such; the summary is over the victims that shared a node with at
least one hog. The CPU numbers show what CPU requests buy under contention (the kernel shares CPU
time by them); the disk numbers show what is not isolated at all.

Writes results/noisy.json and results/noisy.csv (every pod's CPU and memory every 2 s). Both are
written from scratch, so the script refuses a --runtimes that would drop the records of a runtime
already in results/noisy.json, unless --replace is given.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter

from opensandbox import SandboxSync

from checks import dropped_runtimes, hog_verdict
from common import (
    RESULTS,
    RUNS,
    Failures,
    Plan,
    Sampler,
    cli,
    connection,
    create_sandbox,
    delete_run,
    describe,
    heading,
    last_json,
    new_run_id,
    own_metadata,
    parallel,
    pods_by_sandbox,
    preconditions,
    put_loadgen,
    restore_failed,
    run_in,
    save,
    say,
    server_config,
    server_session,
    spread,
    wait_no_pods,
)

SCRIPT = "07_noisy"
VICTIMS = 4
HOGS = 8
VICTIM_SHAPE = {"cpu": "1", "memory": "1Gi"}
HOG_LIMITS = {"cpu": "2", "memory": "512Mi"}
HOG_REQUESTS = {"cpu": "100m", "memory": "256Mi"}
WARM_S = 15
SCENARIOS = ("baseline", "cpu", "disk")
RUNTIMES = {"runc": "runc", "kata": "kata-qemu"}
HOG_SECONDS = 900          # hog.py stops by itself after this (the sandbox is deleted long before)
HOG_COMMAND = {"cpu": f"python3 /tmp/hog.py cpu --workers 2 --seconds {HOG_SECONDS}",
               "disk": f"python3 /tmp/hog.py disk --mb 64 --files 8 --seconds {HOG_SECONDS}"}
# execd applies a command's timeout to a background command too and kills its process group when it
# runs out: a hog's must outlast hog.py's own --seconds, or the hogs die mid-scenario.
HOG_TIMEOUT_S = HOG_SECONDS + 60
PROBE_WINDOW_S = 2         # loadgen/hog_probe.py reads the hog's CPU this far apart


def arguments(parser) -> None:
    parser.add_argument("--runtimes", default="runc,kata", help="comma-separated, of runc, kata")
    parser.add_argument("--iterations", type=int, default=5, help="victim.py iterations per scenario (default 5)")
    parser.add_argument("--replace", action="store_true",
                        help="overwrite results/noisy.json even if it holds a runtime this run leaves out")


def connect(sandbox_id: str) -> SandboxSync:
    return SandboxSync.connect(sandbox_id, connection_config=connection("proxy"))


def start_hogs(kind: str, run_id: str) -> list[str]:
    meta = {**own_metadata(SCRIPT, run_id), "role": f"hog-{kind}"}

    def make(_: int) -> str:
        sb = create_sandbox(meta, resource=HOG_LIMITS, resource_requests=HOG_REQUESTS, ttl_minutes=30)
        try:
            put_loadgen(sb, "hog.py")
            put_loadgen(sb, "hog_probe.py")
            run_in(sb, HOG_COMMAND[kind], timeout=HOG_TIMEOUT_S, background=True)
            return sb.id
        finally:
            sb.close()

    made = parallel(make, range(HOGS), workers=HOGS)
    failed = [r for r in made if not r.ok]
    if failed:
        raise RuntimeError(f"{len(failed)} hog(s) did not start: {failed[0].error[:160]}")
    return [r.value for r in made]


def check_hogs(kind: str, hogs: list[str], sampler: Sampler, run_id: str) -> dict:
    """Are the hogs really running? loadgen/hog_probe.py in each hog's sandbox, judged by
    checks.hog_verdict: its hog.py processes, for CPU hogs the CPU they used over PROBE_WINDOW_S as
    the sandbox's kernel counts it, for disk hogs how recently a file was written. The kubelet's CPU
    for the pod goes beside it (0 under Kata) and decides nothing. {ok, running, hogs: [...]}."""
    names = pods_by_sandbox(f"run={run_id}")

    def probe(sandbox_id: str) -> dict:
        sb = connect(sandbox_id)
        try:
            out = run_in(sb, f"python3 /tmp/hog_probe.py --window {PROBE_WINDOW_S}", timeout=60)
            if out.rc != 0:
                raise RuntimeError(f"hog_probe.py exited {out.rc}: {out.text[-160:]}")
            return last_json(out.text)
        finally:
            sb.close()

    found = parallel(probe, hogs, workers=len(hogs))
    pods = sampler.pods_now()
    out = []
    for result in found:
        info = result.value if result.ok else None
        running, why = hog_verdict(kind, info)
        if not result.ok:
            why = f"the probe failed: {(result.error or '')[:120]}"
        pod = names.get(result.item, {}).get("pod", "")
        out.append({"procs": (info or {}).get("procs"), "cpu_m": (info or {}).get("cpu_m"),
                    "newest_write_s": (info or {}).get("newest_write_s"),
                    "kubelet_cpu_m": (pods.get(pod) or {}).get("cpu_m"), "running": running, "why": why})
    return {"ok": all(h["running"] for h in out), "running": sum(h["running"] for h in out), "hogs": out}


def measure_victims(victims: list[str], iterations: int) -> list[dict]:
    def bench(sandbox_id: str) -> dict:
        sb = connect(sandbox_id)
        try:
            out = run_in(sb, f"python3 /tmp/victim.py --iterations {iterations}", timeout=900)
            if out.rc != 0:
                raise RuntimeError(f"victim.py exited {out.rc}: {out.text[-160:]}")
            return last_json(out.text)
        finally:
            sb.close()

    results = parallel(bench, victims, workers=len(victims), together=True)
    bad = [r for r in results if not r.ok]
    if bad:
        raise RuntimeError(f"{len(bad)} victim(s) failed: {bad[0].error[:160]}")
    return [r.value for r in results]


def one_run(runtime: str, n: int, args, sampler: Sampler, failures: Failures) -> dict:
    run_id = new_run_id()
    victim_meta = {**own_metadata(SCRIPT, run_id), "role": "victim"}
    heading(f"{runtime}, run {n} of {RUNS}: {VICTIMS} victims (run={run_id})")
    record: dict = {"runtime": runtime, "run": n, "victims": [], "scenarios": {}}
    try:
        def make_victim(_: int) -> str:
            sb = create_sandbox(victim_meta, resource=VICTIM_SHAPE, ttl_minutes=60)
            try:
                put_loadgen(sb, "victim.py")
                return sb.id
            finally:
                sb.close()

        made = parallel(make_victim, range(VICTIMS), workers=VICTIMS)
        victims = [r.value for r in made if r.ok]
        if len(victims) < VICTIMS:
            raise RuntimeError(f"only {len(victims)} of {VICTIMS} victims were created")
        placed = pods_by_sandbox(f"run={run_id}")
        nodes = {v: placed.get(v, {}).get("node", "") for v in victims}
        if any(not node for node in nodes.values()):
            raise RuntimeError("could not find the node of every victim (by its opensandbox.io/id label)")
        say("victims on " + ", ".join(f"{k}: {c}" for k, c in sorted(Counter(nodes.values()).items())))
        for scenario in SCENARIOS:
            hog_run = new_run_id()
            hogs: list[str] = []
            try:
                checks: dict = {}
                with sampler.phase(f"{runtime} {scenario} {n}", runtime=runtime, scenario=scenario, run=n):
                    if scenario != "baseline":
                        hogs = start_hogs(scenario, hog_run)
                        time.sleep(WARM_S)
                        checks["before"] = check_hogs(scenario, hogs, sampler, hog_run)
                    hog_placed = pods_by_sandbox(f"run={hog_run}") if hogs else {}
                    hog_nodes = Counter(hog_placed.get(h, {}).get("node", "?") for h in hogs)
                    measured = measure_victims(victims, args.iterations)
                    if scenario != "baseline":
                        checks["during"] = check_hogs(scenario, hogs, sampler, hog_run)
                valid = scenario == "baseline" or (checks["before"]["ok"] and checks["during"]["ok"])
                if not valid:
                    why = next((f"{when}: {h['why']}" for when in ("before", "during") for h in checks[when]["hogs"]
                                if not h["running"]), "?")
                    failures.add(f"{runtime} run {n} {scenario}: not every hog was running (before: "
                                 f"{checks['before']['running']}/{len(hogs)}, during: {checks['during']['running']}/"
                                 f"{len(hogs)}; first: {why}): the scenario is invalid and left out of the summary")
                per_victim = []
                for sandbox_id, values in zip(victims, measured):
                    node = nodes[sandbox_id]
                    per_victim.append({"node": node, "hogs_on_node": hog_nodes.get(node, 0), **values})
                record["scenarios"][scenario] = {"hogs_per_node": dict(sorted(hog_nodes.items())), "victims": per_victim,
                                                 "hog_checks": checks, "valid": valid}
                cpu = describe("cpu", [v for p in per_victim for v in p["cpu_ms"]])
                io = describe("io", [v for p in per_victim for v in p["io_ms"]])
                fs = describe("fsync", [v for p in per_victim for v in p["fsync_ms"]])
                say(f"{scenario:>8}: cpu p50 {cpu['p50']} ms, 64 MiB+fsync p50 {io['p50']} ms, fsync p99 {fs['p99']} ms; "
                    f"hogs {dict(sorted(hog_nodes.items())) or 'none'}")
            finally:
                if hogs:
                    delete_run(hog_run)
                    wait_no_pods(f"run={hog_run}", timeout=600)
        record["victims"] = [{"node": nodes[v]} for v in victims]
    finally:
        delete_run(run_id)
        wait_no_pods(f"run={run_id}", timeout=600)
    return record


def summarize(records: list[dict]) -> dict:
    """runtime -> scenario -> {cpu_ms, io_ms, fsync_ms (pooled over exposed victims), per-run p50 spread,
    slowdown vs baseline}."""
    out: dict = {}
    for runtime in sorted({r["runtime"] for r in records}):
        runs = [r for r in records if r["runtime"] == runtime]
        out[runtime] = {}
        for scenario in SCENARIOS:
            def exposed(p: dict, scenario: str = scenario) -> bool:
                return scenario == "baseline" or p["hogs_on_node"] > 0

            valid_runs = [r for r in runs if r["scenarios"].get(scenario, {}).get("valid", False)]
            victims = [p for r in valid_runs for p in r["scenarios"][scenario]["victims"] if exposed(p)]
            entry = {"victims": len(victims), "valid_runs": len(valid_runs),
                     "invalid_runs": [r["run"] for r in runs if r not in valid_runs]}
            for metric in ("cpu_ms", "io_ms", "fsync_ms"):
                entry[metric] = describe(f"{runtime} {scenario} {metric}", [v for p in victims for v in p[metric]])
                entry[f"{metric}_p50_by_run"] = spread([
                    describe("", [v for p in r["scenarios"][scenario]["victims"] if exposed(p)
                                  for v in p[metric]])["p50"] for r in valid_runs])
            out[runtime][scenario] = entry
        base = out[runtime].get("baseline", {})
        for scenario in ("cpu", "disk"):
            entry = out[runtime].get(scenario)
            if not entry or not base:
                continue
            entry["slowdown_p50"] = {m: (round(entry[m]["p50"] / base[m]["p50"], 2)
                                         if entry[m]["p50"] and base[m]["p50"] else None)
                                     for m in ("cpu_ms", "io_ms", "fsync_ms")}
            entry["slowdown_p99"] = {m: (round(entry[m]["p99"] / base[m]["p99"], 2)
                                         if entry[m]["p99"] and base[m]["p99"] else None)
                                     for m in ("cpu_ms", "io_ms", "fsync_ms")}
    return out


def main() -> int:
    args = cli(__doc__, arguments)
    runtimes = [r.strip() for r in args.runtimes.split(",") if r.strip()]
    if not runtimes or any(r not in RUNTIMES for r in runtimes):
        sys.exit("--runtimes takes runc and/or kata")
    runtimes.sort(key=lambda r: r != "runc")
    plan = Plan(f"07_noisy.py: {VICTIMS} victims, {HOGS} CPU or disk hogs, {', '.join(runtimes)}, {RUNS} run(s)")
    plan.checks.append("no sandboxes and no Pools; server runc + base egress + dns+nft + gateway; 2 nodes Ready")
    for runtime in runtimes:
        if runtime != "runc":
            plan.server_changes.append(f"server_config.py --runtime {RUNTIMES[runtime]}")
            plan.step(f"server_config.py --runtime {RUNTIMES[runtime]}", 1)
        for n in range(1, RUNS + 1):
            plan.step(f"{runtime} run {n}: {VICTIMS} victims; baseline, then {HOGS} CPU hogs, then {HOGS} disk hogs "
                      f"(each warmed {WARM_S} s, then deleted)", 3 if runtime == "runc" else 3.5)
    if plan.server_changes:
        plan.restores.append("server_config.py --baseline, checked with --show --json")
    plan.writes += ["results/noisy.json", "results/noisy.csv"]
    plan.print()
    previous = RESULTS / "noisy.json"
    dropped = dropped_runtimes(json.loads(previous.read_text()) if previous.is_file() else None, runtimes)
    if dropped and not args.replace:
        sys.exit(f"results/noisy.json holds {', '.join(dropped)} records that --runtimes {args.runtimes} would drop "
                 "(both files are written from scratch): run every runtime, or pass --replace")
    if args.dry_run:
        return 0

    preconditions()
    failures = Failures()
    records: list[dict] = []
    with server_session(), Sampler("noisy", every=2.0) as sampler:
        current = "runc"
        for runtime in runtimes:
            if RUNTIMES[runtime] != current:
                server_config("--runtime", RUNTIMES[runtime])
                current = RUNTIMES[runtime]
            for n in range(1, RUNS + 1):
                try:
                    records.append(one_run(runtime, n, args, sampler, failures))
                except Exception as error:  # noqa: BLE001
                    failures.add(f"{runtime} run {n}: {type(error).__name__}: {str(error)[:200]}")
        phases = sampler.phase_list()
    results = {"victims": VICTIMS, "hogs": HOGS, "victim_shape": VICTIM_SHAPE, "hog_limits": HOG_LIMITS,
               "hog_requests": HOG_REQUESTS, "iterations": args.iterations, "records": records,
               "summary": summarize(records), "phases": phases}
    save("noisy", results)

    heading("victims, p50 (and slowdown against baseline)")
    for runtime, scenarios in results["summary"].items():
        for scenario, s in scenarios.items():
            slow = s.get("slowdown_p50") or {}
            print(f"  {runtime:<5} {scenario:<8} cpu {s['cpu_ms']['p50']} ms ({slow.get('cpu_ms', '-')}x)   "
                  f"64 MiB+fsync {s['io_ms']['p50']} ms ({slow.get('io_ms', '-')}x)   "
                  f"fsync p99 {s['fsync_ms']['p99']} ms   [{s['victims']} exposed victim(s)]")
    if restore_failed():
        failures.add("the server was not put back to the baseline")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
