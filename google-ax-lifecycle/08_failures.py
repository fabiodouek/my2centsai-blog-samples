"""Test 8: when things end badly. The command exits; a worker pod dies under a
running task and under a suspended one; and the WorkerPool itself changes."""

import json
import time

from common import ax, banner, create, kubectl, sh, status, timed, worker_node

banner("the command exits with code 3")
create("lc-exit", ["sh", "-c", "echo working; sleep 2; echo done; exit 3"])
ax("resume", "task", "lc-exit", quiet=True)
time.sleep(5)
print(status("lc-exit"))
print(sh("lc-exit", "ps -eo pid,args"))

banner("a worker pod dies: one task running on it, one suspended from it")
create("lc-victim-running")
create("lc-victim-suspended")
ax("resume", "task", "lc-victim-running", quiet=True)
worker = status("lc-victim-running")["worker"]
for _ in range(8):  # land the second task on the same worker
    ax("resume", "task", "lc-victim-suspended", quiet=True)
    if status("lc-victim-suspended")["worker"] == worker:
        break
    ax("suspend", "task", "lc-victim-suspended", quiet=True)
if status("lc-victim-suspended")["phase"] != "Running":
    ax("resume", "task", "lc-victim-suspended", quiet=True)
sh("lc-victim-suspended", "echo still-here > /workspace/marker")
ax("suspend", "task", "lc-victim-suspended", quiet=True)
sh("lc-victim-running", "echo never-snapshotted > /workspace/unsaved")
pod = worker_node(worker).split()[0]
print(f"  both on {worker_node(worker)}")

timed(f"kubectl delete pod {pod}", kubectl, "-n", "ax-system", "delete", "pod", pod, "--wait=true", quiet=True)
time.sleep(5)
print("  running task:  ", status("lc-victim-running"))
print("  suspended task:", status("lc-victim-suspended"))

banner("can either come back?")
timed("resume the suspended one", ax, "resume", "task", "lc-victim-suspended")
print(sh("lc-victim-suspended", "cat /workspace/marker"))
timed("resume the failed one", ax, "resume", "task", "lc-victim-running", check=False)
print("  failed task now:", status("lc-victim-running"))
print(sh("lc-victim-running", "uptime; cat /workspace/unsaved", check=False))

banner("change the WorkerPool while tasks run: declare capacity")
ax("resume", "task", "lc-exit", quiet=True, check=False)
running = [t for t in ("lc-exit", "lc-victim-running", "lc-victim-suspended") if status(t)["phase"] == "Running"]
print(f"  running before: {running}")
capacity = {"spec": {"template": {"resources": {"requests": {"cpu": "1", "memory": "4Gi"},
                                                 "limits": {"cpu": "3", "memory": "12Gi"}}}}}
kubectl("-n", "ax-system", "patch", "workerpool", "ax-workers", "--type", "merge", "-p", json.dumps(capacity))
timed("worker rollout", kubectl, "-n", "ax-system", "rollout", "status", "deploy/ax-workers", "--timeout=300s", quiet=True)
time.sleep(5)
for t in running:
    print(f"  {t}: {status(t)}")

banner("put the pool back as installed")
kubectl("-n", "ax-system", "patch", "workerpool", "ax-workers", "--type", "json",
        "-p", '[{"op":"remove","path":"/spec/template"}]')
kubectl("-n", "ax-system", "rollout", "status", "deploy/ax-workers", "--timeout=300s", quiet=True)
