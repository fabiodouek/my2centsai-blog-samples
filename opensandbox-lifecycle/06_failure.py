"""When things go wrong: a sandbox pod deleted under the sandbox, and a node taken out of scheduling.

1. Delete a sandbox's pod with kubectl, the way a node failure, an eviction or an operator would
   remove it, and watch what the controller, the server and the SDK report for about a minute.
   Nothing here asserts a recovery: the script prints what happens.
2. Cordon one node (kubectl cordon: no new pods; the pods already there keep running) and create
   sandboxes; exit 1 if one lands on the cordoned node. Skipped when a node is already cordoned or
   not Ready (the script would undo someone else's cordon at the end); the last line then says
   SKIPPED, and the exit code stays 0. Our BatchSandbox template (part 2) has no tolerations, so
   they all land on the other node. Then a plain pod with the toleration the release's default
   template carries (operator: Exists, which tolerates every taint) is pinned to the cordoned node,
   to show that the default would have put sandboxes there.
   kubectl drain would also evict the pods on the node, which is case 1 for each sandbox on it;
   the script does not drain.

The node is uncordoned at the end, also when the script fails or is interrupted (and
99_cleanup.py uncordons a node this script marked, if the script was killed outright). The
script deletes only its own sandboxes and its own pod.
"""

import json
import signal
import sys
import time
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.models.sandboxes import SandboxFilter
from opensandbox.sync import SandboxManagerSync

from common import (
    EXAMPLE,
    SANDBOX_NAMESPACE,
    connect_server,
    connection,
    delete_all_sandboxes,
    ensure_port_forward,
    heading,
    image,
    kubectl,
    own_metadata,
    utc_now,
)

SCRIPT = "06_failure"
WATCH_SECONDS = 75
SANDBOXES_ON_CORDON = 4
CORDON_MARK = "opensandbox-lifecycle/cordoned-by"   # node annotation, so 99_cleanup.py can undo it
DEMO_POD = "toleration-demo"
meta = own_metadata(SCRIPT)
cordoned: str | None = None


def uncordon() -> None:
    global cordoned
    if cordoned:
        kubectl("uncordon", cordoned, check=False)
        kubectl("annotate", "node", cordoned, f"{CORDON_MARK}-", check=False)
        print(f"  uncordoned {cordoned}")
        cordoned = None


def on_signal(signum, _frame) -> None:
    # Unwinds through main()'s finally, which uncordons and deletes this run's sandboxes and pod.
    raise SystemExit(128 + signum)


def pods_of(sandbox_id: str) -> list[dict]:
    """Pods with the sandbox's id label (the server labels every sandbox pod opensandbox.io/id)."""
    out = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"opensandbox.io/id={sandbox_id}", "-o", "json",
                  check=False)
    if not out:
        return []
    return [{"name": p["metadata"]["name"], "uid": p["metadata"]["uid"][:8],
             "phase": "Terminating" if p["metadata"].get("deletionTimestamp") else p["status"].get("phase", "?")}
            for p in json.loads(out)["items"]]


def batchsandbox(sandbox_id: str) -> str:
    out = kubectl("-n", SANDBOX_NAMESPACE, "get", "batchsandboxes.sandbox.opensandbox.io", sandbox_id, "-o", "json", check=False)
    if not out:
        return "gone"
    status = json.loads(out).get("status", {})
    return f"phase {status.get('phase', '?')}, replicas {status.get('replicas', 0)}, ready {status.get('ready', 0)}"


def server_state(manager: SandboxManagerSync, sandbox_id: str) -> str:
    try:
        info = manager.get_sandbox_info(sandbox_id)
        return f"{info.status.state}" + (f" ({info.status.reason})" if info.status.reason else "")
    except Exception as error:  # 404 once it is gone
        return f"error {type(error).__name__}: {str(error)[:80]}"


def deleted_pod(manager: SandboxManagerSync) -> None:
    heading("1. delete the pod of a running sandbox")
    sb = SandboxSync.create(image(), timeout=timedelta(minutes=10), ready_timeout=timedelta(seconds=180),
                            metadata=meta, connection_config=connection("proxy"))
    sb.commands.run("echo before > /root/marker.txt")
    first = pods_of(sb.id)
    sid = sb.id[:8]
    print(f"  sandbox {sb.id}: pod {first[0]['name']} (uid {first[0]['uid']}), marker file written")
    print(f"  [{utc_now()}] kubectl delete pod {sid}...-0   (polling every 2 s)")
    kubectl("-n", SANDBOX_NAMESPACE, "delete", "pod", first[0]["name"], "--wait=false")
    last = ""
    started = time.monotonic()
    while time.monotonic() - started < WATCH_SECONDS:
        ensure_port_forward()
        pods = ", ".join(f"{p['name'].replace(sb.id, sid + '...')} {p['phase']} uid {p['uid']}"
                         for p in pods_of(sb.id)) or "no pod"
        line = f"server {server_state(manager, sb.id)} | BatchSandbox {batchsandbox(sb.id)} | {pods}"
        if line != last:
            print(f"  [{utc_now()}] +{time.monotonic() - started:4.0f}s  {line}", flush=True)
            last = line
        time.sleep(2)
    now = pods_of(sb.id)
    replaced = [p for p in now if p["uid"] != first[0]["uid"]]
    print(f"  after {WATCH_SECONDS} s: " + (f"a new pod {replaced[0]['name'].replace(sb.id, sid + '...')} "
                                            f"(uid {replaced[0]['uid']})" if replaced else "no new pod was created"))
    try:
        execution = sb.commands.run("cat /root/marker.txt 2>/dev/null || echo 'no /root/marker.txt'")
        out = "".join(m.text for m in execution.logs.stdout).strip() or "(nothing)"
        print(f"  a command through the SDK now: {out}")
    except Exception as error:
        print(f"  a command through the SDK now fails: {type(error).__name__}: {str(error)[:120]}")
    sb.kill()
    sb.close()


def node_of(sandbox_id: str) -> str:
    return kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", f"{sandbox_id}-0", "-o", "jsonpath={.spec.nodeName}",
                   check=False).strip()


def schedulable_ready_nodes() -> tuple[list[str], list[str]]:
    """(Ready and schedulable nodes, the others)."""
    items = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    good, bad = [], []
    for node in items:
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", []))
        (good if ready and not node["spec"].get("unschedulable") else bad).append(node["metadata"]["name"])
    return sorted(good), sorted(bad)


def cordon_test() -> bool | str:
    """True when the check passed, False when a sandbox landed on the cordoned node, "skipped" when not run."""
    global cordoned
    heading("2. cordon a node, then create sandboxes")
    nodes, others = schedulable_ready_nodes()
    if others or len(nodes) < 2:
        # Someone else's cordon or a failing node: this script would uncordon it at the end, so it stays out.
        print(f"  skipped: needs two Ready, schedulable nodes and found {len(nodes)} "
              f"({', '.join(others) or 'none'} not Ready or already cordoned)")
        return "skipped"
    server_node = kubectl("-n", "opensandbox-system", "get", "pods", "-l", "app.kubernetes.io/instance=opensandbox-server",
                          "-o", "jsonpath={.items[0].spec.nodeName}", check=False).strip()
    target = next((n for n in nodes if n != server_node), nodes[0])
    other = next(n for n in nodes if n != target)
    kubectl("cordon", target)
    cordoned = target
    kubectl("annotate", "node", target, "--overwrite", f"{CORDON_MARK}={meta['run']}")
    taints = json.loads(kubectl("get", "node", target, "-o", "jsonpath={.spec.taints}") or "[]")
    print(f"  [{utc_now()}] kubectl cordon {target}")
    print("  " + kubectl("get", "nodes", "--no-headers", "-o", "custom-columns=NAME:.metadata.name,"
                         "SCHEDULING:.spec.unschedulable").strip().replace("\n", "\n  ").replace("<none>", "schedulable")
          .replace("true", "unschedulable (cordoned)"))
    print("  taints on it: " + ", ".join(f"{t['key']}:{t['effect']}" for t in taints))
    placed: dict[str, int] = {}
    for n in range(1, SANDBOXES_ON_CORDON + 1):
        started = time.monotonic()
        sb = SandboxSync.create(image(), timeout=timedelta(minutes=10), ready_timeout=timedelta(seconds=180),
                                metadata=meta, connection_config=connection("proxy"))
        node = node_of(sb.id)
        placed[node] = placed.get(node, 0) + 1
        print(f"  sandbox {n}: {time.monotonic() - started:4.1f} s, on {node}")
        sb.close()
    ok = placed.get(target, 0) == 0
    print(f"  {'ok  ' if ok else 'FAIL'}  {placed.get(other, 0)} of {SANDBOXES_ON_CORDON} on {other}, "
          f"{placed.get(target, 0)} on the cordoned node")

    heading("   the release's default toleration, on a plain pod pinned to the cordoned node")
    manifest = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": DEMO_POD, "namespace": SANDBOX_NAMESPACE,
                     "labels": {"example": EXAMPLE, "script": SCRIPT, "run": meta["run"]}},
        "spec": {
            "nodeSelector": {"kubernetes.io/hostname": target},
            # What server/opensandbox_server/examples/example.batchsandbox-template.yaml (1.2.0) adds
            # to every sandbox pod; part 2's template removes it.
            "tolerations": [{"operator": "Exists"}],
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 0,
            "containers": [{"name": "sleep", "image": image(), "command": ["sleep", "300"],
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}}}],
        },
    }
    kubectl("apply", "-f", "-", stdin=json.dumps(manifest))
    phase = ""
    for _ in range(30):
        phase = kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", DEMO_POD, "-o", "jsonpath={.status.phase}",
                        check=False).strip()
        if phase in ("Running", "Failed", "Succeeded"):
            break
        time.sleep(2)
    print(f"  pod {DEMO_POD} with tolerations [{{operator: Exists}}]: {phase or 'no status'} on "
          f"{kubectl('-n', SANDBOX_NAMESPACE, 'get', 'pod', DEMO_POD, '-o', 'jsonpath={.spec.nodeName}', check=False) or '-'}")
    kubectl("-n", SANDBOX_NAMESPACE, "delete", "pod", DEMO_POD, "--wait=false", check=False)
    return ok


def main() -> int:
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    connect_server()
    print(f"sandboxes tagged run={meta['run']}")
    failures = 0
    skipped = False
    with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
        try:
            for step in (lambda: deleted_pod(manager), cordon_test):
                try:
                    result = step()
                    if result is False:
                        failures += 1
                    skipped = skipped or result == "skipped"
                except Exception as error:  # report one line, go on with the next step and the clean up
                    print(f"  FAIL  {type(error).__name__}: {str(error)[:200]}")
                    failures += 1
                finally:
                    uncordon()
        finally:  # also on Ctrl-C or SIGTERM (on_signal raises SystemExit)
            uncordon()
            heading("clean up")
            kubectl("-n", SANDBOX_NAMESPACE, "delete", "pod", DEMO_POD, "--ignore-not-found", "--wait=false", check=False)
            delete_all_sandboxes(metadata={"run": meta["run"]})
            left = manager.list_sandbox_infos(SandboxFilter(metadata={"run": meta["run"]})).sandbox_infos
            print(f"  sandboxes of this run left: {len(left)}")
    rows = kubectl("get", "nodes", "--no-headers", "-o", "custom-columns=NAME:.metadata.name,"
                   "READY:.status.conditions[?(@.type=='Ready')].status,UNSCHEDULABLE:.spec.unschedulable").split("\n")
    rows = [r.split() for r in rows if r.strip()]
    for name, ready, unschedulable in rows:
        state = "Ready" if ready == "True" else "NotReady"
        print(f"  {name}: {state}, {'schedulable' if unschedulable == '<none>' else 'CORDONED'}")
    if any(ready != "True" or unschedulable != "<none>" for _, ready, unschedulable in rows):
        print("  FAIL  a node is not Ready and schedulable")
        failures += 1
    if skipped:  # never let a skipped cordon test read as a passed one (the exit code stays 0)
        print("\n  SKIPPED (cordon test): it did not run, see step 2; only the deleted-pod case ran")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
