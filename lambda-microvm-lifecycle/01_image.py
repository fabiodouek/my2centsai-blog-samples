"""Test 1: build an image from a Dockerfile. Base images, build time and states, what the build
produced (versions, per-chipset snapshot builds), the build hooks in the logs, a second version,
deactivating a version, and whether x86_64 is accepted."""

import json
import time

from common import BASE_IMAGE, banner, build_image, err, image_arn, image_name, logs, mvm, redact, run, show, tf

banner("Managed base images")
for img in mvm.list_managed_microvm_images()["items"]:
    print(img["imageArn"], "updated", img.get("updatedAt"))
    for v in mvm.list_managed_microvm_image_versions(imageIdentifier=img["imageArn"]).get("items", []):
        print("  ", {k: v.get(k) for k in ("imageVersion", "status", "state", "createdAt", "deprecationDate", "expirationDate") if k in v})

banner("Build images/sandbox (python:3.12-slim + agent)")
b = build_image("sandbox")
print(f"{b['action']}: states {b['states']}  total {b['seconds']}s")
show(b["image"], ["imageArn", "state", "latestActiveImageVersion", "createdAt", "updatedAt", "stateReason"])

banner("Versions and per-chipset snapshot builds")
arn = image_arn("sandbox")
for v in mvm.list_microvm_image_versions(imageIdentifier=arn)["items"]:
    print(redact({k: v.get(k) for k in ("imageVersion", "state", "status", "createdAt", "updatedAt")}))
version = b["image"]["latestActiveImageVersion"]
d = mvm.get_microvm_image_version(imageIdentifier=arn, imageVersion=version)
show(d, ["imageVersion", "baseImageVersion", "resources", "cpuConfigurations", "hooks", "egressNetworkConnectors",
         "additionalOsCapabilities", "environmentVariables", "logging"])
builds = mvm.list_microvm_image_builds(imageIdentifier=arn, imageVersion=version)["items"]
for bl in builds:
    print(redact({k: bl.get(k) for k in ("buildId", "buildState", "architecture", "chipset", "chipsetGeneration", "createdAt")}))
if builds:
    show(mvm.get_microvm_image_build(imageIdentifier=arn, imageVersion=version, buildId=builds[0]["buildId"]))

banner("Build logs (ready and validate hooks)")
group = f"/aws/lambda-microvms/{image_name('sandbox')}"
try:
    streams = logs.describe_log_streams(logGroupName=group, orderBy="LastEventTime", descending=True, limit=10)["logStreams"]
    for st in streams[:4]:
        events = logs.get_log_events(logGroupName=group, logStreamName=st["logStreamName"], startFromHead=True, limit=40)["events"]
        print(f"-- {st['logStreamName']} ({len(events)} events)")
        for e in events[:12]:
            print("   ", redact(e["message"].rstrip())[:200])
except Exception as e:
    print(err(e))

banner("A second version: update with an environment variable")
b2 = build_image("sandbox", environmentVariables={"SANDBOX_FLAVOUR": "v2"})
print(f"{b2['action']}: states {b2['states']}  total {b2['seconds']}s, latest active {b2['image'].get('latestActiveImageVersion')}")
for v in mvm.list_microvm_image_versions(imageIdentifier=arn)["items"]:
    print(redact({k: v.get(k) for k in ("imageVersion", "state", "status")}))

banner("Latest vs pinned version, then deactivate the old one")
old = version
for label, pin in (("latest active", None), (f"pinned {old}", old)):
    vm = run("sandbox", "01-pin", version=pin)
    vm.wait("RUNNING")
    vm.ready()
    print(f"{label}: imageVersion={vm.describe().get('imageVersion')}  SANDBOX_FLAVOUR={vm.sh('echo ${SANDBOX_FLAVOUR:-unset}')}")
    vm.terminate()
mvm.update_microvm_image_version(imageIdentifier=arn, imageVersion=old, status="INACTIVE")
print(f"version {old} set INACTIVE")
try:
    r = mvm.run_microvm(imageIdentifier=arn, imageVersion=old, maximumDurationInSeconds=120)
    print("run on INACTIVE version succeeded?", r["microvmId"])
    mvm.terminate_microvm(microvmIdentifier=r["microvmId"])
except Exception as e:
    print("run on INACTIVE version:", err(e))
mvm.update_microvm_image_version(imageIdentifier=arn, imageVersion=old, status="ACTIVE")
print(f"version {old} set ACTIVE again")

banner("Does the API accept x86_64?")
try:
    r = mvm.create_microvm_image(
        name=image_name("x86-probe"), baseImageArn=BASE_IMAGE, buildRoleArn=tf()["build_role_arn"],
        codeArtifact={"uri": b["image"].get("codeArtifact", {}).get("uri") or d["codeArtifact"]["uri"]},
        cpuConfigurations=[{"architecture": "X86_64"}])
    print("accepted:", r["state"])
    for _ in range(36):
        s = mvm.get_microvm_image(imageIdentifier=image_arn("x86-probe"))
        if s["state"] not in ("CREATING",):
            break
        time.sleep(5)
    print("x86 image state:", s["state"], s.get("stateReason"))
    mvm.delete_microvm_image(imageIdentifier=image_arn("x86-probe"))
except Exception as e:
    print("x86_64:", err(e))
