"""Test 2: the first resume. How long until a created task is Running, where
does it land, and how big is the sandbox it gets?"""

from common import ax, banner, create, delete, sh, status, summary, timed, worker_node

INSIDE = ("uname -r; echo nproc $(nproc); grep MemTotal /proc/meminfo; "
          "df -h /workspace | tail -1; id")

banner("first resume of the 5 tasks from 01_create.py")
times = []
for i in range(1, 6):
    name = f"lc-create-{i}"
    _, ms = timed(f"ax resume {name}", ax, "resume", "task", name, quiet=True)
    times.append(ms)
    print(f"             -> {status(name)['phase']}, {worker_node(status(name)['worker'])}")
summary("first resume", times)

banner("inside a sandbox with AX's default size (1 CPU, 512 MiB)")
print(sh("lc-create-1", INSIDE))

banner("suspend them again: a suspended task holds no worker capacity")
for i in range(1, 6):
    timed(f"ax suspend lc-create-{i}", ax, "suspend", "task", f"lc-create-{i}", quiet=True)

banner("a task with limits: cpu 2, memory 2Gi")
create("lc-limits", cpu="2", memory="2Gi")
timed("ax resume lc-limits", ax, "resume", "task", "lc-limits", quiet=True)
print(sh("lc-limits", INSIDE))

delete("lc-limits")
