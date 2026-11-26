"""Remove what this repo created outside Terraform: running MicroVMs of the image, the SQS
event source mapping, the three functions (versions and aliases go with them), their log
groups, and the MicroVM image. Then run `terraform -chdir=infra destroy`.

Image storage bills for a one-week minimum even if you delete it sooner.
"""

import time

from common import (FUNCTION, LEAKY_FUNCTION, QUEUE_FUNCTION, banner, image_arn, lam, list_microvms, logs, microvm,
                    mvm, say, tf)


def main() -> None:
    banner("Cleanup")
    live = list_microvms(("PENDING", "RUNNING", "SUSPENDING", "SUSPENDED", "RESUMING"))
    for v in live:
        mvm.terminate_microvm(microvmIdentifier=v["microvmId"])
    say(f"terminated {len(live)} MicroVMs")
    while any(microvm(v["microvmId"])["state"] != "TERMINATED" for v in live):
        time.sleep(3)

    try:
        queue = tf()["queue_arn"]
        for m in lam.list_event_source_mappings(EventSourceArn=queue)["EventSourceMappings"]:
            lam.delete_event_source_mapping(UUID=m["UUID"])
            say(f"deleted event source mapping {m['UUID'][:8]}")
    except Exception as e:  # Terraform already destroyed
        say(f"event source mappings: skipped ({type(e).__name__})")

    for fn in (FUNCTION, QUEUE_FUNCTION, LEAKY_FUNCTION):
        try:
            lam.delete_function(FunctionName=fn)
            say(f"deleted function {fn}")
        except lam.exceptions.ResourceNotFoundException:
            pass
        for group in (f"/aws/lambda/{fn}",):
            try:
                logs.delete_log_group(logGroupName=group)
                say(f"deleted log group {group}")
            except logs.exceptions.ResourceNotFoundException:
                pass

    try:
        mvm.delete_microvm_image(imageIdentifier=image_arn())
        say("deleting the MicroVM image")
        while True:
            try:
                s = mvm.get_microvm_image(imageIdentifier=image_arn())["state"]
            except mvm.exceptions.ResourceNotFoundException:
                break
            if s in ("DELETED",):
                break
            if s.endswith("FAILED"):
                raise RuntimeError(f"image {s}")
            time.sleep(5)
        say("image deleted")
    except mvm.exceptions.ResourceNotFoundException:
        pass
    for group in (f"/aws/lambda-microvms/{image_arn().split(':')[-1]}",):
        try:
            logs.delete_log_group(logGroupName=group)
            say(f"deleted log group {group}")
        except logs.exceptions.ResourceNotFoundException:
            pass
    say("now run: terraform -chdir=infra destroy")


if __name__ == "__main__":
    main()
