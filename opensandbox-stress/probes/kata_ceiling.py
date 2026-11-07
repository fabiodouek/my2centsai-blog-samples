"""Why Kata density stops at 24-25 sandboxes (12-13 per node) with "Insufficient cpu".

Run from the stress directory, on a quiet cluster (no sandboxes; the server on the baseline):
    uv run python probes/kata_ceiling.py [--dry-run]

The puzzle: 02_density.py's Kata runs were refused by the scheduler ("0/2 nodes are available: 2
Insufficient cpu.") with the requests it could see at 64.6-70.1% of each node's allocatable 7910m.
kata_probe.py read one Kata pod: overhead 250m / 160Mi, the sandbox container 100m / 128Mi, an
init container with no requests, so 350m per pod and room for ~20 more per node. Filling a node
at 12-13 pods means ~560m per pod in the scheduler's eyes, or a consumer nobody listed.

  1. preconditions (a quiet cluster, the server on the baseline); kubectl pinned to --context osb-eks
  2. server_config.py --runtime kata-qemu, inside server_session (--baseline in the finally)
  3. 02_density.py's Kata sandboxes (100m / 128Mi, requests = limits, no policy) created 2 at a
     time, the next pair once both are ready, until a create fails, a pod stays unscheduled for
     more than 20 s, or 40 exist. The run's pods are polled every 2 s throughout.
  4. at the first unscheduled pod (or the stop), the dump:
       - that pod's resource-related spec and status (overhead, container and init container
         resources, pod-level spec.resources, runtimeClassName, priority, QoS, nominatedNodeName,
         the status resources the scheduler may use) and its FailedScheduling events, verbatim
       - for each node, every pod bound to it (all namespaces): phase, deletionTimestamp,
         conditions, effective request (the scheduler's formula: pod-level requests if set, else
         max(containers + restartable init containers, the init phase); plus overhead; also with
         the status resources), split into this run's sandboxes, other sandboxes and other pods;
         terminating pods; pods nominated to the node; finished pods still bound (shown, but never
         an explanation: the scheduler does not count them). Taken at once and again 10 s later,
         so a transient bound-but-not-running state shows
       - `kubectl describe node`: its pods table and Allocated resources
       - the arithmetic that explains the refusal, or a plain statement that it does not add up
  5. the RuntimeClass kata-qemu (handler, overhead, scheduling) and any LimitRange or
     ResourceQuota in the sandbox namespace
  6. how long each Kata pod of the fill took from bound to its container started and to Ready
  7. finally: every sandbox of the run deleted and its pods gone; the server back to the baseline

Output: this terminal and logs/probes/kata_ceiling.log, masked (no IP, no node name, no account
ID). Nothing goes to results/.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the stress directory: common, checks

from checks import EXAMPLE, cpu_milli, mem_bytes  # noqa: E402
from common import (  # noqa: E402
    KUBE_CONTEXT,
    LOGS,
    SANDBOX_NAMESPACE,
    Failures,
    Plan,
    cli,
    create_sandbox,
    delete_run,
    describe,
    heading,
    kube_json,
    kubectl,
    new_run_id,
    node_alias,
    node_names,
    own_metadata,
    preconditions,
    restore_failed,
    say,
    server_config,
    server_session,
    show,
    wait_no_pods,
)

SCRIPT = "kata_ceiling"
CONTEXT = "osb-eks"
SHAPE = {"cpu": "100m", "memory": "128Mi"}     # 02_density.py's shape (--cpu / --memory defaults)
RUNTIME_CLASS = "kata-qemu"
BATCH = 2
CAP = 40
UNSCHEDULED_S = 20                              # an unscheduled pod older than this stops the fill
POLL_S = 2
SECOND_SNAPSHOT_S = 10
LOG_PATH = LOGS / "probes" / "kata_ceiling.log"
MIB = 2 ** 20


# --- output ----------------------------------------------------------------------------------------

class _Tee:
    """Writes to the terminal and the log file."""

    def __init__(self, stream, file):
        self.stream, self.file = stream, file

    def write(self, text):
        if self.stream is not None:
            self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        if self.stream is not None:
            self.stream.flush()
        self.file.flush()

    def isatty(self):
        return False


def tee_to_log() -> None:
    """Copy stdout and stderr to logs/probes/kata_ceiling.log, unless stdout already is that file."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_PATH.exists() and os.path.samestat(os.fstat(1), os.stat(LOG_PATH)):
            return
    except OSError:
        pass
    out = LOG_PATH.open("w")
    sys.stdout = _Tee(sys.stdout, out)
    sys.stderr = _Tee(sys.stderr, out)


def _on_hangup(signum, _frame) -> None:
    """The terminal closed. Ignore the SIGHUPs that follow (a closed terminal sends two: the second
    would abort the clean-up the first one started), point fds 1 and 2 at this script's log (a print
    to the gone terminal fails with EIO and would end the finally there), stop the tee's terminal
    copy, then end like an exception so every finally runs: sandboxes deleted, server put back."""
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
    except OSError:
        pass
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, _Tee):
            stream.stream = None       # fds 1 and 2 are the log now: the tee's own copy is enough
    print(f"--- {time.strftime('%H:%M:%S', time.gmtime())}Z the terminal hung up: cleaning up, output follows",
          flush=True)
    raise SystemExit(128 + signum)


_node_map: dict[str, str] = {}


def masked(text: str) -> str:
    """show(), with every node name replaced by its alias first."""
    text = str(text)
    for name, alias in _node_map.items():
        text = text.replace(name, alias)
    return show(text)


# --- the scheduler's arithmetic ------------------------------------------------------------------

def effective(pod: dict, *, status: bool = False) -> dict:
    """The CPU (m) and memory (bytes) the scheduler reserves for a pod (k8s resourcehelper.PodRequests):
    per resource the pod-level request when set, else max(containers + restartable init containers,
    the init phase), plus the RuntimeClass overhead. status=True takes, for each container, the larger
    of its spec request and what its status reports (allocatedResources, resources.requests), which
    the scheduler also considers with in-place resize: for regular containers and restartable init
    containers only, as upstream does (a regular init container always counts its spec)."""
    spec, st = pod.get("spec", {}), pod.get("status", {})

    def req(container: dict, statuses: str, use_status: bool = True) -> tuple[float, float]:
        r = (container.get("resources") or {}).get("requests") or {}
        cpu, mem = cpu_milli(r.get("cpu")), mem_bytes(r.get("memory"))
        if status and use_status:
            for s in st.get(statuses) or []:
                if s.get("name") == container.get("name"):
                    for src in (s.get("allocatedResources") or {}, (s.get("resources") or {}).get("requests") or {}):
                        cpu, mem = max(cpu, cpu_milli(src.get("cpu"))), max(mem, mem_bytes(src.get("memory")))
        return cpu, mem

    main = [req(c, "containerStatuses") for c in spec.get("containers", [])]
    cpu, mem = sum(c for c, _ in main), sum(m for _, m in main)
    side_cpu = side_mem = init_cpu = init_mem = 0.0
    for c in spec.get("initContainers", []):
        sidecar = c.get("restartPolicy") == "Always"
        c_cpu, c_mem = req(c, "initContainerStatuses", use_status=sidecar)
        if sidecar:                                     # runs alongside the containers
            side_cpu, side_mem = side_cpu + c_cpu, side_mem + c_mem
            init_cpu, init_mem = max(init_cpu, side_cpu), max(init_mem, side_mem)
        else:
            init_cpu, init_mem = max(init_cpu, c_cpu + side_cpu), max(init_mem, c_mem + side_mem)
    cpu, mem = max(cpu + side_cpu, init_cpu), max(mem + side_mem, init_mem)
    pod_level = (spec.get("resources") or {}).get("requests") or {}
    if pod_level.get("cpu"):
        cpu = cpu_milli(pod_level["cpu"])
    if pod_level.get("memory"):
        mem = mem_bytes(pod_level["memory"])
    overhead = spec.get("overhead") or {}
    return {"cpu": cpu + cpu_milli(overhead.get("cpu")), "mem": mem + mem_bytes(overhead.get("memory")),
            "pod_level": bool(pod_level), "overhead_cpu": cpu_milli(overhead.get("cpu"))}


def kind_of(pod: dict, run_id: str) -> str:
    labels = pod["metadata"].get("labels") or {}
    if labels.get("run") == run_id and labels.get("example") == EXAMPLE:
        return "this run"
    return "sandbox" if "opensandbox.io/id" in labels else "other"


def conditions(pod: dict) -> dict[str, str]:
    return {c["type"]: c.get("status", "?") for c in pod.get("status", {}).get("conditions") or []}


def terminal(pod: dict) -> bool:
    return pod.get("status", {}).get("phase") in ("Succeeded", "Failed")


def resource_view(pod: dict) -> dict:
    """The resource-related part of a pod's spec and status (no IPs: those fields are not taken)."""
    spec, st = pod.get("spec", {}), pod.get("status", {})

    def containers(key: str) -> list[dict]:
        return [{"name": c.get("name"), "resources": c.get("resources"), "restartPolicy": c.get("restartPolicy")}
                for c in spec.get(key) or []]

    def statuses(key: str) -> list[dict]:
        return [{"name": s.get("name"), "allocatedResources": s.get("allocatedResources"),
                 "resources": s.get("resources"), "state": list((s.get("state") or {}).keys())}
                for s in st.get(key) or []]

    return {
        "spec": {"runtimeClassName": spec.get("runtimeClassName"), "overhead": spec.get("overhead"),
                 "resources (pod level)": spec.get("resources"), "priority": spec.get("priority"),
                 "priorityClassName": spec.get("priorityClassName"), "preemptionPolicy": spec.get("preemptionPolicy"),
                 "schedulerName": spec.get("schedulerName"), "nodeSelector": spec.get("nodeSelector"),
                 "affinity": spec.get("affinity"), "tolerations": len(spec.get("tolerations") or []),
                 "topologySpreadConstraints": spec.get("topologySpreadConstraints"),
                 "initContainers": containers("initContainers"), "containers": containers("containers"),
                 "ephemeralContainers": len(spec.get("ephemeralContainers") or [])},
        "status": {"phase": st.get("phase"), "qosClass": st.get("qosClass"),
                   "nominatedNodeName": st.get("nominatedNodeName"), "resize": st.get("resize"),
                   "conditions": [{k: c.get(k) for k in ("type", "status", "reason", "message")}
                                  for c in st.get("conditions") or []],
                   "initContainerStatuses": statuses("initContainerStatuses"),
                   "containerStatuses": statuses("containerStatuses")},
    }


# --- the snapshot ----------------------------------------------------------------------------------

def snapshot(run_id: str, nodes: list[str]) -> dict:
    """Every pod bound or nominated to each node, with its effective request; the nodes' allocatable."""
    pods = kube_json("get", "pods", "-A", timeout=90).get("items", [])
    node_objs = {n["metadata"]["name"]: n for n in kube_json("get", "nodes").get("items", [])}
    out = {}
    for node in nodes:
        alloc = (node_objs.get(node) or {}).get("status", {}).get("allocatable", {})
        rows = []
        for pod in pods:
            bound = pod.get("spec", {}).get("nodeName") == node
            nominated = pod.get("status", {}).get("nominatedNodeName") == node and not bound
            if not (bound or nominated):
                continue
            spec_req, status_req = effective(pod), effective(pod, status=True)
            rows.append({"ns": pod["metadata"]["namespace"], "name": pod["metadata"]["name"],
                         "kind": kind_of(pod, run_id), "phase": pod.get("status", {}).get("phase", "?"),
                         "deleting": bool(pod["metadata"].get("deletionTimestamp")),
                         "terminal": terminal(pod), "nominated": nominated, "cond": conditions(pod),
                         "cpu": spec_req["cpu"], "mem": spec_req["mem"], "cpu_status": status_req["cpu"],
                         "pod_level": spec_req["pod_level"], "overhead_cpu": spec_req["overhead_cpu"],
                         "runtime": pod.get("spec", {}).get("runtimeClassName") or ""})
        out[node] = {"alloc_cpu": cpu_milli(alloc.get("cpu")), "alloc_mem": mem_bytes(alloc.get("memory")),
                     "alloc_pods": int(alloc.get("pods", 0) or 0), "rows": rows, "at": time.strftime("%H:%M:%S")}
    return out


def sums(rows: list[dict]) -> dict:
    """CPU sums the scheduler might use: bound and not finished (spec, status), terminating among them,
    finished but still bound, nominated."""
    live = [r for r in rows if not r["terminal"] and not r["nominated"]]
    return {"live_spec": sum(r["cpu"] for r in live), "live_status": sum(r["cpu_status"] for r in live),
            "deleting": sum(r["cpu"] for r in live if r["deleting"]),
            "terminal": sum(r["cpu"] for r in rows if r["terminal"] and not r["nominated"]),
            "nominated": sum(r["cpu"] for r in rows if r["nominated"]), "live_pods": len(live),
            "by_kind": {k: (sum(1 for r in live if r["kind"] == k), sum(r["cpu"] for r in live if r["kind"] == k))
                        for k in ("this run", "sandbox", "other")}}


def print_snapshot(label: str, snap: dict, need_cpu: float | None) -> dict[str, dict]:
    heading(label)
    verdicts = {}
    for node, view in snap.items():
        alias = node_alias(node)
        rows, s = view["rows"], sums(view["rows"])
        alloc = view["alloc_cpu"]
        print(f"--- {alias} (taken {view['at']}): allocatable cpu {alloc:.0f}m, memory "
              f"{view['alloc_mem'] / MIB:.0f} MiB, pods {view['alloc_pods']}; {s['live_pods']} pods bound and not finished")
        print(f"    {'namespace/pod':<62} {'kind':<9} {'phase':<10} {'del':<4} {'Sched/Init/Ready':<17} "
              f"{'cpu':>6} {'status':>7} {'mem':>7}  notes")
        for r in sorted(rows, key=lambda r: (r["kind"] != "this run", r["ns"], r["name"])):
            cond = "/".join(r["cond"].get(t, "-")[:1] for t in ("PodScheduled", "Initialized", "Ready"))
            notes = ", ".join(n for n, on in (("pod-level requests", r["pod_level"]),
                                               (f"overhead {r['overhead_cpu']:.0f}m", r["overhead_cpu"] > 0),
                                               ("NOMINATED here", r["nominated"]), ("finished", r["terminal"]),
                                               (f"runtime {r['runtime']}", bool(r["runtime"]))) if on)
            print(f"    {masked(r['ns'] + '/' + r['name'])[:62]:<62} {r['kind']:<9} {r['phase']:<10} "
                  f"{'yes' if r['deleting'] else '':<4} {cond:<17} {r['cpu']:>5.0f}m {r['cpu_status']:>6.0f}m "
                  f"{r['mem'] / MIB:>5.0f}Mi  {notes}")
        for kind, (count, cpu) in s["by_kind"].items():
            print(f"    {kind:<11} {count:3d} pods  {cpu:7.0f}m")
        line = (f"    requested by bound, unfinished pods: {s['live_spec']:.0f}m ({100 * s['live_spec'] / alloc:.1f}%)"
                if alloc else "    no allocatable cpu")
        if s["live_status"] != s["live_spec"]:
            line += f"; with the status resources {s['live_status']:.0f}m"
        print(line + f"; of it terminating {s['deleting']:.0f}m; nominated here {s['nominated']:.0f}m")
        if s["terminal"]:
            print(f"    finished but still bound: {s['terminal']:.0f}m (information only: the scheduler does not "
                  "count Succeeded or Failed pods, so they are never the explanation)")
        if need_cpu is not None and alloc:
            accounts = {"bound, unfinished (spec)": s["live_spec"], "with status resources": s["live_status"],
                        "+ nominated": s["live_status"] + s["nominated"]}
            fits = {name: alloc - used - need_cpu for name, used in accounts.items()}
            for name, margin in fits.items():
                print(f"    {name:<26} free {alloc - accounts[name]:7.0f}m vs {need_cpu:.0f}m asked: "
                      + (f"fits with {margin:.0f}m to spare" if margin >= 0 else f"short by {-margin:.0f}m"))
            verdicts[alias] = {"fits": {k: v >= 0 for k, v in fits.items()}, "min_margin": min(fits.values()),
                               "this_run": s["by_kind"]["this run"][0], "live_spec": s["live_spec"], "alloc": alloc}
    return verdicts


def describe_node(node: str) -> None:
    text = kubectl("describe", "node", node, timeout=60)
    start = text.find("Non-terminated Pods:")
    if start < 0:
        start = text.find("Allocated resources:")
    end = text.find("Events:", start)
    print(f"--- {node_alias(node)}\n{masked(text[start:end if end > 0 else None].rstrip())}")


def scheduling_events(pod_name: str) -> list[str]:
    events = kube_json("-n", SANDBOX_NAMESPACE, "get", "events", "--field-selector",
                       f"involvedObject.name={pod_name}", timeout=60).get("items", [])
    out = []
    for e in events:
        if e.get("reason") in ("FailedScheduling", "Scheduled", "NotTriggerScaleUp"):
            when = e.get("lastTimestamp") or e.get("eventTime") or ""
            out.append(f"{when} {e.get('reason')} x{e.get('count') or (e.get('series') or {}).get('count') or 1}: "
                       f"{e.get('message', '')}")
    return out


# --- timings of the fill ---------------------------------------------------------------------------

def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def timings(pod: dict) -> dict:
    """Seconds (1 s resolution, the API's timestamps): created -> bound, bound -> container started,
    bound -> Ready."""
    st = pod.get("status", {})
    when = {c["type"]: _ts(c.get("lastTransitionTime")) for c in st.get("conditions") or [] if c.get("status") == "True"}
    started = [_ts(((s.get("state") or {}).get("running") or {}).get("startedAt")) for s in st.get("containerStatuses") or []]
    started = [t for t in started if t is not None]
    created, bound = _ts(pod["metadata"].get("creationTimestamp")), when.get("PodScheduled")
    return {"created_to_bound": bound - created if bound and created else None,
            "bound_to_started": min(started) - bound if started and bound else None,
            "bound_to_ready": when["Ready"] - bound if when.get("Ready") and bound else None,
            "node": pod.get("spec", {}).get("nodeName", "")}


def seconds(value: float | None) -> str:
    return f"{value:5.0f}" if value is not None else "    -"


def verdict(verdicts: dict[str, dict], need: float) -> str:
    """The first accounting under which the pod fits on no node explains the refusal; none: it does
    not add up, and the smallest margin says how much the scheduler counts that no pod shows."""
    accounts = list(next(iter(verdicts.values()))["fits"])
    for name in accounts:
        if all(not v["fits"][name] for v in verdicts.values()):
            return (f"explained when counting {name}: on every node the {need:.0f}m asked is more than what is free "
                    + ("(the visible requests alone fill the nodes: look at the pods listed above)"
                       if name == accounts[0] else "(see that sum in the node views)"))
    spare = ", ".join(f"{alias} at least {v['min_margin']:.0f}m to spare" for alias, v in verdicts.items()
                      if v["min_margin"] >= 0)
    per_pod = ", ".join(f"{alias} {(v['alloc'] - v['live_spec'] - need) / max(v['this_run'], 1):.0f}m"
                        for alias, v in verdicts.items())
    return (f"it does not add up: by every sum above the pod fits ({spare}), yet the scheduler said Insufficient "
            f"cpu. It counts at least that much per node that no pod bound or nominated there requests: per "
            f"sandbox of this run, {per_pod} on top of the {need:.0f}m each")


# --- main ------------------------------------------------------------------------------------------

def plan() -> Plan:
    p = Plan("kata_ceiling.py: why Kata density stops at 12-13 sandboxes per node")
    p.checks += [f"kubectl context {CONTEXT}",
                 "preconditions: no sandboxes, no Pools, none of this directory's objects; server on the baseline; "
                 "2 nodes Ready"]
    p.server_changes.append(f"server_config.py --runtime {RUNTIME_CLASS}")
    p.step(f"server_config.py --runtime {RUNTIME_CLASS}", 0.6)
    p.step(f"Kata sandboxes of 02's shape ({SHAPE['cpu']} / {SHAPE['memory']}, no policy), {BATCH} at a time, "
           f"until a create fails, a pod is unscheduled for > {UNSCHEDULED_S} s, or {CAP} exist; pods polled every "
           f"{POLL_S} s", 2.0)
    p.step("the dump: the unscheduled pod's resource spec and FailedScheduling events; every pod bound to each node "
           f"with phase, deletionTimestamp, conditions and effective request, at once and {SECOND_SNAPSHOT_S} s later; "
           "kubectl describe node; the arithmetic", 0.5)
    p.step(f"RuntimeClass {RUNTIME_CLASS}, LimitRanges and ResourceQuotas in the sandbox namespace; bound -> "
           "running per pod", 0.1)
    p.step("finally: every sandbox of the run deleted and its pods gone", 1.0)
    p.restores.append("server_config.py --baseline (server_session)")
    p.writes.append("logs/probes/kata_ceiling.log (masked); nothing in results/")
    return p


def main() -> int:
    args = cli(__doc__)
    if KUBE_CONTEXT != CONTEXT:
        sys.exit(f"the kubectl context is {KUBE_CONTEXT!r}, not {CONTEXT}: refusing")
    the_plan = plan()
    if args.dry_run:
        the_plan.print()
        return 0
    tee_to_log()
    signal.signal(signal.SIGHUP, _on_hangup)    # replaces common's (logs/hangup.log) with this log
    the_plan.print()
    failures = Failures()
    preconditions()
    nodes = node_names()
    _node_map.update({name: node_alias(name) for name in nodes})
    run_id = new_run_id()
    meta = {**own_metadata(SCRIPT, run_id), "mode": "kata"}

    def create(_: int) -> str:
        sb = create_sandbox(meta, resource=SHAPE, ttl_minutes=30, ready_seconds=120, retry=False)
        sandbox_id = sb.id
        sb.close()
        return sandbox_id

    pool = ThreadPoolExecutor(max_workers=BATCH)
    latest: dict[str, dict] = {}            # pod name -> its last JSON seen (the timings use it)
    unscheduled_since: dict[str, float] = {}
    batches: list[dict] = []
    with server_session():
        try:
            server_config("--runtime", RUNTIME_CLASS)
            heading(f"the fill: {BATCH} at a time (sandboxes tagged run={run_id})")
            in_flight: list = []
            submitted, created_ok = 0, 0
            failed: list[str] = []
            stop, stuck = "", None
            started = time.monotonic()
            while not stop:
                if not in_flight:
                    if failed:
                        stop = f"a create failed: {failed[0]}"
                        break
                    if submitted >= CAP:
                        stop = f"the cap: {CAP} sandboxes, no refusal"
                        break
                    size = min(BATCH, CAP - submitted)
                    in_flight = [pool.submit(create, i) for i in range(size)]
                    submitted += size
                    batches.append({"batch": len(batches) + 1, "t": time.monotonic() - started})
                time.sleep(POLL_S)
                pods = kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"run={run_id}", timeout=60).get("items", [])
                now = time.monotonic()
                for pod in pods:
                    name = pod["metadata"]["name"]
                    latest[name] = pod
                    if pod["spec"].get("nodeName") or pod["metadata"].get("deletionTimestamp"):
                        unscheduled_since.pop(name, None)
                        continue
                    first = unscheduled_since.setdefault(name, now)
                    if now - first > UNSCHEDULED_S and stuck is None:
                        stuck = pod
                if stuck is not None:
                    stop = f"pod unscheduled for more than {UNSCHEDULED_S} s"
                    break
                done = [f for f in in_flight if f.done()]
                for f in done:
                    if f.exception() is not None:
                        error = f.exception()
                        failed.append(masked(f"{type(error).__name__}: {str(error)[:200]}"))
                    else:
                        created_ok += 1
                in_flight = [f for f in in_flight if not f.done()]
                if not in_flight and batches and "ready" not in batches[-1]:
                    running = {}
                    for pod in pods:
                        if pod.get("status", {}).get("phase") == "Running":
                            alias = node_alias(pod["spec"].get("nodeName", ""))
                            running[alias] = running.get(alias, 0) + 1
                    batches[-1].update({"ready": created_ok, "per_node": running})
                    say(f"batch {batches[-1]['batch']:2}: {created_ok:2} ready {dict(sorted(running.items()))}, "
                        f"{len(failed)} failed")
            say(f"stopped: {stop}")

            stuck_name = stuck["metadata"]["name"] if stuck is not None else None
            if stuck is None:   # a create failed or the cap: take the newest unscheduled pod seen, if any
                pending = [p for p in latest.values() if not p["spec"].get("nodeName")]
                stuck = max(pending, key=lambda p: p["metadata"].get("creationTimestamp", ""), default=None)
                stuck_name = stuck["metadata"]["name"] if stuck is not None else None
            need = None
            if stuck is not None:
                fresh = kube_json("-n", SANDBOX_NAMESPACE, "get", "pod", stuck_name, check=False, timeout=60)
                stuck = fresh or stuck
                need = effective(stuck)["cpu"]
                heading(f"the unscheduled pod ({'still there' if fresh else 'already deleted: its last copy'})")
                print(masked(json.dumps(resource_view(stuck), indent=1)))
                e = effective(stuck)
                print(f"  effective request: {e['cpu']:.0f}m CPU, {e['mem'] / MIB:.0f} MiB"
                      + (" (pod-level requests set)" if e["pod_level"] else "")
                      + f"; with status resources {effective(stuck, status=True)['cpu']:.0f}m")
                heading("its scheduling events (verbatim)")
                events = scheduling_events(stuck_name)
                for line in events or ["(none found)"]:
                    print(f"  {masked(line)}")
            else:
                heading("no unscheduled pod was seen: the node views below have no pod to compare with")

            first = snapshot(run_id, nodes)
            verdicts = print_snapshot("every pod bound to each node: now", first, need)
            time.sleep(SECOND_SNAPSHOT_S)
            second = snapshot(run_id, nodes)
            print_snapshot(f"every pod bound to each node: {SECOND_SNAPSHOT_S} s later", second, need)

            heading("kubectl describe node: pods table and Allocated resources")
            for node in nodes:
                describe_node(node)

            heading(f"RuntimeClass {RUNTIME_CLASS}; LimitRanges and ResourceQuotas in {SANDBOX_NAMESPACE}")
            rc = kube_json("get", "runtimeclass", RUNTIME_CLASS, check=False)
            print(f"  handler {rc.get('handler')!r}, overhead {json.dumps(rc.get('overhead'))}, "
                  f"scheduling {json.dumps(rc.get('scheduling'))}" if rc else f"  RuntimeClass {RUNTIME_CLASS} not found")
            limits = kube_json("-n", SANDBOX_NAMESPACE, "get", "limitranges,resourcequotas", check=False).get("items", [])
            for item in limits:
                print(f"  {item.get('kind')} {item['metadata']['name']}: {masked(json.dumps(item.get('spec')))}")
                if item.get("kind") == "ResourceQuota":
                    print(f"    used {masked(json.dumps((item.get('status') or {}).get('used')))}")
            if not limits:
                print("  none")

            heading("the fill's Kata pods: bound -> container started -> Ready (API timestamps, 1 s resolution)")
            rows = [timings(p) for p in latest.values() if p["spec"].get("nodeName")]
            for key in ("created_to_bound", "bound_to_started", "bound_to_ready"):
                values = [r[key] for r in rows if r[key] is not None]
                d = describe(key, values)
                print(f"  {key:<17} n {d['n']:>2}" + (f"  min {d['min']:5.1f}  p50 {d['p50']:5.1f}  p95 {d['p95']:5.1f}  "
                                                    f"max {d['max']:5.1f} s" if d["n"] else ""))
            ordered = sorted(latest.values(), key=lambda pod: pod["metadata"].get("creationTimestamp", ""))
            for i, pod in enumerate(ordered, 1):
                t = timings(pod)
                print(f"    {i:2}. {node_alias(t['node']) if t['node'] else 'unbound':<8} created->bound "
                      f"{seconds(t['created_to_bound'])} s, bound->started {seconds(t['bound_to_started'])} s, "
                      f"bound->ready {seconds(t['bound_to_ready'])} s, phase {pod.get('status', {}).get('phase')}")

            heading("the arithmetic")
            bound_now = {node_alias(node): sums(view["rows"])["by_kind"]["this run"][0] for node, view in first.items()}
            print(f"  {created_ok} sandboxes ready; this run's pods bound at the dump {bound_now}; stop: {masked(stop)}")
            if need is None or not verdicts:
                print("  no unscheduled pod to explain")
            else:
                print(f"  {verdict(verdicts, need)}")
        except Exception as error:  # noqa: BLE001 - reported; the clean-up below still runs
            failures.add(f"{type(error).__name__}: {masked(str(error))[:300]}")
        finally:
            heading("clean up")
            say(f"deleting the sandboxes of run {run_id}")
            delete_run(run_id)
            pool.shutdown(wait=True, cancel_futures=True)   # a create still waiting ends when its sandbox is gone
            delete_run(run_id)                              # any create that finished after the first delete
            failures.check(wait_no_pods(f"run={run_id}", timeout=600), "the run's pods are gone")
    if restore_failed():
        failures.add("the server is not back on the baseline (see above)")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
