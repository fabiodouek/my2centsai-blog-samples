"""Terminate this repo's MicroVMs, delete the sandbox image (it bills snapshot storage) and its build log group.
Then run scripts/destroy.sh, which switches off the DNS Firewall association's mutation protection and runs
`terraform destroy`."""

import time

from common import IMAGE, banner, err, image_arn, logs, mvm, our_microvms

banner("MicroVMs")
for m in our_microvms():
    print("terminate", m["microvmId"], m["state"])
    try:
        mvm.terminate_microvm(microvmIdentifier=m["microvmId"])
    except Exception as e:
        print("  ", err(e))
t = time.time()
while our_microvms() and time.time() - t < 300:
    time.sleep(5)

banner("Image")
try:
    mvm.delete_microvm_image(imageIdentifier=image_arn())
    print("deleted")
except Exception as e:
    print(err(e))

banner("Image build log group (created by Lambda, not Terraform)")
try:
    logs.delete_log_group(logGroupName=f"/aws/lambda-microvms/{IMAGE}")
    print("deleted")
except Exception as e:
    print(err(e))
print("\nNow: scripts/destroy.sh")
