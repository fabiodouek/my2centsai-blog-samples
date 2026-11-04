"""Build (or update) the workspace sandbox image `my2cents-ws-sandbox` from images/sandbox/.

Uses part 2's build role and artifacts bucket. Two files are filled in from part 2's Terraform outputs
at zip time: ca.crt (iron-proxy's demo CA, public) and the approved-key entry in claude.json (the last
20 characters of the Anthropic placeholder, which is what Claude Code stores for a pre-approved key).
The build itself uses the default internet egress (apt, npm, GitHub releases), not the proxy.

The image is tagged `ca-sha256` with the first 16 hex digits of the CA's SHA-256. A new part 2 means a new CA, and
sandboxes built for the old one would refuse every iron-proxy certificate, so `--if-needed` (what deploy.sh uses)
rebuilds when the tag doesn't match part 2's CA, or when there is no image yet, and does nothing otherwise.

  .venv/bin/python scripts/build_sandbox_image.py
  .venv/bin/python scripts/build_sandbox_image.py --if-needed
"""

import hashlib
import io
import json
import sys
import time
import zipfile

from mvm import BASE_IMAGE, IMAGE_NAME, ROOT, image_arn, mvm, part2, redact, s3

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


def current() -> dict | None:
    try:
        return mvm.get_microvm_image(imageIdentifier=image_arn())
    except mvm.exceptions.ResourceNotFoundException:
        return None


def main() -> None:
    t = part2()
    want = ca_tag(t["ca_cert_pem"])
    if "--if-needed" in sys.argv[1:]:
        d = current()
        have = ((d or {}).get("tags") or {}).get("ca-sha256")
        if d and d.get("state") in ("CREATED", "UPDATED") and d.get("latestActiveImageVersion") and have == want:
            print(f"sandbox image is up to date (built for part 2's CA {want}); nothing to do")
            return
        print("building the sandbox image: " + ("none yet" if not d else
              f"it was built for CA {have or 'unknown'}, part 2's is {want}" if have != want else f"state {d.get('state')}"))
    data = build_zip(t)
    key = f"{IMAGE_NAME}-{int(time.time())}.zip"
    s3.put_object(Bucket=t["artifacts_bucket"], Key=key, Body=data)
    print(f"uploaded {len(data) / 1024:.0f} KiB code artifact")
    params = {"codeArtifact": {"uri": f"s3://{t['artifacts_bucket']}/{key}"}, "baseImageArn": BASE_IMAGE,
              "buildRoleArn": t["build_role_arn"], "resources": [{"minimumMemoryInMiB": 2048}], "hooks": HOOKS,
              "environmentVariables": {"VALIDATE_CMD": VALIDATE_CMD}}
    try:
        existing = mvm.get_microvm_image(imageIdentifier=image_arn())["state"]
    except mvm.exceptions.ResourceNotFoundException:
        existing = None
    if existing == "CREATE_FAILED":
        # An image whose first version never built has nothing to keep: delete it and start again
        print("previous create failed; deleting the image first")
        mvm.delete_microvm_image(imageIdentifier=image_arn())
        while True:
            try:
                mvm.get_microvm_image(imageIdentifier=image_arn())
                time.sleep(5)
            except mvm.exceptions.ResourceNotFoundException:
                existing = None
                break
    t0 = time.perf_counter()
    if existing:
        mvm.update_microvm_image(imageIdentifier=image_arn(), **params)
        action = "update"
    else:
        mvm.create_microvm_image(name=IMAGE_NAME, tags={"demo": "my2cents-workspace", "ca-sha256": want}, **params)
        action = "create"
    print(f"{action} submitted for {redact(image_arn())}")
    last = None
    while True:
        d = mvm.get_microvm_image(imageIdentifier=image_arn())
        if d["state"] != last:
            print(f"  [{time.perf_counter() - t0:6.1f}s] {d['state']}")
            last = d["state"]
        if d["state"] not in ("CREATING", "UPDATING"):
            break
        time.sleep(5)
    print(redact({k: d.get(k) for k in ("imageArn", "state", "stateReason", "latestActiveImageVersion", "updatedAt") if k in d}))
    versions = mvm.list_microvm_image_versions(imageIdentifier=image_arn()).get("items", [])
    for v in sorted(versions, key=lambda v: str(v.get("createdAt")))[-3:]:
        print(f"  version {v.get('imageVersion')}: state={v.get('state')} status={v.get('status')}")
    print(f"build took {time.perf_counter() - t0:.0f}s")
    if "FAIL" in d["state"] or not d.get("latestActiveImageVersion"):
        sys.exit(1)
    # Record which CA this image trusts (an update keeps the tags from the create)
    mvm.tag_resource(Resource=image_arn(), Tags={"ca-sha256": want})
    print(f"tagged ca-sha256={want}")


if __name__ == "__main__":
    main()
