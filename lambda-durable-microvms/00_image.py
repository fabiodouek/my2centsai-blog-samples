"""Build the worker MicroVM image from image/ (Dockerfile, app.py, requirements.txt).

The image enables every lifecycle hook on port 8080, the worker's port: run (job
delivery), suspend and resume (sessions that suspend between jobs), terminate (fail
running jobs at once), and ready (required whenever a lifecycle hook is enabled).
"""

import io
import sys
import time
import zipfile

from common import BASE_IMAGE, HERE, IMAGE_NAME, INTERNET_EGRESS, banner, image_arn, mvm, s3, save, say, tf

HOOKS = {
    "port": 8080,
    "microvmHooks": {
        "run": "ENABLED", "runTimeoutInSeconds": 10,
        "resume": "ENABLED", "resumeTimeoutInSeconds": 10,
        "suspend": "ENABLED", "suspendTimeoutInSeconds": 10,
        "terminate": "ENABLED", "terminateTimeoutInSeconds": 10,
    },
    "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 120},
}


def zip_image() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((HERE / "image").iterdir()):
            if f.is_file():
                z.write(f, f.name)
    return buf.getvalue()


def exists() -> bool:
    try:
        mvm.get_microvm_image(imageIdentifier=image_arn())
        return True
    except mvm.exceptions.ResourceNotFoundException:
        return False


def main() -> None:
    banner("Build the worker image")
    key = f"{IMAGE_NAME}-{int(time.time())}.zip"
    s3.put_object(Bucket=tf()["bucket"], Key=key, Body=zip_image())
    params = {
        "codeArtifact": {"uri": f"s3://{tf()['bucket']}/{key}"},
        "baseImageArn": BASE_IMAGE,
        "buildRoleArn": tf()["build_role_arn"],
        "hooks": HOOKS,
        # pip and git need the internet during the build
        "egressNetworkConnectors": [INTERNET_EGRESS],
    }
    t0 = time.time()
    if exists():
        if "--rebuild" not in sys.argv:
            d = mvm.get_microvm_image(imageIdentifier=image_arn())
            say(f"image exists: {d['name']} state={d['state']} version={d.get('latestActiveImageVersion')}")
            say("(pass --rebuild to build a new version)")
            return
        mvm.update_microvm_image(imageIdentifier=image_arn(), **params)
        say("update-microvm-image submitted")
    else:
        mvm.create_microvm_image(name=IMAGE_NAME, **params)
        say(f"create-microvm-image submitted: {IMAGE_NAME}")
    last = None
    while True:
        try:
            d = mvm.get_microvm_image(imageIdentifier=image_arn())
        except mvm.exceptions.ResourceNotFoundException:
            time.sleep(5)
            continue
        if d["state"] != last:
            say(f"  [{time.time() - t0:6.1f}s] {d['state']}")
            last = d["state"]
        if d["state"] not in ("CREATING", "UPDATING"):
            break
        time.sleep(5)
    say(f"image {d['state']} after {time.time() - t0:.0f}s, active version {d.get('latestActiveImageVersion')}")
    say(f"build logs: /aws/lambda-microvms/{IMAGE_NAME}")
    save("00_image", {"state": d["state"], "seconds": round(time.time() - t0, 1),
                      "version": d.get("latestActiveImageVersion")})


if __name__ == "__main__":
    main()
