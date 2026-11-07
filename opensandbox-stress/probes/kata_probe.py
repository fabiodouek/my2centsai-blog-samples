"""What the scheduler is asked for one Kata sandbox of 02_density.py's shape.

Run from the stress directory, on a quiet cluster (no sandboxes; the server on the baseline):
    uv run python probes/kata_probe.py [--dry-run]

1. server_config.py --runtime kata-qemu (inside server_session: --baseline in the finally)
2. one sandbox, the density shape (100m / 128Mi) and labels (example, script=02_density, run, mode=kata)
3. its pod: runtimeClassName, spec.overhead, every container's and init container's requests and limits
4. `kubectl describe node` "Allocated resources" of both nodes (node-a / node-b)
5. delete it; the server back to the baseline with Helm settled
Prints the effective per-pod request the scheduler uses: max(sum of containers, largest init
container) + overhead, and the arithmetic for the density ceiling. Output goes to the terminal only
(masked: nodes as node-a and node-b, no IP); nothing is written to results/. probes/kata_ceiling.py
takes it further: what the scheduler counts when it refuses.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the stress directory: common, checks

from checks import cpu_milli, mem_bytes, pod_requests  # noqa: E402
from common import (  # noqa: E402
    SANDBOX_NAMESPACE,
    Plan,
    cli,
    create_sandbox,
    delete_run,
    heading,
    kube_json,
    kubectl,
    new_run_id,
    node_alias,
    node_names,
    own_metadata,
    preconditions,
    server_config,
    server_session,
    show,
    wait_no_pods,
)


def allocated(node: str) -> str:
    text = kubectl("describe", "node", node)
    start = text.find("Allocated resources:")
    end = text.find("Events:", start)
    return show(text[start:end if end > 0 else None].rstrip()).replace(node, node_alias(node))


def main() -> int:
    args = cli(__doc__)
    plan = Plan("probes/kata_probe.py: one Kata sandbox of the density shape, and what it asks the scheduler for")
    plan.checks.append("no sandboxes and no Pools; server runc + base egress + dns+nft + gateway; 2 nodes Ready")
    plan.server_changes.append("server_config.py --runtime kata-qemu (inside server_session)")
    plan.step("server_config.py --runtime kata-qemu (the server pod is replaced)", 0.6)
    plan.step("both nodes' Allocated resources (kubectl describe node), before", 0.1)
    plan.step("one sandbox, 100m / 128Mi, no policy; its pod's runtimeClassName, overhead and every container's "
              "requests and limits; the Allocated resources again and the arithmetic", 0.3)
    plan.step("delete it and wait for its pod to go", 0.3)
    plan.restores.append("server_config.py --baseline (server_session's finally), checked with --show --json")
    plan.writes.append("nothing: the terminal only")
    plan.print()
    if args.dry_run:
        return 0
    preconditions()
    run_id = new_run_id()
    meta = {**own_metadata("02_density", run_id), "mode": "kata"}
    with server_session():
        server_config("--runtime", "kata-qemu")
        heading("before: Allocated resources")
        for node in node_names():
            print(f"--- {node_alias(node)}\n{allocated(node)}")
        try:
            sb = create_sandbox(meta, resource={"cpu": "100m", "memory": "128Mi"}, ttl_minutes=20, ready_seconds=120,
                                retry=False)
            sb.close()
            pods = kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"run={run_id}")["items"]
            pod = pods[0]
            spec = pod["spec"]
            heading("the Kata sandbox's pod")
            print(f"  runtimeClassName: {spec.get('runtimeClassName')}")
            print(f"  overhead: {json.dumps(spec.get('overhead'))}")
            print(f"  node: {node_alias(spec.get('nodeName', ''))}")
            for kind in ("initContainers", "containers"):
                for c in spec.get(kind, []):
                    print(f"  {kind[:-1]:<14} {c['name']:<24} requests {json.dumps((c.get('resources') or {}).get('requests'))}"
                          f"  limits {json.dumps((c.get('resources') or {}).get('limits'))}")
            cpu, mem = pod_requests(pod)
            main_cpu = sum(cpu_milli(((c.get("resources") or {}).get("requests") or {}).get("cpu"))
                           for c in spec.get("containers", []))
            init_cpu = max([cpu_milli(((c.get("resources") or {}).get("requests") or {}).get("cpu"))
                            for c in spec.get("initContainers", [])] or [0])
            over_cpu = cpu_milli((spec.get("overhead") or {}).get("cpu"))
            print(f"  effective CPU request = max(containers {main_cpu:.0f}m, largest init {init_cpu:.0f}m) "
                  f"+ overhead {over_cpu:.0f}m = {cpu:.0f}m;  memory {mem / 2**20:.0f} MiB")
            egress = [c["name"] for c in spec.get("containers", []) if "egress" in c["name"]]
            print(f"  egress sidecar: {'yes: ' + ', '.join(egress) if egress else 'no'}")
            heading("with the sandbox: Allocated resources")
            for node in node_names():
                print(f"--- {node_alias(node)}\n{allocated(node)}")
            nodes = kube_json("get", "nodes")["items"]
            allocs = {node_alias(n["metadata"]["name"]): cpu_milli(n["status"]["allocatable"]["cpu"]) for n in nodes}
            print(f"  allocatable CPU: {allocs}")
            raw = {node_alias(n): allocated(n) for n in node_names()}
            for alias, text in raw.items():
                match = re.search(r"cpu\s+(\d+m?)\s+\((\d+)%\)", text)
                if match:
                    used = cpu_milli(match.group(1))
                    free = allocs[alias] - used
                    print(f"  {alias}: requested {used:.0f}m incl. this sandbox, free {free:.0f}m -> "
                          f"{int(free // cpu) if cpu else '?'} more of these pods fit")
            print(f"  memory request per pod incl. overhead: {mem_bytes('128Mi') / 2**20:.0f} MiB + overhead "
                  f"{mem_bytes((spec.get('overhead') or {}).get('memory')) / 2**20:.0f} MiB")
        finally:
            delete_run(run_id)
            wait_no_pods(f"run={run_id}", timeout=300)
    return 0


if __name__ == "__main__":
    sys.exit(main())
