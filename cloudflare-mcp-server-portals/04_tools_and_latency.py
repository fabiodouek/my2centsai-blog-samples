"""Tool names through the portal, and what the extra hop costs: the same call 30 times direct and through the portal."""
import statistics

from common import *

N = 30
st = load_state()
portal = Mcp(PORTAL_URL, svc_headers(st))
portal.initialize()
direct = Mcp(NOTES_URL)

print("Tools the agent sees through the portal:")
names = [t["name"] for t in portal.tools()]
for n in names:
    print("  ", n)
print("Tools the notes Worker exposes directly:", [t["name"] for t in direct.tools()])


def timed(client, tool):
    ms = []
    for _ in range(N):
        s, t, msg, _h = client.call(tool, {"id": "n1"})
        assert s == 200 and "error" not in msg, (s, msg)
        ms.append(t)
    return ms


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, round(p / 100 * (len(v) - 1)))]


timed(direct, "get_note")  # warm both paths once
timed(portal, "notes_get_note")
d, p = timed(direct, "get_note"), timed(portal, "notes_get_note")
rows = {"direct": d, "portal": p}
print(f"\n{N} calls of get_note, round trip from this machine, ms:")
print(f"{'':8}{'min':>7}{'p50':>7}{'p95':>7}{'max':>7}")
for k, v in rows.items():
    print(f"{k:8}{min(v):7.0f}{statistics.median(v):7.0f}{pct(v, 95):7.0f}{max(v):7.0f}")
print(f"calls the edge answered with error 1014 and that were sent again: {portal.retries_1014}")
print(f"median overhead of the portal: {statistics.median(p) - statistics.median(d):.0f} ms")
save_result("04_latency.json", {"calls": N, "tools_through_portal": names,
                                "direct_ms": [round(x) for x in d], "portal_ms": [round(x) for x in p],
                                "retries_1014": portal.retries_1014, "median_overhead_ms": round(statistics.median(p) - statistics.median(d))})
