"""Test 1: create tasks. What state do they start in, and what does a task cost
before it ever runs?"""

from common import ATESPACE, ax, banner, create, kubectl, status, summary, timed

N = 5

banner(f"create {N} tasks in atespace '{ATESPACE}'")
times = []
for i in range(1, N + 1):
    _, ms = timed(f"ax apply  lc-create-{i}", create, f"lc-create-{i}")
    times.append(ms)
summary("create", times)

banner("ax get tasks")
ax("get", "tasks")

banner("status of a new task")
print(status("lc-create-1"))

banner("worker pods: a created task occupies none of them")
kubectl("-n", "ax-system", "get", "pods", "-l", "ate.dev/worker-pool=ax-workers", "-o", "wide")
