"""Build (or update) the workspace sandbox images from images/sandbox/: one image per sandbox size, all from the same
code artifact. A MicroVM's memory comes from its image (`resources[].minimumMemoryInMiB`, vCPU = memory / 2 GB), so
each size is its own image:

    small   1 GB  0.5 vCPU  my2cents-ws-sandbox-1g
    medium  2 GB  1 vCPU    my2cents-ws-sandbox      (the default, and the original image)
    large   4 GB  2 vCPU    my2cents-ws-sandbox-4g
    xlarge  8 GB  4 vCPU    my2cents-ws-sandbox-8g   (the account's whole 8 GB MicroVM quota)

Uses part 2's build role and artifacts bucket. Two files are filled in from part 2's Terraform outputs at zip time:
ca.crt (iron-proxy's demo CA, public) and the approved-key entry in claude.json (the last 20 characters of the
Anthropic placeholder, which is what Claude Code stores for a pre-approved key). The build itself uses the default
internet egress (apt, npm, GitHub releases), not the proxy.

Each image is tagged `ca-sha256` with the first 16 hex digits of the CA's SHA-256. A new part 2 means a new CA, and
sandboxes built for the old one would refuse every iron-proxy certificate, so `--if-needed` (what deploy.sh uses)
rebuilds an image when its tag doesn't match part 2's CA, or when it doesn't exist yet, and skips it otherwise.
Images are built one after another (an 8 GB build alone fills the account's MicroVM quota).

  .venv/bin/python scripts/build_sandbox_image.py                      # every size
  .venv/bin/python scripts/build_sandbox_image.py --size medium        # one size (repeat --size for more)
  .venv/bin/python scripts/build_sandbox_image.py --if-needed          # only missing or stale images
"""

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile

from mvm import BASE_IMAGE, IMAGE_NAME, ROOT, SIZES, image_arn, mvm, part2, redact, s3

SRC = ROOT / "images" / "sandbox"
SKIP = {"ca.crt", "README.md", ".gitignore", "__pycache__", ".DS_Store"}

HOOKS = {
    "port": 9001,
    "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 30, "resume": "ENABLED", "resumeTimeoutInSeconds": 30,
                     "suspend": "ENABLED", "suspendTimeoutInSeconds": 30, "terminate": "ENABLED", "terminateTimeoutInSeconds": 30},
    "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 300, "validate": "ENABLED", "validateTimeoutInSeconds": 300},
}

# Run by the agent inside /validate (see images/sandbox/warm.sh): the CLIs, then the Claude Code TUI up to its
# input box in tmux. Lambda prefetches what this touches when a MicroVM starts, so the first `claude` in a new
# sandbox doesn't page in a 330 MB binary lazily.
VALIDATE_CMD = "bash /app/warm.sh"


def build_zip(t: dict) -> bytes:
    ca = t["ca_cert_pem"]
    (SRC / "ca.crt").write_text(ca)  # local docker builds use it too; gitignored
    claude_json = json.loads((SRC / "claude.json").read_text())
    claude_json["customApiKeyResponses"]["approved"] = [t["placeholders"]["anthropic"][-20:]]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(SRC.iterdir()):
            if f.name in SKIP or f.is_dir():
                continue
            if f.name == "claude.json":
                z.writestr(f.name, json.dumps(claude_json, indent=2) + "\n")
            else:
                z.write(f, f.name)
        z.writestr("ca.crt", ca)
    return buf.getvalue()


def ca_tag(ca_pem: str) -> str:
    return hashlib.sha256(ca_pem.strip().encode()).hexdigest()[:16]


def current(size: str) -> dict | None:
    try:
        return mvm.get_microvm_image(imageIdentifier=image_arn(size))
    except mvm.exceptions.ResourceNotFoundException:
        return None


def up_to_date(size: str, want: str) -> bool:
    d = current(size)
    have = ((d or {}).get("tags") or {}).get("ca-sha256")
    ok = bool(d and d.get("state") in ("CREATED", "UPDATED") and d.get("latestActiveImageVersion") and have == want)
    if not ok:
        print(f"{size}: building: " + ("none yet" if not d else
              f"it was built for CA {have or 'unknown'}, part 2's is {want}" if have != want else f"state {d.get('state')}"))
    return ok


def build(size: str, artifact_uri: str, t: dict, want: str) -> bool:
    mib, name = SIZES[size]
    arn = image_arn(size)
    params = {"codeArtifact": {"uri": artifact_uri}, "baseImageArn": BASE_IMAGE,
              "buildRoleArn": t["build_role_arn"], "resources": [{"minimumMemoryInMiB": mib}], "hooks": HOOKS,
              "environmentVariables": {"VALIDATE_CMD": VALIDATE_CMD}}
    existing = (current(size) or {}).get("state")
    if existing == "CREATE_FAILED":
        # An image whose first version never built has nothing to keep: delete it and start again
        print(f"{size}: previous create failed; deleting the image first")
        mvm.delete_microvm_image(imageIdentifier=arn)
        while current(size):
            time.sleep(5)
        existing = None
    t0 = time.perf_counter()
    if existing:
        mvm.update_microvm_image(imageIdentifier=arn, **params)
        action = "update"
    else:
        mvm.create_microvm_image(name=name, tags={"demo": "my2cents-workspace", "size": size, "ca-sha256": want}, **params)
        action = "create"
    print(f"{size}: {action} submitted for {redact(arn)} ({mib} MiB)")
    last = None
    while True:
        d = mvm.get_microvm_image(imageIdentifier=arn)
        if d["state"] != last:
            print(f"  [{time.perf_counter() - t0:6.1f}s] {d['state']}")
            last = d["state"]
        if d["state"] not in ("CREATING", "UPDATING"):
            break
        time.sleep(5)
    print(redact({k: d.get(k) for k in ("imageArn", "state", "stateReason", "latestActiveImageVersion", "updatedAt") if k in d}))
    versions = mvm.list_microvm_image_versions(imageIdentifier=arn).get("items", [])
    for v in sorted(versions, key=lambda v: str(v.get("createdAt")))[-3:]:
        print(f"  version {v.get('imageVersion')}: state={v.get('state')} status={v.get('status')}")
    print(f"{size}: build took {time.perf_counter() - t0:.0f}s")
    if "FAIL" in d["state"] or not d.get("latestActiveImageVersion"):
        return False
    # Record which CA this image trusts and its size (an update keeps the tags from the create)
    mvm.tag_resource(Resource=arn, Tags={"ca-sha256": want, "size": size})
    print(f"{size}: tagged ca-sha256={want}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", action="append", choices=list(SIZES), help="build only this size (repeatable)")
    ap.add_argument("--if-needed", action="store_true", help="skip images that exist and trust part 2's CA")
    a = ap.parse_args()
    sizes = a.size or list(SIZES)
    t = part2()
    want = ca_tag(t["ca_cert_pem"])
    if a.if_needed:
        sizes = [s for s in sizes if not up_to_date(s, want)]
        if not sizes:
            print(f"sandbox images are up to date (built for part 2's CA {want}); nothing to do")
            return
    # One code artifact for every size
    data = build_zip(t)
    key = f"{IMAGE_NAME}-{int(time.time())}.zip"
    s3.put_object(Bucket=t["artifacts_bucket"], Key=key, Body=data)
    print(f"uploaded {len(data) / 1024:.0f} KiB code artifact for {', '.join(sizes)}")
    failed = [s for s in sizes if not build(s, f"s3://{t['artifacts_bucket']}/{key}", t, want)]
    if failed:
        print(f"failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
