"""What an expired ECR token in the resume pull secret does on EKS: the snapshot image still pulls.

Resume starts the new pod from the snapshot image with imagePullSecrets: [opensandbox-snapshot-ecr]
(the controller's resumePullSecret, written by 00_snapshot_config.sh with a 12-hour ECR token).
The controller adds that same shared Secret to every resumed pod, so one sandbox's resume cannot be
pointed at a broken copy, and the shared Secret is never touched here (other parts pause and resume
with it). Instead the script makes the pull a resumed pod makes, with a broken copy:

1. Create a sandbox (no network policy: one container), write a marker file, pause it. That pushes
   a snapshot image to ECR with the valid token.
2. Copy the pull Secret with an invalid token in it: opensandbox-lifecycle-bad-token-<run>. The
   script checks that ECR rejects it (GET /v2/ answered 400 for this malformed token in our run,
   against 200 for the shared Secret's) and stops unless ECR answers 400, 401 or 403. An expired token is rejected too:
   either way the kubelet gets no usable credentials from the pull secret.
3. Run three pods that pull that snapshot image with imagePullPolicy: Always (a registry round trip
   with credentials every time, even when the layers are on the node) and print the marker:
     a. imagePullSecrets: [the broken copy], on the node that committed the snapshot
     b. no imagePullSecrets at all, on the same node
     c. imagePullSecrets: [the broken copy], on the other node
4. Print the node group role's managed policies: where the kubelet's own ECR credentials come from.

Exit 1 unless all three pods pulled the image and printed the marker. At the end it deletes the
pods, the copied Secret, the sandbox and its snapshot image (all carry example=opensandbox-lifecycle,
so 99_cleanup.py also finds them after an interrupted run).

Not tested here: a pause with an expired token, which needs the shared Secret broken. The release
docs (docs/guides/pause-resume.md, Troubleshooting) say the commit Job then logs `unauthorized`
and the snapshot fails.
"""

import base64
import json
import subprocess
import sys
import time
import uuid
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.sync import SandboxManagerSync

from common import (
    AWS_REGION,
    ECR_REGISTRY,
    EXAMPLE,
    SANDBOX_NAMESPACE,
    SNAPSHOT_REPO_NAME,
    SNAPSHOT_SECRET,
    connect_server,
    connection,
    delete_all_sandboxes,
    env_sh,
    heading,
    image,
    kubectl,
    own_metadata,
    record_created,
    registry_answer,
    snapshot_token_status,
)

SCRIPT = "02b_token_fallback"
meta = own_metadata(SCRIPT)
BAD_SECRET = f"opensandbox-lifecycle-bad-token-{meta['run']}"
LABELS = {"example": EXAMPLE, "script": SCRIPT, "run": meta["run"]}


def aws(*args: str) -> dict:
    done = subprocess.run(["aws", *args, "--region", AWS_REGION, "--output", "json"], capture_output=True, text=True,
                          timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"aws {args[0]} {args[1]} failed: {done.stderr.strip()[:160]}")
    return json.loads(done.stdout or "{}")


def wait_phase(sandbox_id: str, manager: SandboxManagerSync, target: str, limit: float = 300) -> None:
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if str(manager.get_sandbox_info(sandbox_id).status.state) == target:
            return
        time.sleep(1)
    raise RuntimeError(f"sandbox not {target} after {limit:.0f} s")


def bad_copy() -> int:
    """The shared Secret's shape (a kubernetes.io/dockerconfigjson for this registry) with a token ECR rejects."""
    config = json.dumps({"auths": {ECR_REGISTRY: {"auth": base64.b64encode(b"AWS:not-a-valid-ecr-token").decode()}}})
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
              "metadata": {"name": BAD_SECRET, "namespace": SANDBOX_NAMESPACE, "labels": LABELS},
              "data": {".dockerconfigjson": base64.b64encode(config.encode()).decode()}}
    kubectl("apply", "-f", "-", stdin=json.dumps(secret))
    answer = registry_answer(config)
    print(f"  Secret {BAD_SECRET}: a copy of {SNAPSHOT_SECRET}'s shape, with an invalid token; "
          f"ECR answers {answer} to it ({SNAPSHOT_SECRET}'s own token: {snapshot_token_status()[0]})")
    return answer


def pull_pod(name: str, ref: str, node: str, secret: str | None) -> dict:
    spec = {"nodeName": node, "restartPolicy": "Never", "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 0,
            "containers": [{"name": "c", "image": ref, "imagePullPolicy": "Always",
                            "command": ["sh", "-c", "echo marker: $(cat /root/marker.txt)"],
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}}}]}
    if secret:
        spec["imagePullSecrets"] = [{"name": secret}]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": SANDBOX_NAMESPACE,
                                                            "labels": LABELS}, "spec": spec}


def outcome(name: str, limit: float = 120) -> tuple[str, str, str]:
    """(phase or pull error reason, the pull event, the log) once the pod is done or its pull failed."""
    started = time.monotonic()
    phase, reason = "", ""
    while time.monotonic() - started < limit:
        pod = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", name, "-o", "json"))
        phase = pod["status"].get("phase", "")
        waiting = ((pod["status"].get("containerStatuses") or [{}])[0].get("state") or {}).get("waiting") or {}
        reason = waiting.get("reason", "")
        if phase in ("Succeeded", "Failed") or reason in ("ErrImagePull", "ImagePullBackOff"):
            break
        time.sleep(1)
    events = kubectl("-n", SANDBOX_NAMESPACE, "get", "events", "--field-selector", f"involvedObject.name={name}",
                     "-o", "jsonpath={range .items[*]}{.reason}|{.message}{\"\\n\"}{end}", check=False)
    pull = next((line.split("|", 1)[1] for line in events.splitlines() if line.startswith(("Pulled|", "Failed|"))), "")
    pull = pull.replace(ECR_REGISTRY + "/", "").split(" Image size")[0]
    log = kubectl("-n", SANDBOX_NAMESPACE, "logs", name, check=False).strip()
    return reason or phase, pull, log


def main() -> int:
    connect_server()
    answer, expires = snapshot_token_status()
    if answer != 200:
        sys.exit(f"ECR answers {answer or 'nothing'} to the token in {SNAPSHOT_SECRET}: run ./00_snapshot_config.sh first")
    print(f"the shared {SNAPSHOT_SECRET} is valid until {expires} and is not changed; sandboxes tagged run={meta['run']}")
    marker = uuid.uuid4().hex[:16]
    failures: list[str] = []
    sb = None
    tags: list[str] = []
    with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
        try:
            heading("1. a snapshot image to pull: create, write a marker, pause")
            sb = SandboxSync.create(image(), timeout=timedelta(minutes=30), ready_timeout=timedelta(seconds=180),
                                    metadata=meta, connection_config=connection("proxy"))
            record_created(sb.id)
            sb.commands.run(f"echo {marker} > /root/marker.txt")
            sb.pause()
            wait_phase(sb.id, manager, "Paused")
            owner = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "batchsandboxes.sandbox.opensandbox.io", sb.id,
                                       "-o", "json"))["metadata"]["uid"]
            snaps = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "sandboxsnapshots.sandbox.opensandbox.io",
                                       "-o", "json"))["items"]
            snap = next(s for s in snaps if any(r.get("uid") == owner for r in s["metadata"].get("ownerReferences", [])))
            container = snap["status"]["containers"][0]
            ref, source = container["imageUri"], snap["status"]["sourceNodeName"]
            tags = [ref.rsplit(":", 1)[1]]
            other = next(n for n in kubectl("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}").split()
                         if n != source)
            print(f"  sandbox {sb.id[:8]}... paused; marker {marker}; image "
                  f"{ref.replace(ECR_REGISTRY + '/', '').replace(sb.id, '<id>')}, committed on {source}")

            heading("2. a broken copy of the pull Secret")
            answer = bad_copy()
            if answer not in (400, 401, 403):
                raise RuntimeError(f"ECR answered {answer or 'nothing (a network error)'} to the broken copy's token, "
                                   "not a rejection (400/401/403): the test below would prove nothing")

            heading("3. pull the snapshot image the way a resumed pod does (imagePullPolicy: Always)")
            cases = [("a", f"pull secret {BAD_SECRET[:34]}..., the committing node", source, BAD_SECRET),
                     ("b", "no pull secret, the committing node", source, None),
                     ("c", f"pull secret {BAD_SECRET[:34]}..., the other node", other, BAD_SECRET)]
            for key, label, node, secret in cases:
                name = f"osb-lc-pull-{key}-{meta['run']}"
                kubectl("apply", "-f", "-", stdin=json.dumps(pull_pod(name, ref, node, secret)))
                result, pull, log = outcome(name)
                ok = result == "Succeeded" and log == f"marker: {marker}"
                print(f"  {'ok  ' if ok else 'FAIL'}  {key}. {label}: {result}; {log or 'no output'}")
                print(f"          {pull.replace(sb.id, '<id>') or 'no pull event'}")
                if not ok:
                    failures.append(f"case {key} ({label}): {result}")
                kubectl("-n", SANDBOX_NAMESPACE, "delete", "pod", name, "--wait=false", check=False)

            heading("4. where the kubelet's ECR credentials come from")
            role = aws("eks", "describe-nodegroup", "--cluster-name", env_sh("CLUSTER_NAME"), "--nodegroup-name",
                       env_sh("NODEGROUP_NAME"))["nodegroup"]["nodeRole"].rsplit("/", 1)[1]
            policies = aws("iam", "list-attached-role-policies", "--role-name", role)["AttachedPolicies"]
            print(f"  node group role {role.split('-eks-node-group')[0]}-...: "
                  + ", ".join(sorted(p["PolicyName"] for p in policies)))
        except Exception as error:  # SDK, kubectl or aws trouble: one line, then clean up
            failures.append(f"{type(error).__name__}: {str(error)[:240]}")
        finally:
            heading("clean up")
            kubectl("-n", SANDBOX_NAMESPACE, "delete", "pods,secrets", "-l", f"run={meta['run']},script={SCRIPT}",
                    "--wait=false", check=False)
            if sb is not None:
                sb.close()
            delete_all_sandboxes(metadata={"run": meta["run"]})
            if tags:
                aws("ecr", "batch-delete-image", "--repository-name", SNAPSHOT_REPO_NAME, "--image-ids",
                    json.dumps([{"imageTag": t} for t in tags]))
            print(f"  deleted the pods, {BAD_SECRET}, the sandbox and its snapshot image")
    if failures:
        print("\n" + "\n".join(f"  FAIL  {item}" for item in failures))
        return 1
    print("\nall three pulls worked with a broken or no pull secret: on EKS the kubelet falls back to the node "
          "role's ECR credentials, so an expired token in the resume pull secret does not break resume")
    return 0


if __name__ == "__main__":
    sys.exit(main())
