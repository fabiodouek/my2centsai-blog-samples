"""Delete every sandbox and snapshot created by these scripts, then show what's left in the quota.
Stopped sandboxes otherwise linger (and their disk is billed) until delete_after_stop_seconds runs out."""

import time

from common import DEMO_LABELS, PREFIX, banner, client, rest


def ours() -> list[str]:
    """Names of sandboxes carrying our label or our name prefix, across all pages."""
    found = set()
    (key, value), = DEMO_LABELS.items()
    for params in ({"label": f"{key}={value}"}, {"name_contains": PREFIX}):  # SDK-created ones have no labels
        cursor = None
        while True:
            page = rest("GET", "/boxes", params={**params, "page_size": 100, **({"cursor": cursor} if cursor else {})})
            found.update(b["name"] for b in page["items"]
                         if b["name"].startswith(PREFIX) or (b.get("labels") or {}).get(key) == value)
            cursor = page.get("next_cursor")
            if not cursor:
                break
    return sorted(found)


banner(f"Sandboxes labelled {DEMO_LABELS} or named {PREFIX}-*")
names = ours()
if names:
    res = rest("POST", "/boxes/batch-delete", json={"names": names})
    print(f"batch-delete: deleted {len(res.get('deleted', []))}, skipped {res.get('skipped')}")
for _ in range(60):  # deletion is asynchronous ("deleting"), and snapshots stay in use until it finishes
    if not ours():
        break
    time.sleep(2)
print(f"remaining: {ours()}")

banner(f"Snapshots named {PREFIX}-*")
snaps = []
while batch := client.list_snapshots(name_contains=PREFIX, limit=100, offset=len(snaps)):
    snaps += batch
    if len(batch) < 100:
        break
for s in snaps:
    if s.name.startswith(PREFIX):
        client.delete_snapshot(s.id)
        print(f"  deleted {s.name} {s.id} tags={s.tags}")
print(f"left: {[s.name for s in client.list_snapshots(name_contains=PREFIX)]}")

u = rest("GET", "/usage")
print(f"\nquota now: sandboxes {u['sandboxes_used']}/{u['sandboxes_limit']} (running {u['sandbox_running_count']}, "
      f"stopped {u['sandbox_stopped_count']}) | snapshots {u['snapshots_count']}")
