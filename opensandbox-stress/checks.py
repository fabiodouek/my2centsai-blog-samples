"""Pure decisions the scripts make from what the cluster says, kept apart so the tests can feed
them recorded JSON (no cluster, no SDK):

  BASELINE                       the server settings every script starts from and ends on
  server_problems(state, want)   what in `server_config.py --show --json` differs from `want`
  node_problems(nodes, n)        nodes not Ready, cordoned, or under pressure
  precondition_problems(...)     everything a measurement refuses to start with
  scheduling_reasons(message)    "0/2 nodes are available: 2 Too many pods." -> {"pods": 2}
  refusal(pods, events, ids)     why the cluster stopped taking sandboxes: (limit, detail)
  deletable_images(...)          which snapshot images in ECR are this directory's to delete
  hog_verdict(kind, probe)       whether one of 07_noisy.py's hogs is running: (running, why not)
  dropped_runtimes(prev, wanted) runtimes whose 07 records a narrower --runtimes would overwrite
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

EXAMPLE = "opensandbox-stress"
BASELINE = {"runtime": "runc", "egress_image": "base", "egress_mode": "dns+nft", "ingress": "gateway"}
PRESSURE = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")


def server_problems(state: dict, want: dict) -> list[str]:
    """Differences between the server's settings (server_config.py --show --json) and `want`.
    The gateway counts as part of the baseline: ingress must be "gateway" and the values file must
    still exist, or the next change through server_config.py would refuse."""
    found = []
    for key, value in want.items():
        if state.get(key) != value:
            found.append(f"server {key} is {state.get(key)!r}, expected {value!r}")
    if want.get("ingress") == "gateway" and state.get("gateway_values") is False:
        found.append("../opensandbox-eks/values/server-gateway.yaml is missing (server_config.py would refuse)")
    return found


def node_problems(nodes: dict, expected: int = 2) -> list[str]:
    """From `kubectl get nodes -o json`: fewer than `expected` Ready nodes, a cordoned node, a node
    under memory, disk or PID pressure. Names are not printed (they hold the node's IP)."""
    items = nodes.get("items", [])
    found = []
    ready = 0
    for index, node in enumerate(sorted(items, key=lambda n: n["metadata"]["name"])):
        label = f"node-{chr(ord('a') + index)}"
        conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
        if conditions.get("Ready") == "True":
            ready += 1
        else:
            found.append(f"{label} is not Ready")
        if node.get("spec", {}).get("unschedulable"):
            found.append(f"{label} is cordoned")
        for kind in PRESSURE:
            if conditions.get(kind) == "True":
                found.append(f"{label} reports {kind}")
    if len(items) != expected or ready != expected:
        found.insert(0, f"{ready} of {len(items)} nodes Ready (expected {expected} of {expected})")
    return found


def precondition_problems(*, server_state: dict, want_server: dict, nodes: dict, sandboxes: Iterable[dict],
                          batchsandboxes: int, pools: Iterable[str], expected_nodes: int = 2) -> list[str]:
    """Everything that would make a measurement wrong before it starts:
      - any sandbox the server lists, ours (example=opensandbox-stress) or another part's: they
        hold CPU, memory and pod slots on the same two nodes
      - BatchSandbox objects the server does not list (a hand-made one, a stuck delete)
      - any Pool: its warm pods hold resources too
      - the server not on the expected runtime, egress image and mode (and the gateway kept)
      - nodes not Ready, cordoned or under pressure
    `sandboxes` are dicts with at least "metadata"."""
    found = []
    by_example = Counter((s.get("metadata") or {}).get("example", "(no example tag)") for s in sandboxes)
    if by_example:
        listed = ", ".join(f"{count} with example={name}" for name, count in sorted(by_example.items()))
        hint = "uv run python 99_cleanup.py" if set(by_example) == {EXAMPLE} else "the owning part's cleanup script"
        found.append(f"sandboxes exist ({listed}): delete them first ({hint})")
    if batchsandboxes and not by_example:
        found.append(f"{batchsandboxes} BatchSandbox object(s) exist in the sandbox namespace that the server does not list")
    pools = sorted(pools)
    if pools:
        found.append(f"Pool(s) exist: {', '.join(pools)} (their warm pods hold resources; delete them first)")
    found += server_problems(server_state, want_server)
    found += node_problems(nodes, expected_nodes)
    return found


# --- the server's Helm release and putting the baseline back --------------------------------------------

def helm_state(history: list[dict]) -> dict:
    """{deployed, latest, latest_status} from `helm history -o json` of the server release
    (deployed is None when no revision is deployed)."""
    if not history:
        return {"deployed": None, "latest": None, "latest_status": "missing"}
    latest = max(history, key=lambda h: int(h["revision"]))
    deployed = [int(h["revision"]) for h in history if h.get("status") == "deployed"]
    return {"deployed": max(deployed) if deployed else None, "latest": int(latest["revision"]),
            "latest_status": str(latest.get("status", "?"))}


def helm_problems(state: dict) -> list[str]:
    """A release whose latest revision is not the deployed one is not a known state: a failed
    upgrade leaves its new objects live (Helm 3 does not roll back), and server_config.py --show
    reads the last deployed revision, which then no longer describes the running server."""
    if state["latest_status"] == "missing":
        return ["the server's Helm release has no history"]
    if state["latest"] != state["deployed"] or state["latest_status"] != "deployed":
        return [f"the server's Helm release: latest revision {state['latest']} is {state['latest_status']}, "
                f"the last deployed one is {state['deployed']}"]
    return []


def put_back(*, touched: bool, run_baseline, show, history, wait=None, settle_s: float = 330,
             poll_s: float = 5) -> list[str]:
    """Bring the server back to BASELINE and say what is still wrong (empty when it holds).

    touched:       a server change was attempted in this run (successful or not)
    run_baseline() runs `server_config.py --baseline` (raises on failure)
    show()         `server_config.py --show --json`
    history()      `helm history -o json` of the release

    A pending Helm operation (an upgrade still running, or killed) is waited for up to settle_s
    first. Then --baseline runs whenever a change was attempted, the release's latest revision is
    not the deployed one, or --show differs from the baseline: --show alone is never trusted after
    a change, because it reads the last *deployed* revision. The result is read back both ways."""
    import time

    wait = wait or time.sleep
    problems: list[str] = []
    try:
        state = helm_state(history())
        waited = 0.0
        while state["latest_status"].startswith("pending-") and waited < settle_s:
            wait(poll_s)
            waited += poll_s
            state = helm_state(history())
        if state["latest_status"].startswith("pending-"):
            return [f"the server's Helm release is still {state['latest_status']} after {settle_s:.0f} s: wait for it, "
                    "then run python3 ../opensandbox-lifecycle/server_config.py --baseline"]
        needed = touched or bool(helm_problems(state)) or bool(server_problems(show(), BASELINE))
        if needed:
            run_baseline()
    except BaseException as error:  # noqa: BLE001 - reported; the read-back below says where it stands
        problems.append(f"putting the baseline back failed: {type(error).__name__}: {str(error)[:200]}")
    try:
        problems += helm_problems(helm_state(history()))
        problems += server_problems(show(), BASELINE)
    except BaseException as error:  # noqa: BLE001
        problems.append(f"reading the server's settings back failed: {type(error).__name__}: {str(error)[:200]}")
    return problems


def leftover_problems(objects: list[dict], namespace_pods: list[dict] | None = None) -> list[str]:
    """This directory's own leftovers, from `kubectl get pods,deployments,jobs,configmaps -l
    example=opensandbox-stress` in the two namespaces (items with kind, metadata): an echo server, a
    VM sampler, an API load Job, a latency client hold CPU and pod slots on the nodes being measured.
    namespace_pods are the other pods of the sandbox namespace (not sandboxes): reported, not refused."""
    found = []
    mine = sorted({f"{o.get('kind', '?')}/{o['metadata']['name']}" for o in objects
                   if not o["metadata"].get("deletionTimestamp")})
    if mine:
        found.append(f"this directory's objects are still there ({', '.join(mine[:8])}"
                     + (f" and {len(mine) - 8} more" if len(mine) > 8 else "") + "): run uv run python 99_cleanup.py")
    return found


def other_pods(pods: dict) -> list[str]:
    """Pods in the sandbox namespace that are neither sandboxes (opensandbox.io/id label) nor ours
    (example=opensandbox-stress): part 4's echo server, say. They are measured along with the rest."""
    names = []
    for pod in pods.get("items", []):
        labels = pod["metadata"].get("labels") or {}
        if "opensandbox.io/id" in labels or labels.get("example") == EXAMPLE:
            continue
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        names.append(pod["metadata"]["name"])
    return sorted(names)


def egress_rebuilt(output: str) -> bool | None:
    """From part 5's build_egress.sh output: False when it said the image was already in ECR ("not
    rebuilt"), True when it pushed a new build, None when it said neither."""
    if "not rebuilt" in output:
        return False
    if "pushed " in output:
        return True
    return None


# --- which runs count ------------------------------------------------------------------------------

def counted(records: list[dict], key: str = "failed") -> tuple[list[dict], bool]:
    """(the runs a median may use, whether it had to include failed ones). A run with any failure
    (record[key] truthy) is left out; when every run failed, all of them are used and the second
    value says the median is flagged."""
    clean = [r for r in records if not r.get(key)]
    if clean:
        return clean, False
    return list(records), bool(records)


# --- why the cluster stops taking sandboxes ---------------------------------------------------------

_SCHED = re.compile(r"(\d+) (Insufficient [\w./-]*\w|Too many pods|node\(s\) [^,.]+)")


def scheduling_reasons(message: str) -> dict[str, int]:
    """The scheduler's "0/2 nodes are available: 1 Insufficient cpu, 1 Too many pods. preemption: ..."
    as {"cpu": 1, "pods": 1}. Only the part before "preemption:" counts. Other node reasons keep
    their text ("node(s) were unschedulable")."""
    head = message.split("preemption:", 1)[0]
    out: dict[str, int] = {}
    for count, reason in _SCHED.findall(head):
        if reason.startswith("Insufficient "):
            key = reason.removeprefix("Insufficient ")
        elif reason == "Too many pods":
            key = "pods"
        else:
            key = reason
        out[key] = out.get(key, 0) + int(count)
    return out


def refusal(pods: dict, events: dict, sandbox_ids: Iterable[str] | None = None) -> tuple[str, str]:
    """(limit, detail) for the creates that failed: which resource stopped the cluster.

    pods and events are `kubectl get ... -o json` documents of the sandbox namespace (pods of the
    run; events of the namespace). The first match wins, in this order:
      1. a pod the scheduler could not place: its PodScheduled message, e.g. limit "pods" for
         "Too many pods"; with several reasons, the one reported for the most nodes
      2. a FailedScheduling event (the pod was already deleted: the server removes a sandbox
         whose create timed out)
      3. a pod or event saying no IP address could be assigned (the VPC CNI ran out): "ip"
      4. a pod that was placed but did not become ready in time: "timeout"
    else ("unknown", "")."""
    ids = set(sandbox_ids or [])

    def ours(name: str) -> bool:
        return not ids or any(name.startswith(i) for i in ids)

    def pick(reasons: dict[str, int], message: str) -> tuple[str, str]:
        limit = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else "unschedulable"
        return limit, message.split("preemption:", 1)[0].strip()

    items = [p for p in pods.get("items", []) if ours(p["metadata"]["name"])]
    for pod in items:
        for cond in pod.get("status", {}).get("conditions", []):
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                message = cond.get("message", "")
                return pick(scheduling_reasons(message), message)
    events_list = [e for e in events.get("items", []) if ours(e.get("involvedObject", {}).get("name", ""))]
    for event in events_list:
        if event.get("reason") == "FailedScheduling":
            message = event.get("message", "")
            return pick(scheduling_reasons(message), message)
    for event in events_list:
        message = event.get("message", "")
        if "assign an IP address" in message or "failed to assign" in message.lower():
            return "ip", message[:200]
    for pod in items:
        if pod.get("spec", {}).get("nodeName") and pod.get("status", {}).get("phase") in ("Pending", "Running"):
            waiting = [s.get("state", {}).get("waiting", {}).get("reason") for s in
                       pod.get("status", {}).get("containerStatuses", []) or []]
            reason = next((w for w in waiting if w), pod["status"].get("phase"))
            return "timeout", f"placed on a node but not ready in time ({reason})"
    return "unknown", ""


# --- how full the nodes are, as the scheduler counts ---------------------------------------------------

_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def cpu_milli(quantity: str | None) -> float:
    """A Kubernetes CPU quantity in millicores ("250m" -> 250, "2" -> 2000)."""
    if not quantity:
        return 0.0
    quantity = str(quantity)
    return float(quantity[:-1]) if quantity.endswith("m") else float(quantity) * 1000


def mem_bytes(quantity: str | None) -> float:
    """A Kubernetes memory quantity in bytes ("128Mi", "1Gi", "31296872Ki", "500M")."""
    if not quantity:
        return 0.0
    quantity = str(quantity)
    for suffix in sorted(_UNITS, key=len, reverse=True):
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * _UNITS[suffix]
    return float(quantity)


def pod_requests(pod: dict) -> tuple[float, float]:
    """(CPU millicores, memory bytes) the scheduler reserves for a pod: the larger of the biggest
    init container and the sum of the containers, plus the RuntimeClass overhead (Kata's)."""
    spec = pod.get("spec", {})

    def req(container: dict) -> tuple[float, float]:
        r = (container.get("resources") or {}).get("requests") or {}
        return cpu_milli(r.get("cpu")), mem_bytes(r.get("memory"))

    main = [req(c) for c in spec.get("containers", [])]
    init = [req(c) for c in spec.get("initContainers", [])]
    cpu = max([sum(c for c, _ in main)] + [c for c, _ in init])
    mem = max([sum(m for _, m in main)] + [m for _, m in init])
    overhead = spec.get("overhead") or {}
    return cpu + cpu_milli(overhead.get("cpu")), mem + mem_bytes(overhead.get("memory"))


def allocation(pods: dict, nodes: dict) -> dict[str, dict]:
    """{node alias: {pods, pods_max, cpu_pct, mem_pct}}: the share of each node's allocatable pod
    slots, CPU and memory that placed, unfinished pods request (what the scheduler compares)."""
    names = sorted(n["metadata"]["name"] for n in nodes.get("items", []))
    alias = {name: f"node-{chr(ord('a') + i)}" for i, name in enumerate(names)}
    out = {}
    for node in nodes.get("items", []):
        a = node["status"]["allocatable"]
        out[node["metadata"]["name"]] = {"pods": 0, "pods_max": int(a.get("pods", 0)), "cpu": 0.0, "mem": 0.0,
                                         "cpu_alloc": cpu_milli(a.get("cpu")), "mem_alloc": mem_bytes(a.get("memory"))}
    for pod in pods.get("items", []):
        node = pod.get("spec", {}).get("nodeName")
        if node not in out or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        cpu, mem = pod_requests(pod)
        out[node]["pods"] += 1
        out[node]["cpu"] += cpu
        out[node]["mem"] += mem
    return {alias[name]: {"pods": v["pods"], "pods_max": v["pods_max"],
                          "cpu_pct": round(100 * v["cpu"] / v["cpu_alloc"], 1) if v["cpu_alloc"] else None,
                          "mem_pct": round(100 * v["mem"] / v["mem_alloc"], 1) if v["mem_alloc"] else None}
            for name, v in out.items()}


def delivery_size(slots_per_node: list[int], placed: int, margin: int, wanted: int) -> tuple[int, str]:
    """(how many warm sandboxes the delivery test can hand out at once, and why). Every pod slot the
    nodes allow (allocatable pods), minus the pods already placed (on a quiet cluster: the system
    pods), minus `margin` slots kept free (a DaemonSet pod may be replaced), at most `wanted`."""
    total = sum(slots_per_node)
    room = total - placed - margin
    n = max(0, min(wanted, room))
    if slots_per_node and len(set(slots_per_node)) == 1:
        slots = f"{slots_per_node[0]} pod slots per node x {len(slots_per_node)} nodes = {total}"
    else:
        slots = f"{' + '.join(map(str, slots_per_node)) or '0'} = {total} pod slots"
    reason = f"{slots}, minus {placed} pods already placed (system pods), minus {margin} headroom = {room}"
    reason += f": {wanted} fit" if n == wanted else f": {n} instead of {wanted}"
    return n, reason


# --- snapshot images ----------------------------------------------------------------------------------

def deletable_images(images: list[dict], ours: set[str], existing: set[str]) -> tuple[list[dict], int, int]:
    """(image ids to delete, ours kept because the sandbox still exists, images not ours).

    An image is ours when its tag starts with "<one of our sandbox ids>-" (part 3's
    values/controller-snapshot.yaml puts the sandbox id first in the tag). Untagged images and
    other parts' images are never ours. A sandbox that still exists (Paused, say) keeps its images."""
    delete, kept, others = [], 0, 0
    for item in images:
        tag = item.get("imageTag", "")
        owner = next((sid for sid in ours if tag.startswith(sid + "-")), None) if tag else None
        if owner is None:
            others += 1
        elif owner in existing:
            kept += 1
        else:
            delete.append({"imageDigest": item["imageDigest"], "imageTag": tag})
    return delete, kept, others


# --- 07_noisy.py: is a hog running? --------------------------------------------------------------

HOG_PROCS = {"cpu": 3, "disk": 2}   # hog.py's main process plus its workers (2 spinning, or 1 writer)
CPU_HOG_MIN_M = 300                 # a spinning CPU hog uses at least this much even on a saturated node
DISK_HOG_MAX_AGE_S = 20             # a running disk hog has written a file more recently than this


def hog_verdict(kind: str, probe: dict | None) -> tuple[bool, str]:
    """(running, why not) for one hog, from loadgen/hog_probe.py's output (None: the probe did not
    answer). A CPU hog needs all its processes and CPU_HOG_MIN_M of CPU as its own sandbox's kernel
    counts it (inside a Kata VM, the guest's); a disk hog needs all its processes and a recent write."""
    if not probe:
        return False, "the probe did not answer"
    procs = probe.get("procs") or 0
    if procs < HOG_PROCS[kind]:
        return False, f"{procs} of {HOG_PROCS[kind]} hog.py processes alive"
    if kind == "cpu":
        cpu = probe.get("cpu_m")
        if cpu is None or cpu < CPU_HOG_MIN_M:
            return False, f"{cpu} m of CPU in the sandbox (a spinning hog uses at least {CPU_HOG_MIN_M})"
    else:
        age = probe.get("newest_write_s")
        if age is None or age > DISK_HOG_MAX_AGE_S:
            return False, f"last write {age} s ago (a writing hog: under {DISK_HOG_MAX_AGE_S} s)"
    return True, ""


def dropped_runtimes(previous: dict | None, runtimes: list[str]) -> list[str]:
    """Runtimes with records in the previous results/noisy.json that a run of `runtimes` would not
    write again (07_noisy.py writes its results from scratch)."""
    held = {r.get("runtime") for r in (previous or {}).get("records", [])} - {None}
    return sorted(held - set(runtimes))
