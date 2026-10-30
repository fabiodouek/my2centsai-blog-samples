"""Test 9: the quota (does a stopped sandbox count against it?) and the bill (GET /usage/costs).
Deliberately doesn't hammer the cap: it keeps at most 4 sandboxes alive."""

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from common import banner, create, rest, wait_status


def usage(label: str) -> None:
    u = rest("GET", "/usage")
    print(f"{label:28s} used {u['sandboxes_used']}/{u['sandboxes_limit']} | running {u['sandbox_running_count']} "
          f"stopped {u['sandbox_stopped_count']} | cpu {u['cpu_used_millicores']} m | "
          f"memory {u['memory_used_bytes'] / 1024**3:.0f} GiB | snapshots {u['snapshots_count']}")


banner("Does a stopped sandbox count against the cap?")
usage("before")
boxes = [create("09") for _ in range(4)]
try:
    usage("4 running")
    for b in boxes[:2]:
        b.stop()
        wait_status(b.name, "stopped")
    time.sleep(2)
    usage("2 running, 2 stopped")
finally:
    for b in boxes:
        b.delete()
time.sleep(2)
usage("after delete")

banner("The bill: GET /usage/costs, last 24 hours")
end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
params = {"start_time": (end - timedelta(hours=24)).isoformat(), "end_time": end.isoformat(),
          "granularity": "RESOURCE", "include_totals": "true", "page_size": 100}
items, cursor, totals = [], None, None
while True:
    page = rest("GET", "/usage/costs", params={**params, **({"cursor": cursor} if cursor else {})})
    items += page.get("items", [])
    totals = page.get("totals", totals)
    cursor = page.get("next_cursor")
    if not cursor:
        break
by_type = defaultdict(lambda: [0, 0.0, 0.0])
for it in items:
    t = by_type[it["resource_type"]]
    t[0] += 1
    t[1] += float(it["lcu"])
    t[2] += float(it["lsu"])
for rtype, (n, lcu, lsu) in by_type.items():
    print(f"{rtype:8s} {n:3d} resources | LCU {lcu:.6f} | LSU {lsu:.6f}")
print(f"totals: {totals}")
if items:
    top = max(items, key=lambda it: float(it["lcu"]) + float(it["lsu"]))
    print(f"largest single resource: {top}")
