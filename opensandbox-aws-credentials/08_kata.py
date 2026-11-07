"""The same credential flow under VM isolation: one run on Kata (kata-qemu), then back to runc.

  uv run python 08_kata.py

One server process has one [secure_runtime], so this switches the server with
../opensandbox-lifecycle/switch_runtime.sh (a Helm upgrade; the API is down for a few seconds), runs the flow
once, and ALWAYS switches back to runc at the end, also when a step fails. Sandboxes created while the
server is on Kata run in a QEMU micro-VM; the egress sidecar's credential proxy and the SigV4 addon are
unchanged, because they run in the same pod network namespace inside the VM's pod.

The flow, once: the pod's runtimeClassName is kata-qemu and the guest kernel differs from the node's;
`aws sts get-caller-identity` returns the assumed-role ARN with the sandbox ID as the session name;
one S3 upload and download with a checksum; one Bedrock Converse. No AWS key is in the VM.

Needs ./01_iam.sh, ./02_broker.sh and ./00_image.sh to have run, and Kata installed (../opensandbox-eks/02_kata.sh).
"""

import subprocess
import sys

from common import (
    LIFECYCLE_DIR, MODEL_ID, SANDBOX_NAMESPACE, Checks, account_id, aws_env, cleanup, ensure_broker_forward,
    ensure_port_forward, heading, kubectl, log_mark, log_since, new_sandbox, note, run, sandbox_pod, sh,
    sigv4_events, test_bucket, PLACEHOLDER_ACCESS_KEY_ID,
)

SCRIPT = "08_kata"
SWITCH = LIFECYCLE_DIR / "switch_runtime.sh"
checks = Checks()


def switch(runtime: str) -> bool:
    print(f"  $ ../opensandbox-lifecycle/switch_runtime.sh {runtime}", flush=True)
    return subprocess.run([str(SWITCH), runtime]).returncode == 0


ensure_broker_forward()
bucket = test_bucket()
on_kata = False
try:
    heading("1. switch the server to Kata (kata-qemu)")
    on_kata = True  # restore even if the switch half-finished
    if not switch("kata"):
        sys.exit("could not switch the server to kata-qemu (see the output above); restoring runc")
    ensure_port_forward()

    heading("2. a sandbox with AWS credentials, under Kata")
    sb, view = new_sandbox(script=SCRIPT)
    sid = view["sandbox_id"]
    pod = sandbox_pod(sid)
    runtime_class = kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", pod, "-o", "jsonpath={.spec.runtimeClassName}").strip()
    node = kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", pod, "-o", "jsonpath={.spec.nodeName}").strip()
    node_kernel = kubectl("get", "node", node, "-o", "jsonpath={.status.nodeInfo.kernelVersion}").strip()
    guest_kernel = sh(sb, "uname -r").text.strip()
    print(f"  pod runtimeClassName: {runtime_class}")
    print(f"  node kernel {node_kernel}; the sandbox sees kernel {guest_kernel}")
    checks.check("the pod runs with runtimeClassName kata-qemu", runtime_class == "kata-qemu")
    checks.check("the sandbox's kernel is the VM's, not the node's", bool(guest_kernel) and guest_kernel != node_kernel)
    env = aws_env(sb)
    checks.check("placeholder keys only", env.get("AWS_ACCESS_KEY_ID") == PLACEHOLDER_ACCESS_KEY_ID and "AWS_SESSION_TOKEN" not in env)

    heading("3. identity, S3 and Bedrock from inside the VM")
    mark = log_mark(sid)
    ident = sh(sb, "aws sts get-caller-identity --query Arn --output text")
    run(sb, "aws sts get-caller-identity --query Arn --output text")
    checks.check("assumed-role ARN with the sandbox ID as the session name",
                 ident.rc == 0 and ident.text.strip() == f"arn:aws:sts::{account_id()}:assumed-role/osb-sandbox/{sid}".replace(account_id(), "123456789012"))
    uri = f"s3://{bucket}/osb-test/{sid}"
    sh(sb, "head -c 5242880 /dev/urandom > /tmp/five.bin")
    up = run(sb, f"aws s3 cp /tmp/five.bin {uri}/five.bin --only-show-errors", timeout=180)
    down = run(sb, f"aws s3 cp {uri}/five.bin /tmp/five.back --only-show-errors", timeout=180)
    same = sh(sb, "cmp /tmp/five.bin /tmp/five.back && echo identical").text.strip()
    print(f"  upload/download 5 MiB: {same}")
    checks.check("S3 upload and download under Kata, bytes identical", up == 0 and down == 0 and same == "identical")
    sh(sb, f"aws s3 rm {uri}/ --recursive --only-show-errors")
    prompt = '[{"role":"user","content":[{"text":"Reply with exactly one word: ready"}]}]'
    converse = run(sb, f"aws bedrock-runtime converse --model-id {MODEL_ID} --messages '{prompt}' --inference-config '{{\"maxTokens\":20}}' "
                       "--query output.message.content[0].text --output text",
                   label=f"aws bedrock-runtime converse --model-id {MODEL_ID} --messages ...")
    checks.check("Bedrock Converse under Kata", converse == 0)

    heading("4. the addon, inside the Kata pod")
    lines = sigv4_events(log_since(sid, mark, "credential proxy: [sigv4]", wait=15))
    resigned = [l for l in lines if "re-signed" in l]
    bad = [l for l in lines if " refused " in l or " killed " in l]
    print(f"  addon lines: {len(lines)}; re-signed: {len(resigned)}; refused or killed: {len(bad)}")
    checks.check("the addon re-signed the requests and refused none", len(resigned) >= 4 and not bad)
    note("same credential flow, same addon: Kata changes where the sandbox's processes run (a VM), not how its requests are signed")
finally:
    cleanup()
    if on_kata:
        heading("5. back to runc")
        restored = switch("runc")
        checks.check("server switched back to runc", restored)
        if not restored:
            print("  THE SERVER MAY STILL BE ON KATA: run ../opensandbox-lifecycle/switch_runtime.sh runc", file=sys.stderr)
checks.finish()
