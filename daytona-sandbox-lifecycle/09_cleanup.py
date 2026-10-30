"""Test 9: delete every sandbox created by these scripts and the demo snapshots.
Stopped sandboxes are kept (and their disk billed) until something deletes them."""

import time

from daytona import Daytona, DaytonaConflictError, DaytonaNotFoundError, ListSandboxesQuery

from common import DEMO_LABELS, banner

daytona = Daytona()

banner("Sandboxes labelled " + str(DEMO_LABELS))
# Forks first: a parent can't be deleted while it has forks.
boxes = sorted(daytona.list(ListSandboxesQuery(labels=DEMO_LABELS)), key=lambda s: "fork" not in (s.name or ""))
for s in boxes:
    # A sandbox already on its way out (or mid stop/start) answers 409; wait and retry.
    for attempt in range(10):
        try:
            s.delete(wait=True)
            print(f"  deleted {s.id} {s.name} ({s.state.value})")
            break
        except DaytonaNotFoundError:
            print(f"  already gone {s.id}")
            break
        except DaytonaConflictError:
            time.sleep(3)
print(f"processed {len(boxes)}")

banner("Snapshots named my2cents-lifecycle*")
snaps = [s for s in daytona.snapshot.list().items if s.name.startswith("my2cents-lifecycle")]
for s in snaps:
    daytona.snapshot.delete(s)
    print(f"  deleted {s.name}")
print(f"deleted {len(snaps)}")
