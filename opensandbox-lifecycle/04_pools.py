"""Pools: warm pods waiting for a sandbox, against a cold create; what happens to a used pod; and
what a pooled create cannot do.

1. Apply manifests/pool.yaml (Pool osb-pool: bufferMin 2, bufferMax 3, poolMin 0, poolMax 5,
   recycleStrategy Delete) and time how long the controller takes to fill the buffer.
2. ROUNDS rounds of one cold create (no pool: the server makes a new pod) and one warm create
   (extensions={"poolRef": "osb-pool"}: a Ready pod is taken from the buffer). Both ask for the
   same image and resources. Each sandbox runs a command, then is deleted.
3. Recycle: which pod a warm sandbox got, and that after the sandbox is deleted that pod goes
   (recycleStrategy Delete) and a new one refills the buffer.
4. A create with both poolRef and a networkPolicy: the server answers 400 (pooled pods exist before
   the request, so the sidecar cannot be added); the script checks the 400 and goes on.

Checks (exit 1): the buffer fills; every warm sandbox runs in a pool pod and every cold one in its
own; the used pool pod is deleted and the buffer refilled; the 400. The warm/cold timing is printed,
not checked: three creates each on a shared cluster can come out either way.

The Pool is deleted at the end (its idle pods hold 1 CPU each); KEEP_POOL=1 keeps it. The script
deletes only its own sandboxes.
"""

import json
import os
import statistics
import sys
import time
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

from common import (
    ECR_PREFIX,
    ECR_REGISTRY,
    HERE,
    POOL_NAME,
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

SCRIPT = "04_pools"
ROUNDS = int(os.environ.get("ROUNDS", "3"))
POOL = f"pools.sandbox.opensandbox.io/{POOL_NAME}"
POD_LABEL = f"sandbox.opensandbox.io/pool-name={POOL_NAME}"
FILL_LIMIT = 300
meta = own_metadata(SCRIPT)


def pool_status() -> dict:
    out = kubectl("-n", SANDBOX_NAMESPACE, "get", POOL, "-o", "jsonpath={.status}", check=False).strip()
    return json.loads(out) if out else {}


def counts(status: dict) -> str:
    return (f"total {status.get('total', 0)}, allocated {status.get('allocated', 0)}, "
            f"available {status.get('available', 0)}")


def pool_pods() -> dict[str, str]:
    """name -> phase (Terminating when deleted) of the pool's pods."""
    items = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", POD_LABEL, "-o", "json"))["items"]
    return {p["metadata"]["name"]: ("Terminating" if p["metadata"].get("deletionTimestamp") else p["status"].get("phase", "?"))
            for p in items}


def wait_available(minimum: int, quiet: bool = False) -> float:
    started = time.monotonic()
    last = None
    while time.monotonic() - started < FILL_LIMIT:
        status = pool_status()
        if not quiet and counts(status) != last:
            print(f"  [{utc_now()}] +{time.monotonic() - started:5.1f}s  pool: {counts(status)}", flush=True)
            last = counts(status)
        if status.get("available", 0) >= minimum:
            return time.monotonic() - started
        time.sleep(1)
    raise RuntimeError(f"pool did not reach {minimum} available pods in {FILL_LIMIT} s ({counts(pool_status())})")


def timed_create(pooled: bool) -> tuple[float, SandboxSync]:
    started = time.monotonic()
    sb = SandboxSync.create(image(), timeout=timedelta(minutes=10), ready_timeout=timedelta(seconds=180), metadata=meta,
                            extensions={"poolRef": POOL_NAME} if pooled else None, connection_config=connection("proxy"))
    return time.monotonic() - started, sb


def run(sb: SandboxSync, command: str) -> str:
    execution = sb.commands.run(command)
    return "".join(part.text for part in execution.logs.stdout).strip()


def allocated_pod(sandbox_id: str) -> str:
    """The pool pod a pooled sandbox got, from the BatchSandbox's alloc-status annotation."""
    bs = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "batchsandboxes.sandbox.opensandbox.io", sandbox_id, "-o", "json"))
    raw = (bs["metadata"].get("annotations") or {}).get("sandbox.opensandbox.io/alloc-status", "{}")
    pods = json.loads(raw).get("pods") or []
    return pods[0] if pods else "?"


def main() -> int:
    connect_server()
    failures: list[str] = []
    print(f"sandboxes tagged run={meta['run']}")
    try:
        heading(f"1. apply the Pool {POOL_NAME}")
        manifest = (HERE / "manifests" / "pool.yaml").read_text()
        manifest = manifest.replace("${ECR_REGISTRY}", ECR_REGISTRY).replace("${ECR_PREFIX}", ECR_PREFIX)
        print("  " + kubectl("apply", "-f", "-", stdin=manifest).strip())
        spec = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", POOL, "-o", "jsonpath={.spec}"))
        print(f"  capacitySpec {json.dumps(spec['capacitySpec'])}, recycleStrategy {spec['recycleStrategy']['type']}")
        took = wait_available(spec["capacitySpec"]["bufferMin"])
        print(f"  buffer filled ({spec['capacitySpec']['bufferMin']} Ready pods) {took:.0f} s after the apply")

        heading(f"2. cold and warm creates, {ROUNDS} rounds")
        cold, warm = [], []
        for n in range(1, ROUNDS + 1):
            ensure_port_forward()
            took, sb = timed_create(pooled=False)
            cold.append(took)
            host = run(sb, "echo $(hostname)")
            print(f"  round {n}  cold {took:5.1f} s   ({host})", flush=True)
            if host != f"{sb.id}-0":
                failures.append(f"round {n}: the cold sandbox ran in {host}, not in its own pod")
            sb.kill()
            sb.close()
            wait_available(1, quiet=True)
            took, sb = timed_create(pooled=True)
            warm.append(took)
            host, pod = run(sb, "echo $(hostname)"), allocated_pod(sb.id)
            print(f"  round {n}  warm {took:5.1f} s   ({host}, pool pod {pod})", flush=True)
            if not (pod.startswith(POOL_NAME + "-") and host == pod):
                failures.append(f"round {n}: the pooled sandbox did not run in a pool pod ({host}, {pod})")
            sb.kill()
            sb.close()
        # A timing, not a check: a few creates on a shared cluster can come out either way.
        gap = statistics.median(cold) - statistics.median(warm)
        print(f"  median  cold {statistics.median(cold):.1f} s, warm {statistics.median(warm):.1f} s: "
              + (f"warm is {gap:.1f} s faster" if gap > 0 else f"warm was NOT faster this time ({-gap:.1f} s slower)"))

        heading("3. recycleStrategy Delete: a used pod is replaced, not reused")
        wait_available(spec["capacitySpec"]["bufferMin"], quiet=True)
        before = pool_pods()
        print(f"  [{utc_now()}] pool pods: {', '.join(sorted(before))}")
        took, sb = timed_create(pooled=True)
        used = allocated_pod(sb.id)
        print(f"  [{utc_now()}] sandbox {sb.id[:8]}... got pool pod {used}; pool: {counts(pool_status())}")
        run(sb, "echo left behind > /tmp/previous-user.txt")
        sb.kill()
        sb.close()
        print(f"  [{utc_now()}] sandbox deleted")
        started = time.monotonic()
        last = None
        replaced = False
        while time.monotonic() - started < 120:
            now = pool_pods()
            line = ", ".join(f"{name}{'' if phase == 'Running' else ' ' + phase}" for name, phase in sorted(now.items()))
            if line != last:
                print(f"  [{utc_now()}] +{time.monotonic() - started:4.0f}s  pool pods: {line}", flush=True)
                last = line
            new = set(now) - set(before)
            if used not in now and new and pool_status().get("available", 0) >= spec["capacitySpec"]["bufferMin"]:
                replaced = True
                break
            time.sleep(1)
        if replaced:
            status = pool_status()
            print(f"  {used} was deleted with its sandbox, /tmp/previous-user.txt and all; it never returns to the "
                  f"pool. The buffer holds {status.get('available', 0)} pods that never ran a sandbox "
                  f"(new: {', '.join(sorted(set(pool_pods()) - set(before)))})")
        else:
            failures.append(f"pool pod {used} was not replaced within 120 s")

        heading("4. a pooled create with a network policy")
        try:
            sb = SandboxSync.create(image(), timeout=timedelta(minutes=5), metadata=meta, extensions={"poolRef": POOL_NAME},
                                    network_policy=NetworkPolicy(default_action="deny",
                                                                 egress=[NetworkRule(action="allow", target="pypi.org")]),
                                    connection_config=connection("proxy"))
            failures.append("the server accepted poolRef together with networkPolicy")
            sb.kill()
            sb.close()
        except Exception as error:
            status = getattr(error, "status_code", None)
            text = str(error)
            names_both = "poolRef" in text and "networkPolicy" in text
            print(f"  {'ok  ' if status == 400 and names_both else 'FAIL'}  refused with HTTP {status}: "
                  f"{text.split(' | ')[0][:240]}")
            if status != 400 or not names_both:
                failures.append(f"expected HTTP 400 naming poolRef and networkPolicy, got {status}: {text[:120]}")
        print("  (the script goes on: put an egress policy for pooled sandboxes in the Pool template)")
    except Exception as error:  # SDK or kubectl trouble: one line, then clean up
        failures.append(f"{type(error).__name__}: {str(error)[:240]}")
    finally:
        heading("clean up")
        delete_all_sandboxes(metadata={"run": meta["run"]})
        print("  no sandbox of this run is left")
        if os.environ.get("KEEP_POOL") == "1":
            print(f"  KEEP_POOL=1: the Pool stays ({counts(pool_status())}); 99_cleanup.py deletes it")
        else:
            print("  " + kubectl("-n", SANDBOX_NAMESPACE, "delete", POOL, "--ignore-not-found", "--wait=true",
                                 "--timeout=120s", check=False).strip())
    if failures:
        print("\n" + "\n".join(f"  FAIL  {item}" for item in failures))
        return 1
    print("\nall steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
