"""Is the early Kata refusal OpenSandbox's, or Kubernetes' and Kata's? A control without OpenSandbox.

Run from the stress directory:
    uv run python probes/kata_plain.py [--dry-run] [--cap 30]

kata_ceiling.py found the scheduler refusing a 13th OpenSandbox Kata sandbox on a node whose visible
requests were 12 x 350m + 540m = 4740m of 7910m ("does not add up"): solved for one per-pod cost,
the counts at refusal imply 567-599m per Kata pod, about one more 250m overhead. This probe asks
the scheduler the same question with no OpenSandbox in the way:

  arm "kata"           bare pods: runtimeClassName kata-qemu (overhead 250m / 160Mi from the
                       RuntimeClass), one busybox container (pinned by digest) running sleep 3600,
                       requests = limits = 100m / 128Mi: the same 350m the sandboxes had
  arm "runc-overhead"  the same pods with a temporary RuntimeClass osb-runc-overhead (handler runc,
                       overhead 250m / 160Mi): overhead accounting without Kata

  1. preconditions (a quiet cluster, the server on the baseline); kubectl pinned to --context osb-eks;
     the namespace osb-kata-plain and the RuntimeClass osb-runc-overhead must not exist yet
  2. the namespace osb-kata-plain and the RuntimeClass osb-runc-overhead are created (labels
     example=opensandbox-stress, script=kata_plain)
  3. each arm in turn: pods 2 at a time (the next pair once both run), until one stays unscheduled for
     more than 20 s, or --cap pods exist (default 30). At a refusal: the pod's resource view, its
     events verbatim, every pod bound to each node with its effective request (kata_ceiling.py's
     accounting, at once and 10 s later), kubectl describe node. Then the arm's pods are deleted and
     waited for before the next arm
  4. per arm: the count per node at refusal, the per-pod CPU the scheduler must be counting (solved
     from the counts: base + n*X <= allocatable < base + (n+1)*X on every node), a verdict
  5. finally: the pods, the namespace and the RuntimeClass are deleted (only the ones this script
     created); the server is read back (this script never changes it: it stays on runc)

Output: this terminal and logs/probes/kata_plain.log, masked. Nothing goes to results/. The
accounting (effective requests, the node views, describe node) is imported from kata_ceiling.py in
this directory, so both probes count the same way.

If the run is interrupted before its finally, delete what is left by hand:
    kubectl --context osb-eks delete namespace osb-kata-plain --ignore-not-found
    kubectl --context osb-eks delete runtimeclass osb-runc-overhead --ignore-not-found
The other stress scripts' preconditions do not look in osb-kata-plain: up to 30 `sleep 3600` pods
(~10 CPU requested) left there would sit beside later runs unseen. Before any later run, check
that `kubectl --context osb-eks get namespace osb-kata-plain` finds nothing.

Each arm ends with one outcome: "early" (refused for CPU on every node, the implied per-pod CPU
above what the pods ask), "as-requested" (refused where the requests say), "capped" (no refusal up
to --cap, and the count bounds the per-pod CPU under the sandboxes' 567m), or "inconclusive" (any
other stop, a refusal that is not CPU on every node, or a cap too low to test). The combined reading
is given only when both arms are conclusive.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the stress directory: common, checks
sys.path.insert(1, str(Path(__file__).resolve().parent))

from checks import BASELINE, EXAMPLE, helm_problems, helm_state, scheduling_reasons, server_problems  # noqa: E402
from common import (  # noqa: E402
    KUBE_CONTEXT,
    LOGS,
    VMM_IMAGE,
    Failures,
    Plan,
    cli,
    describe,
    heading,
    helm_history,
    kube_json,
    kubectl,
    new_run_id,
    node_alias,
    node_names,
    preconditions,
    say,
    server_show,
    wait_until,
)
import kata_ceiling as kc  # noqa: E402  (the same accounting kata_ceiling.py uses)

SCRIPT = "kata_plain"
CONTEXT = "osb-eks"
NAMESPACE = "osb-kata-plain"
RUNC_CLASS = "osb-runc-overhead"
ARMS = (("kata", "kata-qemu"), ("runc-overhead", RUNC_CLASS))
BATCH = 2
UNSCHEDULED_S = 20
START_S = 120                     # a bound pod that is not Running by then stops the arm
POLL_S = 2
SECOND_SNAPSHOT_S = 10
POD_CPU_M = 100                   # the container's request; the overhead comes from the RuntimeClass
SANDBOX_RANGE = (567, 599)        # kata_ceiling.py: the per-pod CPU the OpenSandbox Kata sandboxes implied
ARM_DEADLINE_PER_POD_S = 60       # an arm that has not stopped after cap x this is stopped (inconclusive)
LOG_PATH = LOGS / "probes" / "kata_plain.log"
OWN_LABELS = {"example": EXAMPLE, "script": SCRIPT}


# --- manifests -------------------------------------------------------------------------------------

def namespace_manifest() -> dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": dict(OWN_LABELS)}}


def runtime_class_manifest() -> dict:
    return {"apiVersion": "node.k8s.io/v1", "kind": "RuntimeClass",
            "metadata": {"name": RUNC_CLASS, "labels": dict(OWN_LABELS)},
            "handler": "runc", "overhead": {"podFixed": {"cpu": "250m", "memory": "160Mi"}}}


def pod_manifest(name: str, arm: str, runtime_class: str, run_id: str) -> dict:
    """A bare pod: the sandbox's shape (100m / 128Mi, requests = limits) and nothing of OpenSandbox."""
    resources = {"cpu": f"{POD_CPU_M}m", "memory": "128Mi"}
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE,
                     "labels": {**OWN_LABELS, "run": run_id, "arm": arm}},
        "spec": {
            "runtimeClassName": runtime_class,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 1,
            "automountServiceAccountToken": False,
            # Kata's default disable_guest_seccomp = true: RuntimeDefault is not enforced inside the
            # guest (the pod still runs); the rest is applied by the kata-agent in the guest.
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "sleep", "image": VMM_IMAGE, "imagePullPolicy": "IfNotPresent",
                "command": ["sleep", "3600"],
                "resources": {"requests": dict(resources), "limits": dict(resources)},
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                                    "readOnlyRootFilesystem": True},
            }],
        },
    }


def apply(objects: list[dict]) -> None:
    kubectl("create", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}), timeout=90)


# --- one arm ---------------------------------------------------------------------------------------

def arm_pods(run_id: str, arm: str) -> list[dict]:
    return kube_json("-n", NAMESPACE, "get", "pods", "-l", f"run={run_id},arm={arm}", timeout=60).get("items", [])


def events_of(pod_name: str) -> list[str]:
    items = kube_json("-n", NAMESPACE, "get", "events", "--field-selector", f"involvedObject.name={pod_name}",
                      timeout=60).get("items", [])
    return [f"{e.get('lastTimestamp') or e.get('eventTime') or ''} {e.get('reason')} "
            f"x{e.get('count') or (e.get('series') or {}).get('count') or 1}: {e.get('message', '')}"
            for e in items if e.get("reason") in ("FailedScheduling", "Scheduled", "FailedCreatePodSandBox")]


def implied_cpu(views: dict, n_by_node: dict[str, int]) -> tuple[float | None, float | None, list[str]]:
    """The per-pod CPU X the scheduler must count for the arm's pods, from the counts at refusal: on
    each node where the next pod was refused, base + n*X <= allocatable < base + (n+1)*X, base being
    every other bound pod's spec request. Returns (low, high, lines): X in (low, high]."""
    low, high, lines = 0.0, float("inf"), []
    for node, view in views.items():
        n = n_by_node.get(node, 0)
        base = sum(r["cpu"] for r in view["rows"] if r["kind"] != "this run" and not r["terminal"] and not r["nominated"])
        free = view["alloc_cpu"] - base
        if n == 0:
            lines.append(f"{node_alias(node)}: no pod of the arm there: left out of the arithmetic")
            continue
        lo, hi = free / (n + 1), free / n
        lines.append(f"{node_alias(node)}: {n} pods of the arm, other pods {base:.0f}m, allocatable "
                     f"{view['alloc_cpu']:.0f}m -> X in ({lo:.0f}m, {hi:.0f}m]")
        low, high = max(low, lo), min(high, hi)
    if high == float("inf"):
        return None, None, lines + ["no node holds a pod of the arm: nothing to solve"]
    if low >= high:
        return None, None, lines + ["the nodes disagree: no single per-pod cost explains both counts"]
    return low, high, lines


def fill(arm: str, runtime_class: str, run_id: str, nodes: list[str], cap: int, failures: Failures) -> dict:
    heading(f"arm {arm}: bare pods, runtimeClassName {runtime_class}, {POD_CPU_M}m / 128Mi, {BATCH} at a time")
    created, stop, stuck = 0, "", None
    unscheduled_since: dict[str, float] = {}
    bound_at: dict[str, float] = {}
    latest: dict[str, dict] = {}
    batch_names: list[str] = []
    started, deadline = time.monotonic(), cap * ARM_DEADLINE_PER_POD_S
    while not stop:
        if time.monotonic() - started > deadline:
            stop = f"the arm's deadline ({deadline} s) passed: a created pod never showed up or never settled"
            break
        if not batch_names:
            if created >= cap:
                stop = f"the cap: {cap} pods, no refusal"
                break
            size = min(BATCH, cap - created)
            batch_names = [f"kp-{arm}-{created + i + 1:02d}" for i in range(size)]
            apply([pod_manifest(name, arm, runtime_class, run_id) for name in batch_names])
            created += size
        time.sleep(POLL_S)
        now = time.monotonic()
        for pod in arm_pods(run_id, arm):
            latest[pod["metadata"]["name"]] = pod
        for name in batch_names:
            pod = latest.get(name)
            if pod is None:
                continue
            if not pod["spec"].get("nodeName"):
                if now - unscheduled_since.setdefault(name, now) > UNSCHEDULED_S:
                    stuck, stop = pod, f"pod unscheduled for more than {UNSCHEDULED_S} s"
                continue
            bound_at.setdefault(name, now)
            phase = pod.get("status", {}).get("phase")
            if phase in ("Failed", "Succeeded") or (phase != "Running" and now - bound_at[name] > START_S):
                stop = f"pod {name} bound but {phase} after {now - bound_at[name]:.0f} s: not a scheduling refusal"
        if stop:
            break
        if all((latest.get(n) or {}).get("status", {}).get("phase") == "Running" for n in batch_names):
            per_node: dict[str, int] = {}
            for pod in latest.values():
                if pod.get("status", {}).get("phase") == "Running":
                    alias = node_alias(pod["spec"].get("nodeName", ""))
                    per_node[alias] = per_node.get(alias, 0) + 1
            say(f"{arm}: {created:2} running {dict(sorted(per_node.items()))}")
            batch_names = []
    say(f"{arm}: stopped: {stop}")
    result: dict = {"arm": arm, "runtime_class": runtime_class, "created": created, "stop": stop}

    views = kc.snapshot(run_id, nodes)
    bound = {node: sum(1 for r in v["rows"] if r["kind"] == "this run" and not r["terminal"]) for node, v in views.items()}
    result["per_node"] = {node_alias(n): c for n, c in bound.items()}
    need = None
    if stuck is not None:
        name = stuck["metadata"]["name"]
        fresh = kube_json("-n", NAMESPACE, "get", "pod", name, check=False, timeout=60) or stuck
        need = kc.effective(fresh)["cpu"]
        heading(f"arm {arm}: the unscheduled pod")
        print(kc.masked(json.dumps(kc.resource_view(fresh), indent=1)))
        e = kc.effective(fresh)
        print(f"  effective request: {e['cpu']:.0f}m CPU, {e['mem'] / kc.MIB:.0f} MiB"
              + (" (pod-level requests set)" if e["pod_level"] else "")
              + f"; with status resources {kc.effective(fresh, status=True)['cpu']:.0f}m")
        heading(f"arm {arm}: its events (verbatim)")
        lines = events_of(name)
        result["events"] = [kc.masked(line) for line in lines]
        for line in lines or ["(none found)"]:
            print(f"  {kc.masked(line)}")
        message = next((c.get("message", "") for c in fresh.get("status", {}).get("conditions") or []
                        if c.get("type") == "PodScheduled" and c.get("status") == "False"), "")
        if not message:
            message = next((line.split(": ", 1)[1] for line in reversed(lines) if " FailedScheduling " in line
                            and ": " in line), "")
        result["reasons"] = scheduling_reasons(message)
        print(f"  the refusal's reasons per node: {result['reasons'] or 'none found'}")
    verdicts = kc.print_snapshot(f"arm {arm}: every pod bound to each node, now", views, need)
    time.sleep(SECOND_SNAPSHOT_S)
    kc.print_snapshot(f"arm {arm}: every pod bound to each node, {SECOND_SNAPSHOT_S} s later",
                      kc.snapshot(run_id, nodes), need)
    heading(f"arm {arm}: kubectl describe node")
    for node in nodes:
        kc.describe_node(node)

    heading(f"arm {arm}: bound -> container started -> Ready (API timestamps, 1 s resolution)")
    rows = [kc.timings(p) for p in latest.values() if p["spec"].get("nodeName")]
    for key in ("created_to_bound", "bound_to_started", "bound_to_ready"):
        d = describe(key, [r[key] for r in rows if r[key] is not None])
        print(f"  {key:<17} n {d['n']:>2}" + (f"  min {d['min']:5.1f}  p50 {d['p50']:5.1f}  max {d['max']:5.1f} s"
                                            if d["n"] else ""))

    heading(f"arm {arm}: what the scheduler counts per pod")
    spec_cpu = POD_CPU_M + 250
    result["implied"], result["bound_max"] = None, None
    if stuck is None:
        ceilings = []
        for node, view in views.items():
            n = bound.get(node, 0)
            base = sum(r["cpu"] for r in view["rows"] if r["kind"] != "this run" and not r["terminal"] and not r["nominated"])
            if n:
                ceilings.append((view["alloc_cpu"] - base) / n)
        most = min(ceilings) if ceilings else None
        result["bound_max"] = round(most) if most is not None else None
        if not stop.startswith("the cap"):
            result["outcome"] = "inconclusive"
            result["verdict"] = f"inconclusive: the arm stopped without a scheduling refusal: {stop}"
        elif most is not None and most < SANDBOX_RANGE[0]:
            result["outcome"] = "capped"
            result["verdict"] = (f"no refusal up to {created} pods ({result['per_node']}): the scheduler counts at most "
                                 f"{most:.0f}m per pod, under the {SANDBOX_RANGE[0]}m the sandboxes implied"
                                 + (f"; consistent with the {spec_cpu}m the pods ask" if most >= spec_cpu else ""))
        else:
            result["outcome"] = "inconclusive"
            result["verdict"] = (f"inconclusive: no refusal, but {created} pods only bound the cost at "
                                 f"{'?' if most is None else f'{most:.0f}'}m per pod, not under {SANDBOX_RANGE[0]}m: "
                                 "raise --cap")
    elif (result.get("reasons") or {}) != {"cpu": len(nodes)}:
        result["outcome"] = "inconclusive"
        why = result.get("reasons") or "no reason found"
        result["verdict"] = f"inconclusive: refused, but not for CPU on every node ({why}): the CPU arithmetic does not apply"
    else:
        low, high, lines = implied_cpu(views, bound)
        for line in lines:
            print(f"  {line}")
        result["implied"] = [round(low), round(high)] if low is not None else None
        if low is None:
            result["outcome"] = "inconclusive"
            result["verdict"] = "inconclusive: the counts at refusal fit no single per-pod cost: read the node views"
        elif low > spec_cpu + 5:
            same = low < SANDBOX_RANGE[1] and high > SANDBOX_RANGE[0]
            result["outcome"] = "early"
            result["verdict"] = (f"refused early: the scheduler counts {low:.0f}-{high:.0f}m per pod, "
                                 f"{low - spec_cpu:.0f}-{high - spec_cpu:.0f}m more than the {spec_cpu:.0f}m it asks "
                                 f"({need:.0f}m by kata_ceiling's accounting); "
                                 + ("the same range as the sandboxes' 567-599m" if same
                                    else "not the sandboxes' 567-599m"))
        else:
            result["outcome"] = "as-requested"
            result["verdict"] = (f"refused where the requests say: the scheduler counts {low:.0f}-{high:.0f}m per pod, "
                                 f"which includes the {spec_cpu:.0f}m it asks")
        if verdicts:
            print(f"  kata_ceiling's reading of the same views: {kc.verdict(verdicts, need)}")
    print(f"  verdict ({arm}, {result['outcome']}): {result['verdict']}")
    return result


def combined(by_arm: dict[str, dict]) -> str:
    """The two arms together; only when both are conclusive."""
    kata, runc = by_arm.get("kata"), by_arm.get("runc-overhead")
    if not kata or not runc:
        return "inconclusive: an arm did not run"
    open_arms = [f"{a['arm']}: {a['verdict']}" for a in (kata, runc) if a.get("outcome", "inconclusive") == "inconclusive"]
    if open_arms:
        return "inconclusive: " + " | ".join(open_arms)
    kata_early, runc_early = kata["outcome"] == "early", runc["outcome"] == "early"
    if kata_early and runc_early:
        return "both arms are refused early: the scheduler's overhead accounting itself counts more than spec.overhead"
    if kata_early:
        return ("bare Kata pods are refused early and runc pods with the same overhead are not: the extra is "
                "Kata-specific (Kubernetes with the kata-qemu RuntimeClass on these nodes), not OpenSandbox's")
    if runc_early:
        return "only the runc-overhead arm is refused early: read the node views"
    return ("neither arm is refused early: bare pods fill as their requests say, so the early refusal is specific to "
            "OpenSandbox's sandboxes")


def delete_arm(run_id: str, arm: str) -> bool:
    kubectl("-n", NAMESPACE, "delete", "pods", "-l", f"run={run_id},arm={arm}", "--ignore-not-found", "--wait=false",
            check=False, timeout=90)
    return wait_until(lambda: not arm_pods(run_id, arm), timeout=300, every=3)


# --- main ------------------------------------------------------------------------------------------

def arguments(parser) -> None:
    parser.add_argument("--cap", type=int, default=30, help="pods per arm at most (default 30)")


def plan(cap: int) -> Plan:
    p = Plan(f"kata_plain.py: the Kata ceiling without OpenSandbox (2 arms, {cap} pods each at most)")
    p.checks += [f"kubectl context {CONTEXT}",
                 "preconditions: no sandboxes, no Pools, none of this directory's objects; server on the baseline (runc); "
                 "2 nodes Ready",
                 f"namespace {NAMESPACE} and RuntimeClass {RUNC_CLASS} do not exist yet"]
    p.step(f"create namespace {NAMESPACE} and RuntimeClass {RUNC_CLASS} (handler runc, overhead 250m / 160Mi)", 0.1)
    p.step(f"arm kata: bare kata-qemu pods ({POD_CPU_M}m / 128Mi, busybox by digest, sleep 3600), {BATCH} at a time, "
           f"until one is unscheduled for > {UNSCHEDULED_S} s or {cap} exist; the dump at the refusal; the pods "
           "deleted and waited for", 2.5)
    p.step(f"arm runc-overhead: the same pods with RuntimeClass {RUNC_CLASS}; the same dump; deleted", 2.0)
    p.step("per arm: counts per node at refusal, the implied per-pod CPU, a verdict", 0.1)
    p.step(f"finally: the pods, namespace {NAMESPACE} and RuntimeClass {RUNC_CLASS} deleted; the server read back",
           1.0)
    p.restores.append("nothing on the server (never changed: it stays on runc); the objects above are deleted")
    p.writes.append("logs/probes/kata_plain.log (masked); nothing in results/")
    return p


def main() -> int:
    args = cli(__doc__, arguments)
    if KUBE_CONTEXT != CONTEXT:
        sys.exit(f"the kubectl context is {KUBE_CONTEXT!r}, not {CONTEXT}: refusing")
    if not 4 <= args.cap <= 60:
        sys.exit("--cap must be within 4..60")
    the_plan = plan(args.cap)
    if args.dry_run:
        the_plan.print()
        return 0
    kc.LOG_PATH = LOG_PATH                      # kata_ceiling's tee and hang-up handler write this log
    kc.tee_to_log()
    signal.signal(signal.SIGHUP, kc._on_hangup)
    the_plan.print()
    failures = Failures()
    preconditions()
    nodes = node_names()
    kc._node_map.update({name: node_alias(name) for name in nodes})
    leftovers = []
    if kube_json("get", "namespace", NAMESPACE, check=False):
        leftovers.append(f"namespace {NAMESPACE}")
    if kube_json("get", "runtimeclass", RUNC_CLASS, check=False):
        leftovers.append(f"RuntimeClass {RUNC_CLASS}")
    if leftovers:
        sys.exit(f"{' and '.join(leftovers)} already exist (not made by this run): delete them first "
                 "(the commands are in this script's docstring); nothing was created")
    run_id = new_run_id()
    made: list[str] = []
    results: list[dict] = []
    try:
        heading(f"setup: namespace {NAMESPACE}, RuntimeClass {RUNC_CLASS}")
        apply([namespace_manifest()])
        made.append("namespace")
        apply([runtime_class_manifest()])
        made.append("runtimeclass")
        rc = kube_json("get", "runtimeclass", RUNC_CLASS)
        print(f"  {RUNC_CLASS}: handler {rc.get('handler')!r}, overhead {json.dumps(rc.get('overhead'))}")
        kata = kube_json("get", "runtimeclass", "kata-qemu", check=False)
        print(f"  kata-qemu: handler {kata.get('handler')!r}, overhead {json.dumps(kata.get('overhead'))}, "
              f"scheduling {json.dumps(kata.get('scheduling'))}")
        for i, (arm, runtime_class) in enumerate(ARMS):
            clean = False
            try:
                results.append(fill(arm, runtime_class, run_id, nodes, args.cap, failures))
            except Exception as error:  # noqa: BLE001 - reported; the next arm still runs on clean nodes
                failures.add(f"arm {arm}: {type(error).__name__}: {kc.masked(str(error))[:300]}")
                results.append({"arm": arm, "created": 0, "per_node": {}, "implied": None, "outcome": "inconclusive",
                                "verdict": f"inconclusive: the arm failed: {type(error).__name__}"})
            finally:
                say(f"{arm}: deleting its pods")
                clean = delete_arm(run_id, arm)
                failures.check(clean, f"arm {arm}: its pods are gone")
            if not clean:
                # kata_ceiling's accounting tells pods apart by run, not by arm: leftovers would count.
                for later, _ in ARMS[i + 1:]:
                    results.append({"arm": later, "created": 0, "per_node": {}, "implied": None,
                                    "outcome": "inconclusive",
                                    "verdict": f"skipped: the {arm} arm's pods were still there after 300 s"})
                break

        heading("verdicts")
        for r in results:
            implied = f"{r['implied'][0]}-{r['implied'][1]}m" if r.get("implied") else (
                f"at most {r['bound_max']}m" if r.get("bound_max") else "-")
            print(f"  {r['arm']:<14} {r.get('outcome', 'inconclusive'):<13} {r['created']:2} pods created, per node at the "
                  f"stop {r['per_node']}; per-pod CPU the scheduler counts: {implied}; {r['verdict']}")
        print(f"  reading: {combined({r['arm']: r for r in results})}")
    except Exception as error:  # noqa: BLE001 - reported; the clean-up below still runs
        failures.add(f"{type(error).__name__}: {kc.masked(str(error))[:300]}")
    finally:
        heading("clean up")
        if "namespace" in made:
            kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true", "--timeout=300s",
                    check=False, timeout=330)
            failures.check(not kube_json("get", "namespace", NAMESPACE, check=False),
                           f"namespace {NAMESPACE} and its pods are gone")
        if "runtimeclass" in made:
            kubectl("delete", "runtimeclass", RUNC_CLASS, "--ignore-not-found", check=False, timeout=60)
            failures.check(not kube_json("get", "runtimeclass", RUNC_CLASS, check=False),
                           f"RuntimeClass {RUNC_CLASS} is gone")
        try:
            state = server_show()
            problems = server_problems(state, BASELINE) + helm_problems(helm_state(helm_history()))
            failures.check(not problems, f"server read back (not changed): {json.dumps(BASELINE)}"
                           + (f"; {'; '.join(problems)}" if problems else ""))
        except Exception as error:  # noqa: BLE001
            failures.add(f"server read-back: {error}")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
