"""The egress overhead ladder: the same HTTP load through each layer of egress control, one more
layer per rung, and what each sidecar costs in memory.

Rungs 1 to 5 call an echo server in the cluster (plain HTTP on port 80:
echo-stress.opensandbox.svc.cluster.local) from a sandbox with the load generator loadgen/http_load.py.
The echo server is manifests/echo-stress.yaml: part 4's echo program and image, in 3 replicas of
500m (limit 1 CPU each), so the egress path, not the echo server, is under test. The script applies
it first and deletes it at the end (also on failure); part 4's own echo server is not used. The
rungs:
  1 none     no network policy: no sidecar at all
  2 dns      a policy (deny, allow the echo host) with the server in [egress] mode "dns": the
             sidecar's DNS proxy filters names, nothing filters packets. The server is switched
             with server_config.py --egress-mode dns and switched back right after
  3 dns+nft  the same policy in mode "dns+nft" (the series' setting): the resolved addresses also
             go into nftables, so a connection to an address no allowed name resolved to is dropped
  4 mitm     rung 3 plus credentialProxy enabled: the transparent proxy (mitmproxy) in the sidecar
             takes every connection to ports 80 and 443. The echo server is plain HTTP, so this
             rung pays the proxy's own cost, not a TLS handshake per request
  5 vault    rung 4 plus a Credential Vault binding for the echo host (schemes ["http"], an apiKey
             header with a dummy value): the proxy injects the header into every request. The
             echo server's first reply shows the header arrived (its value is never printed)
Rung 6 makes a real AWS call instead, because part 5's SigV4 addon kills any host that is not AWS:
  6 sigv4    ../opensandbox-aws-credentials/build_egress.sh points the server at the egress-sigv4
             image (it does not rebuild an image already in ECR); part 5's broker (port-forward
             127.0.0.1:18090) creates the sandbox with its policy and writes short-lived
             credentials into the vault; loadgen/sts_load.py calls STS GetCallerIdentity with the
             placeholder keys and the addon re-signs each call. Next to it, "aws-floor": the same
             calls unsigned from a sandbox with no policy (AWS answers 403 at once), the network and
             AWS share of the time. This rung includes AWS latency; the floor shows how much.
             The server goes back to the base egress image afterwards.

Each rung runs RUNS times, each time in a fresh sandbox (2 CPU, 2 GiB): 50 warm-up requests, 500
one at a time (latency, with the name lookup timed on its own), then 16 clients for 30 s spread over
4 processes (requests per second and latency under load). Every request is a new connection (a
lookup, a connect, a GET, a close), the way a curl per request is. Failed requests are counted,
never timed; a run with any is marked and left out of the medians. A run where the load generator's
CPU over the load phase reached 1600m (80% of its 2 CPUs) is flagged "client-bound". The sampler
records every container every 2 s: the sidecar's RSS idle and at its peak, and the echo server's
CPU of every echo replica. Each replica is one Python process, which runs Python on one core at a
time; every request is a new connection, so the Service spreads them over the three. A rung where
any replica's CPU reached 800m (80% of its 1-CPU limit) is flagged "target-bound", its requests per
second being the echo server's ceiling rather than the path's; each replica's peak is recorded.

Published reference (components/egress/docs/benchmark.md in the 1.2.0 release, "illustrative
only"): dns+nft plus MITM against dns+nft, short HTTPS requests to public sites, about -48% req/s,
+92% mean latency, +595% P99, about 5x the sidecar's RSS. That test is HTTPS to the internet; ours is
HTTP inside the cluster, so the numbers are printed side by side, not as a reproduction.

--resolve-once (a variant; rungs dns, dns+nft, mitm and vault by default): the load generator
looks the echo host up once at the start (timed and recorded) and opens every connection to that
cached address, so each request is a connect, the GET and the close with no lookup. In dns+nft mode
a lookup through the sidecar costs about 60 ms, which otherwise dominates every rung that has it;
the variant isolates the connection path (nftables, the transparent proxy). If the address stops
being reachable partway (nftables may let an allowed address expire), requests fail: they are
counted with their kind and the seconds since the lookup, the first such time is reported per run,
and the run is left out of the medians like any run with a failure. The variant writes
results/ladder-resolve-once.json, .csv and -containers.csv; results/ladder.json is not touched.

The server always ends on runc, the base egress image and dns+nft (checked with --show --json).
Writes results/ladder.json, results/ladder.csv and results/ladder-containers.csv.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager

from opensandbox import SandboxSync

from checks import counted, egress_rebuilt
from common import (
    AWS_CREDENTIALS_DIR,
    ECHO_HOST,
    ECHO_SELECTOR,
    ECR_PREFIX,
    ECR_REGISTRY,
    RESULTS,
    RUNS,
    SANDBOX_NAMESPACE,
    Failures,
    Plan,
    Sampler,
    any_bound,
    bound,
    cli,
    connection,
    create_sandbox,
    delete_run,
    deny_policy,
    describe,
    echo_down,
    echo_up,
    ensure_port_forward,
    heading,
    kube_json,
    kubectl,
    last_json,
    load_module,
    log,
    mark_server_touched,
    new_run_id,
    own_metadata,
    peaks_by_pod,
    preconditions,
    put_loadgen,
    read_csv,
    restore_failed,
    run_child,
    run_in,
    save,
    say,
    server_config,
    server_session,
    server_show,
    show,
    spread,
    window_cpu,
)

SCRIPT = "05_egress_ladder"
RUNGS = ("none", "dns", "dns+nft", "mitm", "vault", "sigv4")
RESOLVE_ONCE_RUNGS = ("none", "dns+nft", "mitm", "vault")   # dns: its lookup is 0.3 ms already
CACHED = "/cached"                 # the variant's label: "dns+nft/cached"; "@8" marks the 8-client pass
# --resolve-once: a request to an address that stopped answering fails, maybe by timing out; these
# keep a run bounded (normal requests take milliseconds). 4 s, not 3: Linux retransmits a SYN after
# 1 s and 3 s, and a connection that needed two retransmits (a busy echo backlog) must still count.
RESOLVE_ONCE_TIMEOUT_S = 4
RESOLVE_ONCE_MAX_IN_A_ROW = 20
PHASE2_TIMEOUT_S = 10              # http_load.py's default --timeout, which the main ladder uses
REDUCED_CONCURRENCY = 8            # the pass after a target-bound cached rung
# The egress sidecar has no CPU limit (the server sets no [egress] requests or limits, and the sandbox
# namespace has no LimitRange), and the transparent proxy is one mitmdump process (exec'd once by the
# sidecar): Python code in one process runs on one core at a time, so about 1000m is its ceiling.
PROXY_CEILING_M = 1000
PROXY_BOUND = 0.8                  # the sidecar's CPU over the load at 80% of that or more: the proxy is the cap
PROXY_NOTE = ("The egress sidecar has no CPU limit (the server sets no [egress] requests or limits; no LimitRange "
              "in the sandbox namespace). The transparent proxy is one mitmdump process, so about one core "
              f"({PROXY_CEILING_M}m) is its ceiling; a run where the sidecar's CPU over the load phase reached "
              f"{int(PROXY_BOUND * PROXY_CEILING_M)}m is proxy-bound. The sidecar's CPU over the load comes from its "
              "own cumulative counter (sidecar_load_avg_cpu_m; unknown when the kubelet stamped fewer than two "
              "readings 10 s apart inside the load), else from its peak rate (sidecar_load_peak_cpu_m).")
SHAPE = {"cpu": "2", "memory": "2Gi"}
ECHO_REPLICAS = 3                 # manifests/echo-stress.yaml
ECHO_CPU_LIMIT_M = 1000           # per replica
# Each replica is one CPython process: its Python code runs on one core at a time, so a replica tops
# out near 1000m whatever its limit. The flag fires when any replica reaches 80% of that.
ECHO_CEILING_M = min(ECHO_CPU_LIMIT_M, 1000)
TARGET_BOUND = 0.8                # echo CPU at 80% of its ceiling or more: the target, not the path, is the cap
CLIENT_LIMIT_M = 2000             # the load generator's sandbox (SHAPE)
CLIENT_BOUND = 0.8                # its CPU over the load phase at 80% of that or more: the client is the cap
STAMP_LAG_S = 12                  # the kubelet stamps container counters about every 10 s
INJECTED_HEADER = "X-Stress-Key"
PUBLISHED = {"source": "components/egress/docs/benchmark.md (release 1.2.0), illustrative only",
             "compare": "dns+nft + transparent MITM vs dns+nft, short HTTPS requests to public sites",
             "rps_pct": -47.6, "mean_pct": 91.9, "p99_pct": 595.2, "rss_ratio": 5}


def arguments(parser) -> None:
    parser.add_argument("--rungs", default=None,
                        help=f"comma-separated, of {', '.join(RUNGS)} (default: all; with --resolve-once: "
                             f"{', '.join(RESOLVE_ONCE_RUNGS)})")
    parser.add_argument("--resummarize", action="store_true",
                        help="recompute the summary (and each run's sidecar CPU) of results/ladder.json, or with "
                             "--resolve-once results/ladder-resolve-once.json, from its records and CSV; no cluster calls")
    parser.add_argument("--resolve-once", action="store_true",
                        help="the variant: one lookup per run, then every connection to the cached address; "
                             "writes results/ladder-resolve-once.*")
    parser.add_argument("--sequential", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--duration", type=float, default=30.0,
                        help="seconds of load (default 30: the CPU flags need counter readings 10 s apart inside it)")
    parser.add_argument("--sts-calls", type=int, default=100, help="sequential STS calls per sigv4 run (default 100)")


def local_tool_problems() -> list[str]:
    """What build_egress.sh needs on this machine even when it builds nothing: docker with buildx
    (it checks for it first) and uv (it runs the addon's unit tests)."""
    problems = []
    if shutil.which("docker") is None:
        problems.append("docker is not installed")
    elif subprocess.run(["docker", "buildx", "version"], capture_output=True, timeout=60).returncode != 0:
        problems.append("docker buildx is not available")
    if shutil.which("uv") is None:
        problems.append("uv is not installed")
    return problems


@contextmanager
def echo_server(wanted: bool):
    """The ladder's own echo server for the body of the with (manifests/echo-stress.yaml), deleted after."""
    if not wanted:
        yield
        return
    heading("echo-stress: the echo server for rungs 1 to 5")
    try:
        echo_up()
        say(f"echo-stress ready at {ECHO_HOST}: {len(echo_pods())} replicas (500m, limit {ECHO_CPU_LIMIT_M}m each)")
        yield
    finally:
        echo_down()
        say("echo-stress deleted")


def aws_tools_image() -> str:
    """Part 5's workload image (part 4's egress-tools plus boto3)."""
    return f"{ECR_REGISTRY}/{ECR_PREFIX}/aws-tools:1"


def pod_of(sandbox_id: str) -> str:
    name = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"opensandbox.io/id={sandbox_id}", "-o",
                   "jsonpath={.items[0].metadata.name}", check=False).strip()
    return name or f"{sandbox_id}-0"


def echo_pods() -> list[str]:
    """The echo replicas' pod names (running, not terminating)."""
    pods = kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", ECHO_SELECTOR, check=False)
    return sorted(p["metadata"]["name"] for p in pods.get("items", [])
                  if not p["metadata"].get("deletionTimestamp") and p.get("status", {}).get("phase") == "Running")


def usage(pod: str, start: float, end: float, name: str = "ladder") -> dict:
    """From results/<name>-containers.csv: each container of `pod`, idle (the last sample before
    `start`) and at its peak between start and end: {container: {idle_rss_mib, peak_rss_mib, peak_cpu_m}}."""
    rows = [r for r in read_csv(RESULTS / f"{name}-containers.csv") if r["pod"] == pod]
    out: dict[str, dict] = {}
    for name in sorted({r["container"] for r in rows}):
        mine = [r for r in rows if r["container"] == name]
        before = [r for r in mine if r["t"] <= start and r["rss_mib"] is not None]
        during = [r for r in mine if start <= r["t"] <= end]
        out[name] = {"idle_rss_mib": before[-1]["rss_mib"] if before else None,
                     "peak_rss_mib": max((r["rss_mib"] for r in during if r["rss_mib"] is not None), default=None),
                     "peak_cpu_m": max((r["cpu_m"] for r in during if r["cpu_m"] is not None), default=None)}
    return out


def failure_pattern(first_s: float | None, last_ok_s: float | None) -> str | None:
    """None: no request failed. "expired": none succeeded after the first failure (with one lookup
    and nothing to renew it, what an allowed address that lapsed looks like). "isolated": requests
    succeeded after it (slow connections, a busy backlog), not a lapse."""
    if first_s is None:
        return None
    return "expired" if last_ok_s is None or last_ok_s <= first_s else "isolated"


def split_label(label: str) -> tuple[str, str]:
    """("dns+nft", "/cached@8") for "dns+nft/cached@8"; (rung, "") for a plain rung."""
    for mark in (CACHED, "@"):
        if mark in label:
            at = label.index(mark)
            return label[:at], label[at:]
    return label, ""


def phase2_connect_ms(ladder: dict | None) -> dict[str, float]:
    """From the main ladder's results (results/ladder.json): per rung, the median over runs of the mean
    connect + GET + close time in the one-at-a-time phase (the request's mean minus the lookup's: means
    add up, so this is exact but for the stored lookup mean's 0.1 ms rounding)."""
    out: dict[str, list[float]] = {}
    for r in (ladder or {}).get("records", []):
        times = r.get("sequential_ms") or []
        seq = statistics.fmean(times) if times else (r.get("sequential") or {}).get("mean")
        look = (r.get("lookup") or {}).get("mean")
        if seq is not None and look is not None and r.get("rung") not in ("sigv4", "aws-floor"):
            out.setdefault(r["rung"], []).append(seq - look)
    return {rung: round(statistics.median(v), 2) for rung, v in out.items()}


def reduced_pass(records: list[dict], rungs: list[str]) -> list[str]:
    """The cached rungs to run again with REDUCED_CONCURRENCY clients: each target-bound rung with
    the rung it is compared with (dns+nft; when dns+nft itself is target-bound, every rung compared
    with it). In the order of `rungs`."""
    flagged = {r["base_rung"] for r in records if r.get("resolve_once") and r.get("target_bound")}
    wanted: set[str] = set()
    for rung in flagged:
        wanted |= set(rungs) if rung == "dns+nft" else {rung, "dns+nft"}
    return [r for r in rungs if r in wanted]


def proxy_cpu(rows: list[dict], pod: str, load_unix: tuple[float, float], window: tuple[float, float]) -> dict:
    """The egress sidecar's CPU during one run's load phase, from results/<name>-containers.csv rows:
    the average from its cumulative counter over the load (window_cpu, by the kubelet's stamps; None
    when unknown), the peak rate the sampler read during the run (window: sampler clock), and
    proxy_bound: the average (or, unknown, the peak) against PROXY_BOUND of PROXY_CEILING_M (None
    without a sidecar or any reading)."""
    names = sorted({r["container"] for r in rows if r["pod"] == pod and "egress" in r["container"]})
    if not names:
        return {"sidecar_load_avg_cpu_m": None, "sidecar_load_peak_cpu_m": None, "proxy_bound": None}
    avg = window_cpu(rows, pod, names[0], load_unix[0], load_unix[1])
    peak = max((r["cpu_m"] for r in rows if r["pod"] == pod and r["container"] == names[0]
                and window[0] <= r["t"] <= window[1] and r["cpu_m"] is not None), default=None)
    return {"sidecar_load_avg_cpu_m": avg, "sidecar_load_peak_cpu_m": peak,
            "proxy_bound": bound(avg if avg is not None else peak, PROXY_BOUND * PROXY_CEILING_M)}


def run_pod(rows: list[dict], window: tuple[float, float]) -> str | None:
    """The one sandbox pod (a "sandbox" container) the sampler saw during a run's phase window."""
    pods = sorted({r["pod"] for r in rows if r["container"] == "sandbox" and window[0] <= r["t"] <= window[1]})
    return pods[0] if len(pods) == 1 else None


def pct_change(new: float | None, base: float | None) -> float | None:
    return round(100 * (new - base) / base, 1) if new is not None and base else None


def http_rung(rung: str, n: int, args, sampler: Sampler, concurrency: int | None = None) -> dict:
    """One run of an echo rung. With --resolve-once the record is labelled "<rung>/cached" (and
    "@<clients>" when `concurrency` overrides --concurrency)."""
    concurrency = concurrency or args.concurrency
    label = rung + (CACHED if args.resolve_once else "") + (f"@{concurrency}" if concurrency != args.concurrency else "")
    run_id = new_run_id()
    meta = {**own_metadata(SCRIPT, run_id), "rung": rung.replace("+", "-")}
    policy = None if rung == "none" else deny_policy(ECHO_HOST)
    proxy = rung in ("mitm", "vault")
    sb = None
    try:
        sb = create_sandbox(meta, resource=SHAPE, policy=policy, proxy=proxy, ttl_minutes=20)
        if rung == "vault":
            sb.credential_vault.create(
                credentials=[{"name": "stress-key", "source": {"type": "inline", "value": f"dummy-{run_id}"}}],
                bindings=[{"name": "echo-key", "match": {"hosts": [ECHO_HOST], "schemes": ["http"]},
                           "auth": {"type": "apiKey", "name": INJECTED_HEADER, "credential": "stress-key"}}])
        script = put_loadgen(sb, "http_load.py")
        pod = pod_of(sb.id)
        command = (f"python3 {script} --host {ECHO_HOST} --sequential {args.sequential} --concurrency "
                   f"{concurrency} --duration {args.duration}"
                   + (f" --expect-header {INJECTED_HEADER}" if rung == "vault" else "")
                   + (f" --resolve-once --timeout {RESOLVE_ONCE_TIMEOUT_S} --max-consecutive-failures "
                      f"{RESOLVE_ONCE_MAX_IN_A_ROW}" if args.resolve_once else ""))
        time.sleep(sampler.every + 1)       # an idle sample of the new sidecar first
        start = sampler.elapsed()
        with sampler.phase(f"{label} {n}", rung=label, run=n):
            out = run_in(sb, command, timeout=int(120 + args.sequential * 0.5 + args.duration * 2))
        end = sampler.elapsed()
        # The kubelet stamps a container's counter at its own housekeeping (about every 10 s): keep
        # sampling until a reading stamped near the end of the load has been read.
        time.sleep(STAMP_LAG_S)
        if out.rc != 0:
            raise RuntimeError(f"the load generator exited {out.rc}: {show(out.text[-200:])}")
        result = last_json(out.text)
        log("ladder", f"{rung} {n}: statuses {result['statuses']}")
        containers = usage(pod, start, end, args.name)
        sidecar = next((v for k, v in containers.items() if "egress" in k), None)
        # CPU over the load phase only, from the cumulative counters, for the two "who was the cap"
        # flags: an echo replica, or the client itself. The window is the load phase as the load
        # generator timed it (Unix time; the sandbox shares the node's clock, as the kubelet's stamps
        # do), and readings are picked by their own stamps. Unknown (None) is printed and counted as
        # unknown, never as "not bound".
        load = result["load"]
        ls, le = load["start_unix"], load["end_unix"]
        crow = read_csv(RESULTS / f"{args.name}-containers.csv")
        replicas = {p: {"avg_cpu_m": window_cpu(crow, p, "echo", ls, le), "peak_cpu_m": peak}
                    for p, peak in peaks_by_pod(read_csv(RESULTS / f"{args.name}.csv"), echo_pods(), start, end).items()}
        echo_busiest = max((v["avg_cpu_m"] for v in replicas.values() if v["avg_cpu_m"] is not None), default=None)
        main = next((k for k in containers if "egress" not in k), None)
        client_cpu = window_cpu(crow, pod, main, ls, le) if main else None
        errors = {"sequential": result["sequential"]["errors"], "load": load["errors"]}
        once = result.get("resolve_once_ms") if result.get("resolve_once") else None
        why = result.get("failures") or {}
        return {
            "rung": label, "base_rung": rung, "run": n, "statuses": result["statuses"],
            "header_seen": result.get("header_seen"),
            "errors": errors, "failed": errors["sequential"] + errors["load"],
            "sequential": describe("sequential", result["sequential"]["ms"]),
            "sequential_ms": result["sequential"]["ms"],
            "lookup": describe("lookup", [once] if once is not None else result["sequential"]["dns_ms"]),
            "resolve_once": once is not None, "timeout_s": result.get("timeout_s"),
            "failure_kinds": why.get("kinds", {}), "failures_counted": why.get("count", 0),
            "first_failure_s": why.get("first_s"), "last_ok_s": why.get("last_ok_s"), "failure_clock": why.get("since"),
            "failure_pattern": failure_pattern(why.get("first_s"), why.get("last_ok_s")),
            "failure_times_s": why.get("times_s", [])[:200], "stopped_early": result.get("stopped_early", []),
            "load": {**describe("load", load["ms"]), "rps": load["rps"], "errors": load["errors"],
                     "concurrency": load["concurrency"], "processes": load.get("processes")},
            "sidecar": sidecar, "containers": containers,
            "echo_replicas": replicas, "echo_busiest_avg_cpu_m": echo_busiest,
            "target_bound": any_bound([v["avg_cpu_m"] for v in replicas.values()], TARGET_BOUND * ECHO_CEILING_M),
            "client_avg_cpu_m": client_cpu,
            "client_bound": bound(client_cpu, CLIENT_BOUND * CLIENT_LIMIT_M),
            "pod": pod, **proxy_cpu(crow, pod, (ls, le), (start, end)),
            "load_window_unix": [ls, le],
        }
    finally:
        if sb is not None:
            sb.close()
        delete_run(run_id)


def sts_rung(kind: str, n: int, args, sampler: Sampler, awscred) -> dict:
    """kind "sigv4": a broker-made sandbox, signed calls through the addon; "aws-floor": a sandbox
    with no policy, unsigned calls."""
    run_id = new_run_id()
    sandbox_id = None
    sb = None
    try:
        if kind == "sigv4":
            view = awscred.broker_create(script="stress-05_egress_ladder", run_id=run_id, timeout_minutes=20)
            sandbox_id = view["sandbox_id"]
            sb = SandboxSync.connect(sandbox_id, connection_config=connection("proxy"))
        else:
            meta = {**own_metadata(SCRIPT, run_id), "rung": "aws-floor"}
            sb = create_sandbox(meta, image_ref=aws_tools_image(), resource=SHAPE, ttl_minutes=20)
            sandbox_id = sb.id
        script = put_loadgen(sb, "sts_load.py")
        pod = pod_of(sandbox_id)
        time.sleep(sampler.every + 1)
        start = sampler.elapsed()
        mode = "signed" if kind == "sigv4" else "unsigned"
        with sampler.phase(f"{kind} {n}", rung=kind, run=n):
            out = run_in(sb, f"python3 {script} --mode {mode} --sequential {args.sts_calls}", timeout=600)
        end = sampler.elapsed()
        time.sleep(sampler.every + 1)
        if out.rc != 0:
            raise RuntimeError(f"sts_load.py exited {out.rc}: {show(out.text[-200:])}")
        result = last_json(out.text)
        containers = usage(pod, start, end, args.name)
        errors = {"sequential": result["sequential"].get("failed", 0), "load": result["load"].get("failed", 0)}
        return {"rung": kind, "run": n, "outcomes": result["outcomes"], "errors": errors,
                "failed": errors["sequential"] + errors["load"],
                "sequential": describe("sequential", result["sequential"]["ms"]),
                "sequential_ms": result["sequential"]["ms"],
                "load": {**describe("load", result["load"]["ms"]), "rps": result["load"]["rps"],
                         "concurrency": result["load"]["concurrency"]},
                "sidecar": next((v for k, v in containers.items() if "egress" in k), None), "containers": containers}
    finally:
        if sb is not None:
            sb.close()
        if kind == "sigv4" and sandbox_id:
            try:
                awscred.broker_delete(sandbox_id)
            except Exception as error:  # noqa: BLE001 - reported, 99_cleanup.py sweeps
                print(f"  broker delete of the sigv4 sandbox failed: {show(str(error))[:160]}", file=sys.stderr)
        else:
            delete_run(run_id)


def switch_to_sigv4() -> tuple[str, bool | None]:
    """Run part 5's build_egress.sh (it builds nothing when the image is in ECR, and switches the server
    to it by digest), then read the server's egress image back. Returns it (registry masked)."""
    script = AWS_CREDENTIALS_DIR / "build_egress.sh"
    say("../opensandbox-aws-credentials/build_egress.sh (no rebuild when the image is already in ECR)")
    mark_server_touched()
    done = run_child([str(script)], timeout=1800, cwd=str(AWS_CREDENTIALS_DIR))
    log("build_egress", done.stdout + done.stderr)
    for line in done.stdout.splitlines():
        if line.strip().startswith(("ok", "FAIL", "egress image")):
            print(f"      {show(line.strip())[:170]}")
    ensure_port_forward()
    if done.returncode != 0:
        raise RuntimeError(f"build_egress.sh failed (exit {done.returncode}): see logs/build_egress.log")
    rebuilt = egress_rebuilt(done.stdout)
    if rebuilt:
        print("  WARN  build_egress.sh REBUILT and pushed the egress-sigv4 image (part 5's egress/ files or the base "
              "image changed since part 5 built it): this rung measures that new build", flush=True)
    image = server_show()["egress_image"]
    if not str(image).startswith(f"{ECR_PREFIX}/egress-sigv4@sha256:"):
        raise RuntimeError(f"after build_egress.sh the server runs egress image {image!r}, not egress-sigv4")
    return image, rebuilt



def summarize(records: list[dict]) -> dict:
    """Per rung: the pooled sequential latencies, and the spread over runs of the load numbers."""
    by_rung: dict[str, list[dict]] = {}
    for r in records:
        by_rung.setdefault(r["rung"], []).append(r)
    out = {}
    for rung, every in by_rung.items():
        runs, flagged = counted(every)          # runs with a failed request are left out (or flagged)
        seq = [v for r in runs for v in r.get("sequential_ms", [])]
        out[rung] = {
            "runs": len(runs), "runs_left_out": [r["run"] for r in every if r not in runs],
            "median_includes_failed_runs": flagged,
            "failed_requests": sum(r.get("failed", 0) for r in every),
            "sequential": describe(f"{rung} sequential", seq),
            "sequential_p50_ms": spread([r["sequential"]["p50"] for r in runs]),
            "load_rps": spread([r["load"]["rps"] for r in runs]),
            "load_mean_ms": spread([r["load"]["mean"] for r in runs]),
            "load_p50_ms": spread([r["load"]["p50"] for r in runs]),
            "load_p99_ms": spread([r["load"]["p99"] for r in runs]),
            "sidecar_idle_rss_mib": spread([(r.get("sidecar") or {}).get("idle_rss_mib") for r in runs]),
            "sidecar_peak_rss_mib": spread([(r.get("sidecar") or {}).get("peak_rss_mib") for r in runs]),
            "sidecar_peak_cpu_m": spread([(r.get("sidecar") or {}).get("peak_cpu_m") for r in runs]),
            "target_bound_runs": sum(1 for r in runs if r.get("target_bound")),
            "proxy_bound_runs": sum(1 for r in runs if r.get("proxy_bound")),
            "sidecar_load_avg_cpu_m": spread([r.get("sidecar_load_avg_cpu_m") for r in runs]),
            "sidecar_load_peak_cpu_m": spread([r.get("sidecar_load_peak_cpu_m") for r in runs]),
            "client_bound_runs": sum(1 for r in runs if r.get("client_bound")),
            "cap_unknown_runs": sum(1 for r in runs if rung not in ("sigv4", "aws-floor")
                                    and None in (r.get("target_bound"), r.get("client_bound"))),
        }
        if any(r.get("resolve_once") for r in every):
            # The variant: the one lookup per run, and when (seconds after it) requests to the cached
            # address first failed in each run (None: none failed), and whether any succeeded after
            # that (failure_pattern). Every run counts here.
            out[rung]["lookup_once_ms"] = spread([r["lookup"]["p50"] for r in every])
            out[rung]["sequential_mean_ms"] = spread([r["sequential"]["mean"] for r in runs])
            out[rung]["first_failure_s_by_run"] = [r.get("first_failure_s") for r in every]
            out[rung]["failure_pattern_by_run"] = [r.get("failure_pattern") for r in every]
            kinds: dict[str, int] = {}
            for r in every:
                for kind, n in (r.get("failure_kinds") or {}).items():
                    kinds[kind] = kinds.get(kind, 0) + n
            out[rung]["failure_kinds"] = kinds
    for rung, s in out.items():
        name, mark = split_label(rung)    # "/cached" rungs are compared with "/cached" bases, "@8" with "@8"
        for base in ("none", "dns+nft"):
            b = out.get(base + mark)
            if not b or name == base:
                continue
            s[f"vs_{base}"] = {
                "rps_pct": pct_change(s["load_rps"]["median"], b["load_rps"]["median"]),
                "mean_pct": pct_change(s["load_mean_ms"]["median"], b["load_mean_ms"]["median"]),
                "p99_pct": pct_change(s["load_p99_ms"]["median"], b["load_p99_ms"]["median"]),
                "sequential_p50_pct": pct_change(s["sequential_p50_ms"]["median"], b["sequential_p50_ms"]["median"]),
            }
        if name == "mitm" and "dns+nft" + mark in out:
            m, d = s["sidecar_peak_rss_mib"]["median"], out["dns+nft" + mark]["sidecar_peak_rss_mib"]["median"]
            s["rss_ratio_vs_dns+nft"] = round(m / d, 1) if m and d else None
    return out



def report(results: dict) -> None:
    """The ladder's table (and the variant's notes), from results alone: a run and --resummarize print it."""
    heading("the ladder (medians over the runs)")
    order = ("none", "dns", "dns+nft", "mitm", "vault", "aws-floor", "sigv4")
    labels = sorted(results["summary"], key=lambda k: (split_label(k)[1], order.index(split_label(k)[0])))
    print(f"  {'rung':<18} {'1-at-a-time p50':>16} {'p99':>8} {'req/s':>8} {'load p99':>9} {'sidecar RSS':>12} "
          f"{'vs dns+nft':>22}")
    for rung in labels:
        s = results["summary"][rung]
        vs = s.get("vs_dns+nft") or {}
        rel = f"{vs.get('rps_pct')}% req/s, {vs.get('p99_pct')}% p99" if vs else "-"
        marks = (" *" if s["target_bound_runs"] else "") + (" c" if s.get("client_bound_runs") else "") + \
                (" p" if s.get("proxy_bound_runs") else "") + \
                (" ?" if s.get("cap_unknown_runs") else "") + \
                (f" (failed runs left out: {s['runs_left_out']})" if s.get("runs_left_out") else "")
        print(f"  {rung:<18} {str(s['sequential']['p50']):>16} {str(s['sequential']['p99']):>8} "
              f"{str(s['load_rps']['median']):>8} {str(s['load_p99_ms']['median']):>9} "
              f"{str(s['sidecar_peak_rss_mib']['median']):>12} {rel:>22}{marks}")
    print(f"  * an echo replica (one Python process, limit {ECHO_CPU_LIMIT_M}m) reached {int(TARGET_BOUND * ECHO_CEILING_M)}m: "
          "that rung's req/s is the echo server's ceiling, not the path's (per-replica numbers in results/ladder.json)")
    print(f"  c the load generator's CPU reached {int(CLIENT_BOUND * CLIENT_LIMIT_M)}m: the client, not the path, was the cap")
    print(f"  p the egress sidecar's CPU over the load reached {int(PROXY_BOUND * PROXY_CEILING_M)}m: the proxy (one "
          "mitmdump process, no CPU limit) was the cap")
    print("  ? in a run of that rung the CPU over the load phase is unknown (counter readings under 10 s apart): "
          "whether the echo server or the client was the cap is not known, not ruled out")
    if results.get("sigv4_rebuilt"):
        print("  the egress-sigv4 image was REBUILT by build_egress.sh for this run (see above)")
    print(f"  published ({PUBLISHED['compare']}): {PUBLISHED['rps_pct']}% req/s, +{PUBLISHED['mean_pct']}% mean, "
          f"+{PUBLISHED['p99_pct']}% p99, ~{PUBLISHED['rss_ratio']}x RSS")
    print("  the sigv4 rung includes AWS's own latency; aws-floor is the unsigned call without a sidecar")
    if results.get("resolve_once"):
        print("  --resolve-once: one lookup per run, then every request to the cached address (no lookup in the "
              f"times); request timeout {RESOLVE_ONCE_TIMEOUT_S} s (the main ladder: {PHASE2_TIMEOUT_S} s)")
        before = results.get("phase2_sequential_connect_ms") or {}
        for rung in [k for k in labels if k.endswith(CACHED) or "@" in k]:
            s = results["summary"][rung]
            name = split_label(rung)[0]
            print(f"  {rung:<18} lookup {s.get('lookup_once_ms', {}).get('median')} ms; one at a time mean "
                  f"{s.get('sequential_mean_ms', {}).get('median')} ms (main ladder, connect+GET+close: "
                  f"{before.get(name, '-')} ms); first failure per run (s after the lookup) "
                  f"{s.get('first_failure_s_by_run')} {s.get('failure_pattern_by_run')}; kinds {s.get('failure_kinds') or '-'}")
        if results.get("proxy_note"):
            print(f"  {results['proxy_note']}")
        for rung in [k for k in labels if results["summary"][k].get("sidecar_load_peak_cpu_m", {}).get("median")]:
            s = results["summary"][rung]
            print(f"  {rung:<18} sidecar CPU over the load: mean {s['sidecar_load_avg_cpu_m']['runs']} m, peak "
                  f"{s['sidecar_load_peak_cpu_m']['runs']} m; proxy-bound runs {s['proxy_bound_runs']} of {s['runs']}")
        if results.get("reduced_concurrency_pass"):
            print(f"  * at {results['concurrency']} clients a cached rung was target-bound: its ratio there is a bound; the "
                  f"@{REDUCED_CONCURRENCY} rows are the pair again with {REDUCED_CONCURRENCY} clients (one run each)")


def resummarize(name: str, dry_run: bool) -> int:
    """--resummarize: the sidecar's CPU per run (proxy_cpu) and the summary again, from
    results/<name>.json and results/<name>-containers.csv; no cluster calls. --dry-run writes nothing."""
    path = RESULTS / f"{name}.json"
    if not path.is_file():
        sys.exit(f"results/{name}.json is not there: nothing to resummarize")
    results = json.loads(path.read_text())
    rows = read_csv(RESULTS / f"{name}-containers.csv")
    phases = {p["name"]: p for p in results.get("phases", [])}
    for record in results["records"]:
        phase = phases.get(f"{record['rung']} {record['run']}")
        if not phase or "load_window_unix" not in record:
            continue                      # an STS rung, or a run without a load phase
        window = (phase["start"], phase["end"] + STAMP_LAG_S)
        pod = record.get("pod") or run_pod(rows, (phase["start"], phase["end"]))
        if pod:
            record.update({"pod": pod, **proxy_cpu(rows, pod, tuple(record["load_window_unix"]), window)})
    results["proxy_note"] = PROXY_NOTE
    results["summary"] = summarize(results["records"])
    if dry_run:
        print(f"  --dry-run: results/{name}.json recomputed from its records and CSV; nothing written")
    else:
        save(name, results)
        print(f"  results/{name}.json: the sidecar's CPU and the summary recomputed from its records and CSV")
    report(results)
    return 0

def main() -> int:
    args = cli(__doc__, arguments)
    args.name = "ladder-resolve-once" if args.resolve_once else "ladder"
    if args.resummarize:
        return resummarize(args.name, args.dry_run)
    wanted = args.rungs or ",".join(RESOLVE_ONCE_RUNGS if args.resolve_once else RUNGS)
    rungs = [r.strip() for r in wanted.split(",") if r.strip()]
    unknown = [r for r in rungs if r not in RUNGS]
    if unknown or not rungs:
        sys.exit(f"unknown rung(s) {unknown}: choose from {', '.join(RUNGS)}")
    if args.resolve_once and "sigv4" in rungs:
        sys.exit("--resolve-once is for the echo rungs: the sigv4 rung calls AWS through its own client")
    baseline_rungs = [r for r in rungs if r in ("none", "dns+nft", "mitm", "vault")]
    http_rungs = [r for r in rungs if r != "sigv4"]
    per_run = 0.25 + args.sequential * 0.01 / 60 + (args.duration + STAMP_LAG_S) / 60 + 0.5
    if args.resolve_once:   # no 60 ms lookup per request: about a minute a run, as the none and dns rungs took
        per_run = 0.25 + args.sequential * 0.006 / 60 + (args.duration + STAMP_LAG_S) / 60
    plan = Plan(f"05_egress_ladder.py{' --resolve-once' if args.resolve_once else ''}: rungs {', '.join(rungs)}, "
                f"{RUNS} run(s) each" + (" (one lookup per run, then the cached address)" if args.resolve_once else ""))
    plan.checks.append("no sandboxes and no Pools; server runc + base egress + dns+nft + gateway; 2 nodes Ready")
    if "sigv4" in rungs:
        plan.checks.append("docker with buildx, and uv, on this machine (build_egress.sh needs both even when it "
                           "does not rebuild); part 5's broker reachable (port-forward 127.0.0.1:18090)")
    if http_rungs:
        plan.step(f"apply manifests/echo-stress.yaml (echo-stress: part 4's echo program, {ECHO_REPLICAS} replicas of "
                  "500m, limit 1 CPU each) and wait for them", 0.5)
    if baseline_rungs:
        plan.step(f"{RUNS} round(s) of {', '.join(baseline_rungs)} on the baseline server, one fresh sandbox each",
                  round(RUNS * len(baseline_rungs) * per_run, 1))
    if args.resolve_once:
        plan.step(f"only if a cached rung is target-bound: it and the rung it is compared with once more with "
                  f"{REDUCED_CONCURRENCY} clients", round(len(baseline_rungs) * per_run, 1))
    if "dns" in rungs:
        plan.server_changes.append("server_config.py --egress-mode dns, then --egress-mode dns+nft")
        plan.step("server_config.py --egress-mode dns", 1)
        plan.step(f"{RUNS} run(s) of dns", round(RUNS * per_run, 1))
        plan.step("server_config.py --egress-mode dns+nft", 1)
    if "sigv4" in rungs:
        plan.server_changes.append("build_egress.sh (server egress image -> egress-sigv4@<digest>), then --egress-image base")
        plan.step("build_egress.sh: addon tests, no rebuild, server switched to egress-sigv4 by digest", 2)
        plan.step(f"{RUNS} round(s) of aws-floor (unsigned, no policy) and sigv4 (broker sandbox, signed through "
                  f"the addon), {args.sts_calls} sequential + 4x25 concurrent STS calls each", RUNS * 2)
        plan.step("server_config.py --egress-image base", 1)
    if http_rungs:
        plan.step("delete echo-stress (Deployment, Service, ConfigMap; in a finally)", 0.3)
    plan.restores.append("server_config.py --baseline if anything differs, checked with --show --json")
    plan.writes += [f"results/{args.name}.json", f"results/{args.name}.csv", f"results/{args.name}-containers.csv"]
    plan.print()
    if args.dry_run:
        return 0

    if "sigv4" in rungs:
        missing = local_tool_problems()
        if missing:
            sys.exit("the sigv4 rung needs, on this machine: " + "; ".join(missing))
    preconditions()
    awscred = None
    if "sigv4" in rungs:
        # Part 5's helpers, loaded by path (they load parts 2 and 3 again under the same names; this
        # script keeps using its own port-forward keeper, and uses only the broker client from here).
        awscred = load_module("osb_awscred_common", AWS_CREDENTIALS_DIR / "common.py")
        awscred.ensure_broker_forward()
    failures = Failures()
    records: list[dict] = []
    results: dict = {"published": PUBLISHED, "rungs": rungs, "shape": SHAPE, "sequential": args.sequential,
                     "concurrency": args.concurrency, "duration_s": args.duration, "resolve_once": args.resolve_once}
    if args.resolve_once:
        results["resolve_once_settings"] = {
            "timeout_s": RESOLVE_ONCE_TIMEOUT_S, "max_consecutive_failures": RESOLVE_ONCE_MAX_IN_A_ROW,
            "main_ladder_timeout_s": PHASE2_TIMEOUT_S,
            "timeout_note": f"the main ladder (results/ladder.json) ran http_load.py with its default {PHASE2_TIMEOUT_S} s "
                            f"request timeout, this variant with {RESOLVE_ONCE_TIMEOUT_S} s; the timeout only decides "
                            "which slow connections count as failures (failures are never timed)"}
        main_ladder = RESULTS / "ladder.json"
        results["phase2_sequential_connect_ms"] = phase2_connect_ms(
            json.loads(main_ladder.read_text()) if main_ladder.is_file() else None)

    def attempt(fn, *a) -> None:
        try:
            record = fn(*a)
            records.append(record)
            seq, load = record["sequential"], record["load"]
            flag = ("  TARGET-BOUND" if record.get("target_bound") else "") + \
                   ("  CLIENT-BOUND" if record.get("client_bound") else "") + \
                   ("  CPU UNKNOWN (no usable counter window: not known whether the echo or the client was the cap)"
                    if record["rung"] not in ("sigv4", "aws-floor")
                    and None in (record.get("target_bound"), record.get("client_bound")) else "")
            side = (record.get("sidecar") or {}).get("peak_rss_mib")
            once = (f"; one lookup {record['lookup']['p50']} ms" if record.get("resolve_once") else "")
            first = (f", the first {record['first_failure_s']} s after {record.get('failure_clock')} "
                     f"{record.get('failure_kinds')}" if record.get("first_failure_s") is not None else "")
            stopped = f"; stopped early: {', '.join(record['stopped_early'])}" if record.get("stopped_early") else ""
            if record.get("failure_pattern"):
                stopped += (f"; {record['failures_counted']} failure(s) counted in all, the first {record['first_failure_s']} s "
                            f"after {record.get('failure_clock')}, the last success {record.get('last_ok_s')} s: "
                            f"{record['failure_pattern'].upper()} {record.get('failure_kinds')}")
            say(f"{record['rung']:>9} {record['run']}: one at a time p50 {seq['p50']} ms p99 {seq['p99']}; "
                f"{load['concurrency']} clients {load['rps']} req/s p99 {load['p99']} ms; sidecar peak RSS {side} MiB{flag}"
                + once
                + (f"; FAILED requests {record['errors']}{first} (not timed; run left out of the medians)"
                   if record["failed"] else "") + stopped)
            if record["failed"]:
                failures.add(f"{record['rung']} {record['run']}: {record['failed']} failed request(s) {record['errors']}"
                             + first)
            if record.get("base_rung") == "vault" and not record.get("header_seen"):
                failures.add(f"vault {record['run']}: the echo server did not see the injected {INJECTED_HEADER} header")
                record["failed"] = record["failed"] or 1
        except Exception as error:  # noqa: BLE001 - the next rung still runs
            failures.add(f"{a[0]} run {a[1]}: {type(error).__name__}: {str(error)[:200]}")

    with server_session(), Sampler(args.name, every=2.0, containers=True) as sampler, echo_server(bool(http_rungs)):
        for n in range(1, RUNS + 1):
            for rung in baseline_rungs:
                heading(f"{rung}{CACHED if args.resolve_once else ''}, run {n} of {RUNS}")
                attempt(http_rung, rung, n, args, sampler)
        again = reduced_pass(records, baseline_rungs) if args.resolve_once else []
        if again:
            # A cached rung reached the echo server's ceiling: its ratio against dns+nft is only a
            # bound. Run that pair once more with fewer clients, where the ceiling is farther away.
            heading(f"target-bound cached rung(s): {', '.join(again)} once more with {REDUCED_CONCURRENCY} clients")
            results["reduced_concurrency_pass"] = {"clients": REDUCED_CONCURRENCY, "rungs": again}
            for rung in again:
                attempt(http_rung, rung, 1, args, sampler, REDUCED_CONCURRENCY)
        if "dns" in rungs:
            heading("server: [egress] mode dns")
            try:
                server_config("--egress-mode", "dns")
                for n in range(1, RUNS + 1):
                    heading(f"dns, run {n} of {RUNS}")
                    attempt(http_rung, "dns", n, args, sampler)
            except Exception as error:  # noqa: BLE001
                failures.add(f"dns: {type(error).__name__}: {str(error)[:200]}")
            finally:
                try:
                    server_config("--egress-mode", "dns+nft")
                except Exception as error:  # noqa: BLE001 - server_session() puts the baseline back anyway
                    failures.add(f"back to dns+nft: {str(error)[:200]}")
        if "sigv4" in rungs:
            heading("server: the egress-sigv4 image (part 5)")
            try:
                image, rebuilt = switch_to_sigv4()
                results["sigv4_image"] = image.split("@")[0] + "@" + image.split("@")[1][:19] + "..."
                results["sigv4_rebuilt"] = rebuilt
                for n in range(1, RUNS + 1):
                    heading(f"aws-floor and sigv4, run {n} of {RUNS}")
                    attempt(sts_rung, "aws-floor", n, args, sampler, awscred)
                    attempt(sts_rung, "sigv4", n, args, sampler, awscred)
            except Exception as error:  # noqa: BLE001
                failures.add(f"sigv4: {type(error).__name__}: {str(error)[:200]}")
            finally:
                try:
                    server_config("--egress-image", "base")
                except Exception as error:  # noqa: BLE001 - server_session() puts the baseline back anyway
                    failures.add(f"back to the base egress image: {str(error)[:200]}")
        results["phases"] = sampler.phase_list()
    results["records"] = records
    results["summary"] = summarize(records)
    results["proxy_note"] = PROXY_NOTE
    save(args.name, results)

    report(results)
    if restore_failed():
        failures.add("the server was not put back to the baseline")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
