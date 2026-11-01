"""First sandboxes on EKS: how long a create takes cold (the workload image not yet on the
node) and warm, which Kubernetes objects one sandbox becomes, and a quick use test.

Gate 2: a sandbox that stays in Provisioning for 3 minutes usually means the workload Pod
can't reach its supervisor through the supervisor's Service (the NetworkPolicy path). The
script then prints the evidence and exits 2.

    uv run python 07_first_sandbox.py           # cold create only if the image isn't cached yet
    uv run python 07_first_sandbox.py --cold    # remove the workload image from both nodes first
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import (CONTEXT, GATEWAY, NAMESPACE, WORKSPACE, client, delete_all_sandboxes,
                    kubectl_json, objects_for, phase_name, require_gateway)

GATE_SECONDS = 180
WORKLOAD_REPO = "/openshell-eks/workload"   # matched against the node's image list


def sh(*args: str, check: bool = True) -> str:
    return subprocess.run(list(args), check=check, capture_output=True, text=True).stdout


def kubectl(*args: str, check: bool = True) -> str:
    return sh("kubectl", "--context", CONTEXT, *args, check=check)


def evict_workload_image() -> None:
    """Remove the workload image from both nodes with containerd's ctr (the EKS AL2023 AMI has
    no crictl), through one debug pod per node, and wait for them to finish. --sync waits for
    containerd's garbage collection, so the layers go too and the next start is a real pull."""
    for node in kubectl_json("get", "nodes")["items"]:
        kubectl("debug", "-n", "default", f"node/{node['metadata']['name']}", "--quiet", "--profile=sysadmin",
                "--image=public.ecr.aws/amazonlinux/amazonlinux:2023", "--",
                "chroot", "/host", "sh", "-c",
                "ctr -n k8s.io images ls -q | grep 'openshell-eks/workload' | xargs -r ctr -n k8s.io images rm --sync",
                check=False)
    debuggers: list = []
    for _ in range(60):
        debuggers = [p for p in kubectl_json("-n", "default", "get", "pods")["items"]
                     if p["metadata"]["name"].startswith("node-debugger-")]
        if debuggers and all(p["status"].get("phase") in ("Succeeded", "Failed") for p in debuggers):
            break
        time.sleep(2)
    for p in debuggers:
        kubectl("-n", "default", "delete", "pod", p["metadata"]["name"], "--wait=false", check=False)


def pull_event(sandbox_name: str) -> str:
    """How the workload image got onto the node, from the workload Pod's Pulled event."""
    # Events outlive their Pod and every run reuses the Pod name, so match this Pod's UID.
    uid = kubectl("-n", NAMESPACE, "get", "pod", f"{WORKSPACE}--{sandbox_name}", "-o", "jsonpath={.metadata.uid}")
    events = kubectl_json("-n", NAMESPACE, "get", "events",
                          "--field-selector", f"involvedObject.uid={uid},reason=Pulled")["items"]
    msgs = [e.get("message", "") for e in events]
    pulled = [m for m in msgs if m.startswith("Successfully pulled")]
    return (pulled or msgs or ["no Pulled event"])[0]


def create_and_time(c, name: str) -> tuple[float, str]:
    """Create a sandbox from the chart's default workload image; return (seconds to Ready, id)."""
    t0 = time.monotonic()
    ref = c.create(workspace=WORKSPACE, name=name)
    last = None
    while True:
        status = c.get(name, workspace=WORKSPACE).status
        phase = phase_name(status.phase)
        if phase != last:
            print(f"    +{time.monotonic() - t0:5.1f}s  {phase}")
            last = phase
        if phase == "Ready":
            return time.monotonic() - t0, ref.id
        if phase == "Error" or time.monotonic() - t0 > GATE_SECONDS:
            gate_failed(name, ref.id, phase, status)
        time.sleep(0.5)


def gate_failed(name: str, sandbox_id: str, phase: str, status) -> None:
    print(f"\nsandbox {name} is {phase} after {GATE_SECONDS} s; conditions:", file=sys.stderr)
    for cond in getattr(status, "conditions", ()):
        print(f"  {cond.type}={cond.status} {cond.reason}: {cond.message}", file=sys.stderr)
    print("\n--- policyendpoints", file=sys.stderr)
    print(kubectl("-n", NAMESPACE, "get", "policyendpoints", "-o", "wide", check=False), file=sys.stderr)
    print("--- supervisor log (tail)", file=sys.stderr)
    print(kubectl("-n", NAMESPACE, "logs", "-l",
                  f"openshell.ai/sandbox-id={sandbox_id},openshell.ai/component=supervisor",
                  "--tail=30", check=False), file=sys.stderr)
    print("--- pods and events", file=sys.stderr)
    print(kubectl("-n", NAMESPACE, "get", "pods", "-l", f"openshell.ai/sandbox-id={sandbox_id}", "-o", "wide",
                  check=False), file=sys.stderr)
    print(kubectl("-n", NAMESPACE, "get", "events", "--sort-by=.lastTimestamp", check=False)[-3000:], file=sys.stderr)
    print("gate 2 failed: control path not admitted", file=sys.stderr)
    sys.exit(2)


def cli(*args: str) -> str:
    return sh("openshell", *args, "-g", GATEWAY)


def main() -> None:
    cold = "--cold" in sys.argv
    require_gateway()
    with client() as c:
        if n := delete_all_sandboxes(c):
            print(f"deleted {n} leftover sandbox(es)")

        if cold:
            print("removing the workload image from both nodes")
            evict_workload_image()
        rows: list[tuple[str, float]] = []

        print("\n== create 1")
        secs, first_id = create_and_time(c, "first")
        pulled = pull_event("first")
        print(f"    image: {pulled}")
        rows.append(("first create, " + ("image cached" if "already present" in pulled else "image pulled"), secs))

        print("\n== what one sandbox is in Kubernetes")
        for kind, names in objects_for(first_id).items():
            print(f"  {kind:28} {len(names)}  {', '.join(names)}")
        print(kubectl("-n", NAMESPACE, "get", "pods", "-l", f"openshell.ai/sandbox-id={first_id}",
                      "-o", "custom-columns=POD:.metadata.name,NODE:.spec.nodeName,"
                      "COMPONENT:.metadata.labels.openshell\\.ai/component,STATUS:.status.phase"))

        print("== use it")
        for cmd in (["id"], ["uname", "-r"], ["sh", "-c", "grep -E '^(Seccomp|NoNewPrivs|CapEff)' /proc/self/status"],
                    ["sh", "-c", "env | grep -ciE 'proxy|token' || true"]):
            r = c.exec("first", cmd, workspace=WORKSPACE, no_login_shell=True)
            print(f"  $ {' '.join(cmd)}\n    " + (r.stdout.strip() or r.stderr.strip()).replace("\n", "\n    "))
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "hello.txt"
            src.write_text("hello from the laptop\n" * 1000)
            cli("sandbox", "upload", "first", str(src), "/sandbox/", "--no-git-ignore")
            out = Path(tmp) / "back"
            out.mkdir()
            cli("sandbox", "download", "first", "/sandbox/hello.txt", str(out) + "/")
            same = (out / "hello.txt").read_bytes() == src.read_bytes()
            print(f"  upload and download round trip: {'ok' if same else 'MISMATCH'}")
        print("  last log lines:")
        for line in cli("logs", "first", "-n", "4").strip().splitlines():
            print("    " + line[:160])

        print("\n== warm creates")
        secs, _ = create_and_time(c, "warm-1")
        rows.append(("warm create", secs))
        five = []
        for i in range(2, 7):
            secs, _ = create_and_time(c, f"warm-{i}")
            five.append(secs)
        rows.append(("five more warm creates: median", statistics.median(five)))
        rows.append(("five more warm creates: fastest", min(five)))
        rows.append(("five more warm creates: slowest", max(five)))

        print("\n== timings (create call to Ready)")
        for label, secs in rows:
            print(f"  {label:40} {secs:6.1f} s")

        t0 = time.monotonic()
        n = delete_all_sandboxes(c)
        print(f"\ndeleted {n} sandbox(es) in {time.monotonic() - t0:.1f} s")
        left = kubectl_json("-n", NAMESPACE, "get", "pvc", "-l", "openshell.ai/managed-by=openshell")["items"]
        print(f"workspace PVCs left: {len(left)}")


if __name__ == "__main__":
    main()
