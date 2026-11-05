"""Terminate every MicroVM started from this repo's images and delete the images.
Then run `terraform -chdir=infra destroy` to remove the bucket and roles."""

import time

from common import PREFIX, banner, err, mvm, our_microvms

banner("MicroVMs")
for m in our_microvms():
    print("terminate", m["microvmId"], m["state"])
    try:
        mvm.terminate_microvm(microvmIdentifier=m["microvmId"])
    except Exception as e:
        print("  ", err(e))
t = time.time()
while (left := [m for m in our_microvms() if m["state"] != "TERMINATED"]) and time.time() - t < 300:
    time.sleep(5)
print("still not terminated:", [m["microvmId"] for m in left] if left else "none")

banner("Images")
kw = {}
while True:
    r = mvm.list_microvm_images(**kw)
    for img in r.get("items", []):
        if img.get("name", "").startswith(PREFIX):
            try:
                mvm.delete_microvm_image(imageIdentifier=img["imageArn"])
                print("delete", img["name"])
            except Exception as e:
                print("delete", img["name"], err(e))
    if not r.get("nextToken"):
        break
    kw["nextToken"] = r["nextToken"]
print("\nNow: terraform -chdir=infra destroy")
