"""The control plane under load: what the lifecycle server and the controller use, and when the API
slows down.

The server is one replica (requests 250m CPU / 512Mi, limits 1 CPU / 2Gi, part 2's values) with
SQLite on an EBS volume; the controller is one replica too. The sampler reads both every 2 s.

1. idle: 30 s with nothing happening, the floor.
2. API ramp, RUNS times: 20 idle sandboxes exist (so a list returns something and a status poll has
   ids to poll), and a Job next to the server (manifests/api-load.yaml, loadgen/api_load.py) runs
   1, 4, 16 and 64 clients for 30 s each, every client on a keep-alive connection alternating
   GET /v1/sandboxes?pageSize=20 and GET /v1/sandboxes/<id>. The Job calls the server's Service in
   the cluster: from the laptop, the kubectl port-forward would be the bottleneck being measured.
   For each level: requests per second, latency p50/p95/p99, error statuses, and the server's and
   the controller's CPU (mean, peak) and memory (peak) over that window.
3. create and delete waves, RUNS times: 10, 20 and 40 sandboxes created at once from here, then
   deleted at once; per wave the create latency, the time until every pod is gone, and the two
   pods' CPU and memory.

The Job reads the API key from the server's own Secret; nothing prints it. The server is not
changed. Writes results/control_plane.json, results/control_plane.csv and
results/control_plane-containers.csv (per container, with the cumulative CPU counter the per-level
averages come from). Deletes its sandboxes,
the Job and its ConfigMap.
"""

from __future__ import annotations

import json
import sys
import time

from opensandbox.sync import SandboxManagerSync

from checks import counted
from common import (
    LOADGEN,
    RESULTS,
    RUNS,
    SYSTEM_NAMESPACE,
    Failures,
    Plan,
    Sampler,
    cli,
    connection,
    create_sandbox,
    delete_run,
    describe,
    ensure_port_forward,
    heading,
    kubectl,
    log,
    new_run_id,
    own_metadata,
    parallel,
    preconditions,
    read_csv,
    render_manifest,
    save,
    say,
    show,
    spread,
    wait_no_pods,
    window_cpu,
    window_stats,
)
from measure import Phase

SCRIPT = "06_control_plane"
JOB = "osb-stress-api-load"
SHAPE = {"cpu": "100m", "memory": "128Mi"}
PODS = {"server": "opensandbox-server-", "controller": "opensandbox-controller-manager-"}
CONTAINERS = {"server": "main", "controller": "manager"}   # the one container of each pod that matters
IDLE_S = 30


def arguments(parser) -> None:
    parser.add_argument("--levels", default="1,4,16,64", help="API clients at once (default 1,4,16,64)")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="seconds per level (default 30: the CPU average needs counter readings 10 s apart)")
    parser.add_argument("--waves", default="10,20,40", help="create/delete wave sizes (default 10,20,40)")
    parser.add_argument("--skip", default="", help="'ramp' or 'waves' to leave one out")


def usage(rows: list[dict], start: float, end: float, crows: list[dict] | None = None,
          unix: tuple[float, float] | None = None) -> dict:
    """The server's and the controller's CPU (mean and peak of the kubelet's rate) and memory (peak)
    between start and end (sampler clock). With the per-container rows and the window in Unix time
    (the Job's clock: a node's, as the kubelet's stamps are), also cpu_avg_m: the average over that
    window from the container's cumulative CPU counter, readings picked by their own stamps; None
    ("unknown") when no two readings 10 s apart fall inside it."""
    out = {}
    for name, prefix in PODS.items():
        stats = window_stats(rows, prefix, start, end)
        if crows is not None and unix is not None:
            pods = sorted({r["pod"] for r in crows if r["pod"].startswith(prefix)})
            # The pods with readings in the window (a server pod replaced mid-run has none in the later
            # windows); unknown only when none has.
            values = [v for v in (window_cpu(crows, pod, CONTAINERS[name], *unix) for pod in pods) if v is not None]
            stats["cpu_avg_m"] = round(sum(values), 1) if values else None
        out[name] = stats
    return out


def render_job(levels: str, duration: float, ids: list[str]) -> str:
    return render_manifest("api-load.yaml", LEVELS=levels, DURATION=f"{duration:g}", IDS=",".join(ids))


def configmap() -> str:
    return json.dumps({"apiVersion": "v1", "kind": "ConfigMap",
                       "metadata": {"name": JOB, "namespace": SYSTEM_NAMESPACE,
                                    "labels": {"example": "opensandbox-stress"}},
                       "data": {"api_load.py": (LOADGEN / "api_load.py").read_text()}})


def delete_job() -> None:
    kubectl("-n", SYSTEM_NAMESPACE, "delete", "job", JOB, "--ignore-not-found", "--wait=true", "--timeout=120s",
            "--cascade=foreground", check=False)
    kubectl("-n", SYSTEM_NAMESPACE, "delete", "configmap", JOB, "--ignore-not-found", check=False)


def ramp(n: int, args, sampler: Sampler, ids: list[str]) -> list[dict]:
    """One Job run of every level; returns one record per level (times on the sampler's clock)."""
    delete_job()
    kubectl("apply", "-f", "-", stdin=configmap())
    kubectl("apply", "-f", "-", stdin=render_job(args.levels, args.duration, ids))
    levels = [int(x) for x in args.levels.split(",") if x]
    budget = 120 + len(levels) * (args.duration + 10)
    kubectl("-n", SYSTEM_NAMESPACE, "wait", "--for=condition=complete", f"job/{JOB}", f"--timeout={budget:.0f}s",
            check=False, timeout=budget + 30)
    out = kubectl("-n", SYSTEM_NAMESPACE, "logs", f"job/{JOB}", check=False, timeout=60)
    log("api_load", out)
    records = []
    for line in out.splitlines():
        line = line.strip()
        if not (line.startswith("{") and '"level"' in line):
            continue
        level = json.loads(line)
        start, end = round(level["start"] - sampler.t0, 1), round(level["end"] - sampler.t0, 1)
        sampler.phases.append(Phase(name=f"api x{level['level']} run {n}", start=start, end=end))
        errors = {code: count for code, count in level["statuses"].items() if code != "200"}
        records.append({"run": n, "level": level["level"], "rps": level["rps"], "requests": level["ok"],
                        "failed": sum(errors.values()), "errors": errors,
                        "latency": describe(f"api x{level['level']}", level["ms"]), "start": start, "end": end,
                        "start_unix": level["start"], "end_unix": level["end"]})
    delete_job()
    if len(records) != len(levels):
        raise RuntimeError(f"the API load Job reported {len(records)} of {len(levels)} levels (see logs/api_load.log)")
    return records


def wave(size: int, n: int, sampler: Sampler) -> dict:
    run_id = new_run_id()
    meta = {**own_metadata(SCRIPT, run_id), "step": f"wave{size}"}

    def create(_: int) -> str:
        sb = create_sandbox(meta, resource=SHAPE, ttl_minutes=20, retry=False)
        sandbox_id = sb.id
        sb.close()
        return sandbox_id

    def kill(sandbox_id: str) -> None:
        with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
            manager.kill_sandbox(sandbox_id)

    try:
        ensure_port_forward()
        with sampler.phase(f"create x{size} run {n}") as created_phase:
            made = parallel(create, range(size), workers=size, together=True)
        ids = [r.value for r in made if r.ok]
        time.sleep(10)
        with sampler.phase(f"delete x{size} run {n}") as deleted_phase:
            killed = parallel(kill, ids, workers=max(1, len(ids)), together=True)
            started = time.monotonic()
            gone = wait_no_pods(f"run={run_id}", timeout=600)
            pods_gone_s = round(time.monotonic() - started, 1) if gone else None
        return {"size": size, "run": n, "created": len(ids), "failed": size - len(ids),
                "create": describe(f"create x{size}", [r.ms for r in made if r.ok]),
                "kill": describe(f"kill x{size}", [r.ms for r in killed if r.ok]), "pods_gone_s": pods_gone_s,
                "create_window": [created_phase.start, created_phase.end],
                "delete_window": [deleted_phase.start, deleted_phase.end]}
    finally:
        delete_run(run_id)


def main() -> int:
    args = cli(__doc__, arguments)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    levels = [int(x) for x in args.levels.split(",") if x]
    sizes = [int(x) for x in args.waves.split(",") if x]
    plan = Plan(f"06_control_plane.py: API ramp {args.levels} clients, waves of {args.waves}, {RUNS} run(s) each")
    plan.checks.append("no sandboxes and no Pools; server runc + base egress + dns+nft + gateway; 2 nodes Ready")
    plan.step(f"{IDLE_S} s idle: the server's and the controller's floor", IDLE_S / 60)
    if "ramp" not in skip:
        plan.step("create 20 idle sandboxes (100m/128Mi) for the API to list and poll", 1)
        for n in range(1, RUNS + 1):
            plan.step(f"ramp {n}: Job {JOB} in {SYSTEM_NAMESPACE}: {args.levels} clients x {args.duration:g} s, "
                      "list and status polls on the server's Service", round((len(levels) * (args.duration + 5) + 60) / 60, 1))
        plan.step("delete the 20 sandboxes", 0.5)
    if "waves" not in skip:
        for n in range(1, RUNS + 1):
            plan.step(f"waves {n}: create {args.waves} sandboxes at once, then delete them at once", 1.5 * len(sizes))
    plan.writes += ["results/control_plane.json", "results/control_plane.csv", "results/control_plane-containers.csv"]
    plan.print()
    if args.dry_run:
        return 0

    preconditions()
    failures = Failures()
    results: dict = {"levels": levels, "duration_s": args.duration, "waves": [], "ramp": []}
    hold_run = new_run_id()
    idle_window = (0.0, 0.0)
    try:
        with Sampler("control_plane", every=2.0, containers=True) as sampler:
            heading(f"idle, {IDLE_S} s")
            with sampler.phase("idle") as idle:
                time.sleep(IDLE_S)
            if "ramp" not in skip:
                heading("API ramp: 20 sandboxes to list and poll")
                meta = {**own_metadata(SCRIPT, hold_run), "step": "ramp"}

                def hold(_: int) -> str:
                    sb = create_sandbox(meta, resource=SHAPE, ttl_minutes=60)
                    sb.close()
                    return sb.id

                made = parallel(hold, range(20), workers=10)
                ids = [r.value for r in made if r.ok]
                say(f"{len(ids)} sandboxes up")
                for n in range(1, RUNS + 1):
                    heading(f"API ramp, run {n} of {RUNS} (in-cluster Job)")
                    try:
                        for record in ramp(n, args, sampler, ids):
                            results["ramp"].append(record)
                            say(f"x{record['level']:>3} clients: {record['rps']} req/s, p50 {record['latency']['p50']} ms, "
                                f"p99 {record['latency']['p99']} ms"
                                + (f"; FAILED {record['failed']} {record['errors']} (not timed; this run left out of "
                                   "the level's medians)" if record["failed"] else "; no errors"))
                            if record["failed"] and not record["requests"]:
                                failures.add(f"ramp {n} x{record['level']}: no call answered 200 ({record['errors']})")
                    except Exception as error:  # noqa: BLE001
                        failures.add(f"ramp {n}: {type(error).__name__}: {str(error)[:200]}")
                delete_run(hold_run)
                wait_no_pods(f"run={hold_run}", timeout=300)
            if "waves" not in skip:
                for n in range(1, RUNS + 1):
                    for size in sizes:
                        heading(f"wave of {size}, run {n} of {RUNS}")
                        try:
                            record = wave(size, n, sampler)
                            results["waves"].append(record)
                            say(f"x{size}: create p50 {record['create']['p50']} ms p95 {record['create']['p95']}; "
                                f"every pod gone {record['pods_gone_s']} s after the deletes")
                            if record["failed"]:
                                failures.add(f"wave x{size} run {n}: {record['failed']} create(s) failed")
                        except Exception as error:  # noqa: BLE001
                            failures.add(f"wave x{size} run {n}: {type(error).__name__}: {str(error)[:200]}")
            idle_window = (idle.start, idle.end)
            results["phases"] = sampler.phase_list()
    finally:
        delete_job()
        delete_run(hold_run)

    rows = read_csv(RESULTS / "control_plane.csv")
    crows = read_csv(RESULTS / "control_plane-containers.csv")
    results["idle"] = usage(rows, *idle_window)
    for record in results["ramp"]:
        record["pods"] = usage(rows, record["start"], record["end"], crows, (record["start_unix"], record["end_unix"]))
    for record in results["waves"]:
        # The waves last seconds: their CPU is the kubelet's rate (mean, peak); the counter average
        # needs readings 10 s apart, so it is not computed for them.
        record["pods_create"] = usage(rows, *record["create_window"])
        record["pods_delete"] = usage(rows, *record["delete_window"])
    results["ramp_by_level"] = {}
    for level in levels:
        mine, flagged = counted([r for r in results["ramp"] if r["level"] == level])
        results["ramp_by_level"][str(level)] = {
            "rps": spread([r["rps"] for r in mine]),
            "p99_ms": spread([r["latency"]["p99"] for r in mine]),
            "server_cpu_peak_m": spread([r["pods"]["server"]["cpu_peak_m"] for r in mine]),
            "server_cpu_avg_m": spread([r["pods"]["server"].get("cpu_avg_m") for r in mine]),
            "server_cpu_avg_unknown_runs": sum(1 for r in mine if r["pods"]["server"].get("cpu_avg_m") is None),
            "failed_calls": sum(r["failed"] for r in results["ramp"] if r["level"] == level),
            "runs_counted": [r["run"] for r in mine], "median_includes_failed_runs": flagged}
    save("control_plane", results)

    heading("control plane")
    i = results["idle"]
    print(f"  idle: server {i['server']['cpu_mean_m']}m / {i['server']['mem_peak_mib']} MiB, "
          f"controller {i['controller']['cpu_mean_m']}m / {i['controller']['mem_peak_mib']} MiB")
    for level, s in results["ramp_by_level"].items():
        print(f"  {level:>3} API clients: {s['rps']['median']} req/s (spread {s['rps']['spread']}), "
              f"p99 {s['p99_ms']['median']} ms, server CPU "
              f"{str(s['server_cpu_avg_m']['median']) + 'm' if s['server_cpu_avg_m']['median'] is not None else 'unknown'}"
              f"{' (unknown in ' + str(s['server_cpu_avg_unknown_runs']) + ' run(s))' if s['server_cpu_avg_unknown_runs'] else ''}"
              " on average over the level "
              f"(peak {s['server_cpu_peak_m']['median']}m; limit 1000m)"
              + (f"; {s['failed_calls']} failed call(s)" if s["failed_calls"] else "")
              + ("  * every run had failures: median includes them" if s["median_includes_failed_runs"] else ""))
    for size in sizes:
        mine = [w for w in results["waves"] if w["size"] == size]
        print(f"  wave of {size}: create p50 {spread([w['create']['p50'] for w in mine])['median']} ms; controller CPU peak "
              f"{spread([w['pods_create']['controller']['cpu_peak_m'] for w in mine])['median']}m; "
              f"server CPU peak {spread([w['pods_create']['server']['cpu_peak_m'] for w in mine])['median']}m")
    print(f"  {show('(full API load output: logs/api_load.log)')}")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
