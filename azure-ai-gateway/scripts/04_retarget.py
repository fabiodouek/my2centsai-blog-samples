"""Retarget the alias to another model while a loop of calls runs. Counts failed calls."""
import threading
import time

import common

prov = common.names()["FOUNDRY"]
B = common.model_id(prov, "alt")
events = {}


def retarget():
    events["start"] = time.time()
    common.aig("model-alias", "set", "-n", "chat", "--routing-name", "chat",
               "--backend-model", f"resource-id={B}", "priority=1", "weight=100", "-o", "none")
    events["done"] = time.time()


t0 = time.time()
failed = 0
first_new = None
for i in range(1, 26):
    if i == 4:
        threading.Thread(target=retarget).start()
        print(f"{time.time()-t0:5.1f}s  retarget started: alias chat -> deployment alt")
    r, dt = common.call(prompt="Reply with one word.")
    who = common.served_by(r)
    failed += r.status_code != 200
    if first_new is None and who == "alt":
        first_new = time.time()
        print(f"{time.time()-t0:5.1f}s  first answer from the new target")
    print(f"{time.time()-t0:5.1f}s  call {i:2d}  HTTP {r.status_code}  {dt:4.1f}s  {who}")
    time.sleep(1.0)
print(f"failed calls: {failed} of {i}")
if "done" in events and first_new:
    print(f"az returned {events['done']-events['start']:.1f}s after start; first new answer {first_new-events['start']:.1f}s after start")
