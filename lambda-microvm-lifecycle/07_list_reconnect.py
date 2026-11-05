"""Test 7: list, filter, and reconnect from another process. What list_microvms returns and how it
filters (no tags on MicroVMs), the run hook payload limit (docs: 16 KB), clientToken idempotency,
and a second process that attaches to a running MicroVM with nothing but its ID."""

import json
import subprocess
import sys
import time
import uuid

from common import HERE, banner, err, image_arn, mvm, redact, run

banner("Two MicroVMs, then list")
a = run("sandbox", "07-a")
b = run("sandbox", "07-b")
a.wait("RUNNING")
b.wait("RUNNING")
items = mvm.list_microvms()["items"]
print(f"list_microvms: {len(items)} items; fields: {sorted(items[0]) if items else []}")
mine = [m for m in items if m["microvmId"] in (a.id, b.id)]
print(redact(mine[:1]))
print("filter by image:", len(mvm.list_microvms(imageIdentifier=image_arn("sandbox"))["items"]))
page = mvm.list_microvms(maxResults=1)
print("maxResults=1 ->", len(page["items"]), "item, nextToken present:", bool(page.get("nextToken")))
print("run_microvm accepts tags?", "tags" in mvm.meta.service_model.operation_model("RunMicrovm").input_shape.members)
try:
    mvm.tag_resource(Resource=f"arn:aws:lambda:{mvm.meta.region_name}:{image_arn('sandbox').split(':')[4]}:microvm:{a.id}",
                     Tags={"test": "07"})
    print("tag_resource on a MicroVM ARN: accepted")
except Exception as e:
    print("tag_resource on a MicroVM ARN:", redact(err(e)))

banner("Run hook payload size")
for size in (4096, 4097, 16384):
    try:
        vm = run("sandbox", "07-payload", payload="x" * size)
        vm.wait("RUNNING")
        vm.ready()
        print(f"{size:6d} bytes: accepted, /run hook saw {vm.agent_state()['run_payload_len']} bytes")
        vm.terminate()
    except Exception as e:
        print(f"{size:6d} bytes: {err(e)}")

banner("clientToken idempotency")
tok = str(uuid.uuid4())
params = {"imageIdentifier": image_arn("sandbox"), "clientToken": tok, "maximumDurationInSeconds": 600}
r1 = mvm.run_microvm(**params)
time.sleep(1.2)
try:
    r2 = mvm.run_microvm(**params)
    print("same clientToken twice ->", "same MicroVM" if r1["microvmId"] == r2["microvmId"] else "two MicroVMs",
          r1["microvmId"], r2["microvmId"])
    for m in {r1["microvmId"], r2["microvmId"]}:
        mvm.terminate_microvm(microvmIdentifier=m)
except Exception as e:
    print("second call:", err(e))
    mvm.terminate_microvm(microvmIdentifier=r1["microvmId"])

banner("Reconnect from another process, knowing only the ID")
a.ready()
a.exec("echo written-by-process-one > /root/marker")
child = f"""
import sys; sys.path.insert(0, {str(HERE)!r})
from common import VM
vm = VM.attach({a.id!r})
print('state', vm.state(), '| marker:', vm.sh('cat /root/marker'), '| uptime', vm.sh("cut -d' ' -f1 /proc/uptime"))
"""
t = time.perf_counter()
out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
print(out.stdout.strip() or out.stderr.strip()[-400:], f"({time.perf_counter() - t:.2f}s incl. interpreter start)")

for vm in (a, b):
    vm.terminate()
