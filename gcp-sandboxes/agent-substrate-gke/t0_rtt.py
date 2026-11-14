"""Client path baseline (EKS baseline section 1.2): the median of 10 x `ax get tasks` on an empty atespace, and the
first-call cost of a session. Every client-side number in the other scripts includes this round trip."""
import statistics, time
from common import ax, banner, delete_all, kate, save, summary

delete_all()
banner("10 x ax get tasks on an empty atespace")
times = []
for _ in range(10):
    t0 = time.perf_counter(); ax("get", "tasks", quiet=True); times.append((time.perf_counter() - t0) * 1000)
summary("ax get tasks", times)
banner("10 x kubectl ate get atespace (the native path, through the steady port-forward)")
ktimes = []
for _ in range(10):
    t0 = time.perf_counter(); kate("get", "atespace", quiet=True); ktimes.append((time.perf_counter() - t0) * 1000)
summary("kubectl ate get atespace", ktimes)
save("rtt", {"get_tasks_ms": times, "median": statistics.median(times), "kate_get_atespace_ms": ktimes, "kate_median": statistics.median(ktimes)})
