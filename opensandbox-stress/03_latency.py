"""Create latency under concurrency, cold and from a Pool, and how fast a Pool hands out sandboxes.

Where the client runs matters, so every number says where it was taken:
  in-cluster    a pod next to the server (manifests/latency-client.yaml, osb-stress-client) runs
                loadgen/create_latency.py with the same SDK (opensandbox==1.2.0) against the
                server's Service. Steps 1, 2 and the API path of 4 are measured from here: the
                laptop's kubectl port-forward, which would carry every create and every health-check
                poll of 40 or 87 creates at once, is not in the path.
  laptop        one cold/warm pair at one create at a time through the port-forward, labelled
                "via port-forward", so the numbers can be set beside part 3's (also taken that way).
                Never mixed with the in-cluster numbers.
  controller    step 3 and the controller path of 4: the laptop through `kubectl proxy` to the
                Kubernetes API, timed with a watch; the round trip to the API is recorded beside them.

1. Cold: N creates at once (N = --levels, default 1,5,10,20,40), each a new pod (shape --cpu 250m
   --memory 512Mi). A create is timed from the SDK call until the sandbox answers its health check,
   as a user waits for it. Each level runs RUNS rounds, and more for the small levels, so every level
   has at least 30 samples. One discarded create first warms each path.
2. Warm: the same with extensions={"poolRef": "osb-stress-pool"} against a Pool (manifests/pool.yaml)
   holding 40 Ready pods of the same shape and allowed no more (poolMax), so it creates no
   replacement while sandboxes are held. Rounds keep their sandboxes until the Pool would run short,
   then all are deleted and the Pool is back to full before the next round: no replacement pod
   starts during a measured round.
3. The controller alone: a BatchSandbox with poolRef applied straight to the Kubernetes API (no
   lifecycle server, no SDK, no health check), timed until the watch reports the pods allocated
   (status.allocated) and ready. One at a time, and ten in one object.
4. Delivery: a Pool of as many small pods (100m/128Mi) as the nodes have pod slots for, up to 100
   (the number and the reason are printed), handed out at once: one BatchSandbox with that many
   replicas (the controller path), then as many creates at once from the in-cluster client.

Failed creates and claims that time out are never samples: they are counted per round, written to
the results and printed, and a level with any is marked. p50 and p95 are reported; below 100
samples the nearest-rank p99 is the maximum, so it is kept in the results but not printed.

Published reference points, printed next to our numbers and not reproduced:
  - the release's operator docs (docs/architecture/control-plane/operator.md): "measured delivery
    of 100 sandboxes" with pooling, 0.92 s for a BatchSandbox. No hardware or setup is given.
  - an AWS China blog post (2026-07-15): about 230 ms to claim a sandbox from a warm Pool (about
    140 ms each in a batch of 10) and about 3.1 s cold, with the controller only (no lifecycle
    server, egress sidecar or ingress), Kata with Cloud Hypervisor, on c5.metal.

Runs on runc with the base egress image (the server is not changed). Writes results/latency.json
and results/latency.csv (the server's and the controller's CPU and memory through the test).
Deletes its sandboxes, its BatchSandboxes, the Pool and the client pod.
"""

from __future__ import annotations

import json
import math
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from checks import delivery_size
from common import (
    BATCHSANDBOXES,
    LOADGEN,
    POOL_NAME,
    RUNS,
    SANDBOX_NAMESPACE,
    SYSTEM_NAMESPACE,
    Failures,
    KubeApi,
    Plan,
    Sampler,
    apply_pool,
    bound,
    cli,
    create_sandbox,
    delete_pool,
    delete_run,
    describe,
    ensure_port_forward,
    heading,
    image,
    kube_json,
    kubectl,
    last_json,
    new_run_id,
    own_metadata,
    parallel,
    pct,
    pod_slots,
    pool_status,
    preconditions,
    render_manifest,
    save,
    say,
    show,
    spread,
    wait_no_pods,
    wait_pool_available,
    wait_pool_full,
)

SCRIPT = "03_latency"
MIN_SAMPLES = 30
DELIVERY_MAX = 100
SLOT_MARGIN = 4          # pod slots left free during the delivery test (a DaemonSet pod may restart)
CLAIMS = 10              # single controller-path claims per run
# Our hand-made BatchSandboxes carry this label (the server's own carry our metadata instead), so the
# clean up here and in 99_cleanup.py deletes them and nothing the server made.
CLAIM_LABEL = "osb-stress-claim"
BS_PATH = f"/apis/sandbox.opensandbox.io/v1alpha1/namespaces/{SANDBOX_NAMESPACE}/batchsandboxes"
REFERENCES = {
    "operator_docs": {"what": "measured delivery of 100 sandboxes with pooling, BatchSandbox", "total_s": 0.92,
                      "source": "docs/architecture/control-plane/operator.md (release 1.2.0)",
                      "setup": "not stated"},
    "aws_china_blog": {"warm_claim_ms": 230, "batch_of_10_each_ms": 140, "cold_s": 3.1,
                       "source": "https://aws.amazon.com/cn/blogs/china/amazon-eks-build-security-ai-agent/",
                       "setup": "controller only (no lifecycle server, egress sidecar or ingress); Kata with "
                                "Cloud Hypervisor; c5.metal"},
}


def arguments(parser) -> None:
    parser.add_argument("--levels", default="1,5,10,20,40", help="creates in flight at once (default 1,5,10,20,40)")
    parser.add_argument("--cpu", default="250m", help="sandbox and pool pod CPU for steps 1-3 (default 250m)")
    parser.add_argument("--memory", default="512Mi", help="sandbox and pool pod memory for steps 1-3 (default 512Mi)")
    parser.add_argument("--skip", default="", help="steps to leave out, e.g. 'delivery' or 'cold,warm'")


def rounds_for(n: int) -> int:
    return max(RUNS, math.ceil(MIN_SAMPLES / n))


CLIENT = "osb-stress-client"
CLIENT_BOUND_M = 800      # the in-cluster client is one Python process: at 80% of one core (in its
                          # busiest second) it is the limit
CLIENT_PATHS = {
    "in_cluster": "a pod in opensandbox-system (osb-stress-client), SDK opensandbox==1.2.0 -> the server's Service "
                  "(opensandbox-server.opensandbox-system.svc:80), server proxy on for the health check",
    "laptop": "the laptop, SDK opensandbox==1.2.0 -> kubectl port-forward 127.0.0.1:18080 -> the server",
    "controller": "the laptop -> kubectl proxy -> the Kubernetes API (create, then a watch); no lifecycle server",
}


@contextmanager
def in_cluster_client():
    """The client pod for the duration of the with: its ConfigMap from loadgen/create_latency.py, the
    pod from manifests/latency-client.yaml (it installs the SDK, then waits), deleted at the end."""
    def remove() -> None:
        kubectl("-n", SYSTEM_NAMESPACE, "delete", "pod", CLIENT, "--ignore-not-found", "--wait=true", "--timeout=60s",
                check=False, timeout=90)
        kubectl("-n", SYSTEM_NAMESPACE, "delete", "configmap", CLIENT, "--ignore-not-found", check=False)

    heading(f"the in-cluster client ({CLIENT})")
    try:
        remove()
        kubectl("apply", "-f", "-", stdin=json.dumps({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CLIENT, "namespace": SYSTEM_NAMESPACE,
                         "labels": {"app": CLIENT, "example": "opensandbox-stress"}},
            "data": {"create_latency.py": (LOADGEN / "create_latency.py").read_text(),
                     "client-requirements.txt": (LOADGEN / "client-requirements.txt").read_text()}}))
        kubectl("apply", "-f", "-", stdin=render_manifest("latency-client.yaml"))
        wait_client_ready(timeout=300)
        say(f"{CLIENT} ready (SDK installed from the pinned, hashed requirements)")
        yield
    finally:
        remove()
        say(f"{CLIENT} deleted")


def wait_client_ready(timeout: float) -> None:
    """Wait for the client pod to be Ready; stop at once (with the end of its log, masked) when it
    failed, e.g. pip refused a hash or could not reach PyPI, rather than wait out the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = kube_json("-n", SYSTEM_NAMESPACE, "get", "pod", CLIENT, check=False)
        status = pod.get("status") or {}
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
            return
        terminated = [s["state"]["terminated"] for s in status.get("containerStatuses", []) or []
                      if (s.get("state") or {}).get("terminated")]
        if status.get("phase") == "Failed" or terminated:
            raise RuntimeError(f"the client pod {CLIENT} failed before it was ready (installing the SDK?): "
                               + client_log_summary())
        time.sleep(2)
    raise RuntimeError(f"the client pod {CLIENT} was not ready within {timeout:.0f} s: " + client_log_summary())


def client_log_summary() -> str:
    """Print the useful end of the client pod's log (masked): pip's ERROR lines when there are any,
    else the last 15 lines. Returns the last ERROR line (or the last line) for the one-line failure."""
    lines = [line for line in kubectl("-n", SYSTEM_NAMESPACE, "logs", CLIENT, "--tail=60", check=False).splitlines()
             if line.strip()]
    errors = [line for line in lines if "ERROR" in line]
    shown = errors[-8:] or lines[-15:]
    print(f"  the end of {CLIENT}'s log:" if shown else f"  {CLIENT} logged nothing", flush=True)
    for line in shown:
        print(f"      {show(line)[:300]}", flush=True)
    return show((errors or lines or ["(no log)"])[-1])[:200]


def client_round(n: int, run_id: str, step: str, *, pooled: bool, shape: dict, keep: bool = False) -> dict:
    """One round of N creates at once from the in-cluster client; its JSON (ms of the successful
    creates only, failures counted). keep=True leaves the sandboxes for the caller to delete."""
    args = ["--n", str(n), "--run", run_id, "--step", step, "--image", image(), "--cpu", shape["cpu"],
            "--memory", shape["memory"]] + (["--pool", POOL_NAME] if pooled else []) + (["--keep"] if keep else [])
    out = kubectl("-n", SYSTEM_NAMESPACE, "exec", CLIENT, "--", "python3", "/app/create_latency.py", *args,
                  timeout=600 + 5 * n)
    return last_json(out)


def laptop_round(n: int, run_id: str, step: str, *, pooled: bool, shape: dict, keep: bool = False) -> dict:
    """The same round from the laptop through the port-forward (only for the labelled pair)."""
    meta = {**own_metadata(SCRIPT, run_id), "step": step}

    def create(_: int) -> str:
        sb = create_sandbox(meta, resource=shape, extensions={"poolRef": POOL_NAME} if pooled else None,
                            ttl_minutes=20, retry=False)
        sb.close()
        return sb.id

    ensure_port_forward()
    results = parallel(create, range(n), workers=n, together=True)
    ok = [x for x in results if x.ok]
    errors: dict[str, int] = {}
    for x in results:
        if not x.ok:
            errors[x.error_type or "error"] = errors.get(x.error_type or "error", 0) + 1
    if not keep:
        delete_run(run_id)
    return {"n": n, "ok": len(ok), "ms": [round(x.ms, 1) for x in ok], "failed": len(results) - len(ok),
            "errors": errors, "error_samples": [show(f"{x.error_type}: {(x.error or '')[:160]}") for x in results
                                                if not x.ok][:3],
            "wall_ms": round(1000 * (max(x.end for x in ok) - min(x.start for x in results)), 1) if ok else None}


def level(n: int, step: str, *, pooled: bool, shape: dict, top: int, via: str, failures: Failures,
          rounds: int | None = None) -> dict:
    """rounds_for(N) rounds of N creates at once (via the in-cluster client or the laptop), each round
    starting on empty nodes (cold) or with no Pool refill under way (warm). Failures are counted,
    never samples.

    Warm: the Pool holds `top` pods and may not grow past them (poolMax), so while sandboxes are
    held it creates no replacement. Rounds therefore keep their sandboxes until the Pool would run
    short (top // N rounds), then all are deleted and the Pool is waited back to full: no measured
    round overlaps a refill, and a 1-at-a-time level needs one refill, not thirty."""
    run_id = new_run_id()
    rounds = rounds or rounds_for(n)
    per_fill = max(1, top // n) if pooled else 1
    samples, per_round, walls, errors, failed_rounds, failed = [], [], [], {}, 0, 0
    client_cpu: list[float | None] = []
    try:
        for index in range(rounds):
            if pooled and index % per_fill == 0:
                if index:
                    delete_run(run_id)                 # the held rounds' sandboxes; their pods are recycled
                wait_pool_full(top)                    # full again, and no released pod still terminating
            result = (client_round if via == "in_cluster" else laptop_round)(n, run_id, step, pooled=pooled,
                                                                             shape=shape, keep=pooled)
            samples += result["ms"]
            per_round.append(pct(result["ms"], 50) if result["ms"] else None)
            client_cpu.append(result.get("client_cpu_peak_m", result.get("client_cpu_m")))
            if result.get("wall_ms") is not None:
                walls.append(result["wall_ms"])
            if result["failed"]:
                failed += result["failed"]
                failed_rounds += 1
                for kind, count in result["errors"].items():
                    errors[kind] = errors.get(kind, 0) + count
            if not pooled:
                delete_run(run_id)                     # whatever a failed kill left
            if not pooled and n >= 5:
                # A terminating pod keeps its CPU, memory and pod slot until it is gone (up to its 30 s
                # grace period): wait, so the next round starts on empty nodes.
                wait_no_pods(f"run={run_id}", timeout=300)
    finally:
        # Also when a round raised: held warm sandboxes (up to `top` of them) must not outlive the level.
        delete_run(run_id)
        wait_no_pods(f"run={run_id}", timeout=300)
    label = f"{step} x{n}" + (" (via port-forward)" if via == "laptop" else "")
    d = describe(label, samples)
    if failed:
        failures.add(f"{label}: {failed} create(s) failed in {failed_rounds} of {rounds} round(s): {errors}")
    busy = [c for c in client_cpu if c is not None and c >= CLIENT_BOUND_M] if via == "in_cluster" else []
    say(f"{label:>26}: p50 {d['p50']} ms  p95 {d['p95']}  (n={d['n']}, {rounds} rounds"
        + (f", {failed} FAILED, not counted" if failed else "")
        + (f", CLIENT-BOUND in {len(busy)} round(s)" if busy else "") + ")")
    return {"n": n, "via": via, "rounds": rounds, "describe": d, "p50_by_round": spread(per_round),
            "wall_ms": spread(walls), "failed": failed, "failed_rounds": failed_rounds, "errors": errors,
            "flagged": bool(failed), "client_cpu_peak_m": client_cpu, "client_bound_rounds": len(busy), "ms": samples}


def warm_up(step: str, *, pooled: bool, shape: dict, top: int, via: str) -> None:
    """One create, discarded: the first call of a path pays for connections and caches."""
    if pooled:
        wait_pool_full(top)
    run_id = new_run_id()
    (client_round if via == "in_cluster" else laptop_round)(1, run_id, f"{step}-warmup", pooled=pooled, shape=shape)
    delete_run(run_id)
    wait_no_pods(f"run={run_id}", timeout=120)


def claim(api: KubeApi, replicas: int, name: str, delete: bool = True) -> dict:
    """One BatchSandbox with poolRef straight to the API: ms to created, allocated, ready. delete=False
    leaves it (the caller deletes several at once, so no Pool refill runs between claims)."""
    expire = (datetime.now(timezone.utc) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"apiVersion": "sandbox.opensandbox.io/v1alpha1", "kind": "BatchSandbox",
            "metadata": {"name": name, "namespace": SANDBOX_NAMESPACE,
                         "labels": {"example": "opensandbox-stress", CLAIM_LABEL: "true"}},
            "spec": {"replicas": replicas, "poolRef": POOL_NAME, "expireTime": expire}}
    started = time.monotonic()
    created = api.call("POST", BS_PATH, body)
    t_created = time.monotonic()
    rv = created["metadata"]["resourceVersion"]
    seen: dict[str, float] = {}

    def done(obj: dict) -> bool:
        status = obj.get("status") or {}
        now = time.monotonic()
        if status.get("allocated", 0) >= replicas:
            seen.setdefault("allocated", now)
        if status.get("ready", 0) >= replicas:
            seen.setdefault("ready", now)
        return "ready" in seen

    api.watch(BS_PATH, rv, done, timeout=180, name=name)
    if delete:
        api.call("DELETE", f"{BS_PATH}/{name}")
    ms = lambda t: round(1000 * (t - started), 1) if t else None  # noqa: E731
    return {"replicas": replicas, "via": "controller", "created_ms": ms(t_created),
            "allocated_ms": ms(seen.get("allocated")), "ready_ms": ms(seen.get("ready")), "timed_out": "ready" not in seen}


def claims_summary(label: str, claims: list[dict], failures: Failures) -> dict:
    """Percentiles over the claims that completed; the ones that timed out counted apart."""
    done = [c for c in claims if not c["timed_out"]]
    out = {"via": "controller", "allocated": describe(f"allocated {label}", [c["allocated_ms"] for c in done]),
           "ready": describe(f"ready {label}", [c["ready_ms"] for c in done]),
           "timed_out": len(claims) - len(done), "claims": claims}
    if out["timed_out"]:
        failures.add(f"controller path {label}: {out['timed_out']} of {len(claims)} claim(s) not ready within 180 s "
                     "(left out of the percentiles)")
    return out


def rtt(api: KubeApi) -> float:
    times = []
    for _ in range(10):
        started = time.monotonic()
        api.call("GET", "/version")
        times.append(1000 * (time.monotonic() - started))
    return round(pct(times, 50), 1)


def main() -> int:
    args = cli(__doc__, arguments)
    levels = sorted({int(x) for x in args.levels.split(",") if x.strip()})
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    if not levels or levels[0] < 1:
        sys.exit("--levels needs positive numbers")
    shape = {"cpu": args.cpu, "memory": args.memory}
    top = max(levels)
    in_cluster = bool({"cold", "warm", "delivery"} - skip)
    plan = Plan(f"03_latency.py: creates at {', '.join(map(str, levels))} in flight, cold and warm (in-cluster "
                "client); one pair via the port-forward; the controller path; a Pool delivery")
    plan.checks.append("no sandboxes, no Pools, none of this directory's objects; server runc + base egress + "
                       "dns+nft + gateway (Helm's latest revision deployed); 2 nodes Ready")
    if in_cluster:
        plan.step(f"apply the client pod {CLIENT} in {SYSTEM_NAMESPACE} (python:3.12-slim + pip install "
                  "opensandbox==1.2.0) and wait for it", 1)
    if "cold" not in skip:
        plan.step("cold warm-up: one create from the client and one from the laptop, discarded", 0.3)
        for n in levels:
            plan.step(f"cold x{n} (in-cluster): {rounds_for(n)} round(s) of {n} creates at once ({args.cpu}/{args.memory})",
                      round(rounds_for(n) * (0.15 if n < 5 else 0.75), 1))
        plan.step(f"cold x1 via port-forward (laptop): {rounds_for(1)} rounds, labelled as such", 1.5)
    if not {"warm", "controller"} <= skip:
        plan.step(f"apply Pool {POOL_NAME} ({top} warm pods of {args.cpu}/{args.memory}) and wait for it to fill", 1.5)
    if "warm" not in skip:
        plan.step("warm warm-up: one pooled create from the client and one from the laptop, discarded", 0.5)
        for n in levels:
            fills = -(-rounds_for(n) // max(1, top // n))
            plan.step(f"warm x{n} (in-cluster): {rounds_for(n)} round(s) of {n} pooled creates at once, held until "
                      f"the Pool would run short, {fills} refill(s) to {top}", round(rounds_for(n) * 0.05 + fills * 0.8, 1))
        plan.step(f"warm x1 via port-forward (laptop): {rounds_for(1)} rounds, labelled as such", 2)
    if "controller" not in skip:
        plan.step(f"controller path: {CLAIMS} single BatchSandbox claims held from one full Pool, then one of 10 "
                  f"replicas, x{RUNS}", RUNS * 2)
    if "delivery" not in skip:
        plan.step(f"Pool of N = min({DELIVERY_MAX}, pod slots per node x nodes - pods placed - {SLOT_MARGIN} headroom) "
                  "pods of 100m/128Mi (N and why printed); per run: one BatchSandbox with that many replicas, then "
                  "that many creates at once from the in-cluster client", 4 + 3.5 * RUNS)
    plan.step(f"delete the Pool {POOL_NAME}, the client pod and every sandbox and BatchSandbox made here", 1)
    plan.writes += ["results/latency.json", "results/latency.csv"]
    plan.print()
    if args.dry_run:
        return 0

    preconditions()
    failures = Failures()
    results: dict = {"shape": shape, "levels": levels, "min_samples": MIN_SAMPLES, "references": REFERENCES,
                     "client": CLIENT_PATHS, "cold": {}, "warm": {}, "via_port_forward": {}}
    try:
        with Sampler("latency", every=2.0) as sampler, KubeApi() as api, \
                (in_cluster_client() if in_cluster else _nothing()):
            results["api_rtt_ms"] = rtt(api)
            say(f"round trip to the Kubernetes API from here: {results['api_rtt_ms']} ms (median of 10 GET /version)")
            if "cold" not in skip:
                heading("1. cold creates (in-cluster client)")
                warm_up("cold", pooled=False, shape=shape, top=top, via="in_cluster")
                for n in levels:
                    with sampler.phase(f"cold x{n}"):
                        results["cold"][str(n)] = level(n, "cold", pooled=False, shape=shape, top=top, via="in_cluster",
                                                        failures=failures)
                heading("cold x1 via the port-forward (laptop), for comparison with part 3")
                warm_up("cold", pooled=False, shape=shape, top=top, via="laptop")
                with sampler.phase("cold x1 via port-forward"):
                    results["via_port_forward"]["cold"] = level(1, "cold", pooled=False, shape=shape, top=top,
                                                                via="laptop", failures=failures)
            if not {"warm", "controller"} <= skip:
                heading(f"Pool {POOL_NAME}: {top} warm pods")
                apply_pool(cpu=args.cpu, memory=args.memory, buffer_min=top, buffer_max=top, pool_max=top)
                results["pool_fill_s"] = round(wait_pool_available(top, timeout=900), 1)
                say(f"{top} pods Ready in the Pool {results['pool_fill_s']} s after the apply")
            if "warm" not in skip:
                heading("2. warm creates (poolRef; in-cluster client)")
                warm_up("warm", pooled=True, shape=shape, top=top, via="in_cluster")
                for n in levels:
                    with sampler.phase(f"warm x{n}"):
                        results["warm"][str(n)] = level(n, "warm", pooled=True, shape=shape, top=top, via="in_cluster",
                                                        failures=failures)
                heading("warm x1 via the port-forward (laptop), for comparison with part 3")
                warm_up("warm", pooled=True, shape=shape, top=top, via="laptop")
                with sampler.phase("warm x1 via port-forward"):
                    results["via_port_forward"]["warm"] = level(1, "warm", pooled=True, shape=shape, top=top,
                                                                via="laptop", failures=failures)
            if "controller" not in skip:
                heading("3. the controller alone: BatchSandbox with poolRef")
                singles, batches = [], []
                with sampler.phase("controller claims"):
                    for r in range(1, RUNS + 1):
                        # CLAIMS single claims held from one full Pool (no refill runs while they are
                        # held: the Pool is at its poolMax), then deleted together.
                        wait_pool_full(top)
                        held = []
                        for i in range(CLAIMS):
                            name = f"osb-stress-claim-{r}-{i}-{new_run_id()[:4]}"
                            singles.append(claim(api, 1, name, delete=False))
                            held.append(name)
                        for name in held:
                            api.call("DELETE", f"{BS_PATH}/{name}")
                        wait_pool_full(top)
                        batches.append(claim(api, 10, f"osb-stress-claim10-{r}-{new_run_id()[:4]}"))
                results["controller"] = {"single": claims_summary("x1", singles, failures),
                                         "batch_of_10": claims_summary("x10", batches, failures)}
                c = results["controller"]
                say(f"single claim: allocated p50 {c['single']['allocated']['p50']} ms, ready p50 {c['single']['ready']['p50']} ms "
                    f"(API round trip {results['api_rtt_ms']} ms)")
                say(f"10 in one object: allocated p50 {c['batch_of_10']['allocated']['p50']} ms")
            if not {"warm", "controller"} <= skip:
                delete_pool()
            if "delivery" not in skip:
                heading("4. delivery: as many warm sandboxes as the nodes take, at once")
                slots, placed = pod_slots()
                n, why = delivery_size(slots, placed, SLOT_MARGIN, DELIVERY_MAX)
                say(f"delivering {n}: {why}")
                if n < 1:
                    raise RuntimeError(f"no room for a delivery: {why}")
                apply_pool(cpu="100m", memory="128Mi", buffer_min=n, buffer_max=n, pool_max=n)
                fill = wait_pool_available(n, timeout=1200)
                delivery = {"n": n, "why": why, "slots_per_node": slots, "placed": placed, "margin": SLOT_MARGIN,
                            "fill_s": round(fill, 1), "controller": [], "api": [], "refill_s": [],
                            "api_client": CLIENT_PATHS["in_cluster"]}
                say(f"{n} pods Ready in the Pool after {fill:.0f} s")
                small = {"cpu": "100m", "memory": "128Mi"}
                for r in range(1, RUNS + 1):
                    with sampler.phase(f"delivery controller {r}"):
                        delivery["controller"].append(claim(api, n, f"osb-stress-delivery-{r}-{new_run_id()[:4]}"))
                    last = delivery["controller"][-1]
                    say(f"run {r}: one BatchSandbox x{n}: allocated after {last['allocated_ms']} ms, ready after "
                        f"{last['ready_ms']} ms" + (" (TIMED OUT)" if last["timed_out"] else ""))
                    if last["timed_out"]:
                        failures.add(f"delivery run {r}: the BatchSandbox x{n} was not ready within 180 s")
                    delivery["refill_s"].append(round(wait_pool_full(n, timeout=1200), 1))
                    run_id = new_run_id()
                    with sampler.phase(f"delivery api {r}"):
                        out = client_round(n, run_id, "delivery", pooled=True, shape=small)
                    out["describe"] = describe(f"api x{n}", out["ms"])
                    out.pop("ms", None)
                    out["via"] = "in_cluster"
                    out["client_bound"] = bound(out.get("client_cpu_peak_m", out.get("client_cpu_m")), CLIENT_BOUND_M)
                    if out["client_bound"]:
                        say(f"run {r}: CLIENT-BOUND: the in-cluster client reached {out.get('client_cpu_peak_m')}m in its "
                            f"busiest second ({out.get('client_cpu_m')}m over the round; one Python process): the "
                            "delivery time includes the client's own limit")
                    delivery["api"].append(out)
                    say(f"run {r}: {n} creates at once (in-cluster): {out['ok']} ok, all answered after {out['wall_ms']} ms "
                        f"(p50 {out['describe']['p50']} ms each)" + (f", {out['failed']} FAILED" if out["failed"] else ""))
                    if out["failed"]:
                        failures.add(f"delivery run {r}: {out['failed']} pooled create(s) failed: {out['errors']}")
                    delete_run(run_id)
                    delivery["refill_s"].append(round(wait_pool_full(n, timeout=1200), 1))
                good_claims = [c for c in delivery["controller"] if not c["timed_out"]]
                good_api = [a for a in delivery["api"] if not a["failed"]]
                delivery["controller_allocated_ms"] = spread([c["allocated_ms"] for c in good_claims])
                delivery["controller_ready_ms"] = spread([c["ready_ms"] for c in good_claims])
                delivery["api_wall_ms"] = spread([a["wall_ms"] for a in good_api])
                delivery["runs_left_out"] = {"controller": len(delivery["controller"]) - len(good_claims),
                                             "api": len(delivery["api"]) - len(good_api)}
                results["delivery"] = delivery
            results["phases"] = sampler.phase_list()
    except Exception as error:  # noqa: BLE001 - one line, then clean up
        failures.add(f"{type(error).__name__}: {str(error)[:240]}")
    finally:
        heading("clean up")
        kubectl("-n", SANDBOX_NAMESPACE, "delete", BATCHSANDBOXES, "-l", f"{CLAIM_LABEL}=true", "--ignore-not-found",
                "--wait=false", check=False)
        delete_pool()
        print(f"  Pool {POOL_NAME} deleted; {pool_status() or 'no status'}")
    save("latency", results)

    heading("create latency (ms; in-cluster client; p99 is the maximum below 100 samples, so p95)")
    print(f"  {'in flight':>9}  {'cold p50':>9} {'p95':>7}   {'warm p50':>9} {'p95':>7}")
    for n in levels:
        c = results["cold"].get(str(n), {})
        w = results["warm"].get(str(n), {})
        cd, wd = c.get("describe", {}), w.get("describe", {})
        mark = "  * failures, not counted" if c.get("flagged") or w.get("flagged") else ""
        print(f"  {n:>9}  {str(cd.get('p50', '-')):>9} {str(cd.get('p95', '-')):>7}   "
              f"{str(wd.get('p50', '-')):>9} {str(wd.get('p95', '-')):>7}{mark}")
    pf = results["via_port_forward"]
    if pf:
        print(f"  via port-forward (laptop), x1: cold p50 {pf.get('cold', {}).get('describe', {}).get('p50', '-')} ms, "
              f"warm p50 {pf.get('warm', {}).get('describe', {}).get('p50', '-')} ms (a different path: not comparable "
              "with the rows above)")
    if "delivery" in results:
        d = results["delivery"]
        print(f"  delivery of {d['n']} ({d['why']})")
        print(f"    controller path allocated in {d['controller_allocated_ms']['median']} ms (median), "
              f"in-cluster API path {d['api_wall_ms']['median']} ms; published: 100 in 920 ms (no setup stated)")
    return failures.report()


@contextmanager
def _nothing():
    yield


if __name__ == "__main__":
    sys.exit(main())
