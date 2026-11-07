"""Remove what this directory's scripts leave behind, and nothing else.

- every sandbox with the metadata example=opensandbox-lifecycle (all of this directory's scripts
  tag theirs). The project's resume benchmark (03) does not use that tag; 03_resume_benchmark.sh
  deletes any sandbox its own run left, by the ids in its own results file.
- the Pool osb-pool (04_pools.py) and its pods
- pods and Secrets labelled example=opensandbox-lifecycle (06_failure.py's demo pod,
  02b_token_fallback.py's pull pods and its broken Secret copy), and the cordon on a node
  06_failure.py marked, if a script was killed before it could undo it
- snapshot images in ECR opensandbox-eks/snapshots of this directory's sandboxes only: the ids
  recorded in results/created-sandboxes.txt (02, 02b) and in results/resume-uncontrolled-*.jsonl
  (03). An image is deleted only when its sandbox no longer exists, neither listed by the server
  (any state, Paused included) nor as a BatchSandbox. Other parts' images, untagged images and
  images of sandboxes that still exist are left alone.

It keeps the ECR repository, the Secret opensandbox-snapshot-ecr and the controller's snapshot
settings (part 6 uses them; ../opensandbox-eks/99_teardown.sh removes everything at the end).
Needs the server (it starts a port-forward if none is running). Exit 1 if anything of ours is left.
"""

import json
import subprocess
import sys
import time

from opensandbox.sync import SandboxManagerSync

from common import (
    AWS_REGION,
    CREATED_LOG,
    EXAMPLE,
    HERE,
    OWN,
    POOL_NAME,
    SANDBOX_NAMESPACE,
    SNAPSHOT_REPO_NAME,
    connect_server,
    connection,
    delete_all_sandboxes,
    kubectl,
    list_sandbox_ids,
)

CORDON_MARK = "opensandbox-lifecycle/cordoned-by"


def aws(*args: str) -> dict:
    done = subprocess.run(["aws", *args, "--region", AWS_REGION, "--output", "json"], capture_output=True, text=True,
                          timeout=120)
    if done.returncode != 0:
        code = done.stderr.split("(", 1)[1].split(")", 1)[0] if "(" in done.stderr else done.stderr.strip()[:120]
        raise RuntimeError(f"aws {args[0]} {args[1]}: {code}")
    return json.loads(done.stdout or "{}")


def our_sandbox_ids() -> set[str]:
    """Every sandbox id this directory recorded: 02/02b's log and the benchmark's own trial records."""
    ids = set(CREATED_LOG.read_text().split()) if CREATED_LOG.is_file() else set()
    for path in sorted((HERE / "results").glob("resume-uncontrolled-*.jsonl")):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record.get("type") == "trial" and record.get("sandbox_id"):
                ids.add(record["sandbox_id"])
    return ids


def deletable_images(images: list[dict], ours: set[str], existing: set[str]) -> tuple[list[dict], int, int]:
    """(image ids to delete, ours kept because the sandbox exists, images not ours).

    An image is ours when its tag starts with "<one of our sandbox ids>-" (the imageURITemplate in
    values/controller-snapshot.yaml puts the sandbox id first). Untagged images are never ours."""
    delete, kept, others = [], 0, 0
    for item in images:
        tag = item.get("imageTag", "")
        owner = next((sandbox_id for sandbox_id in ours if tag.startswith(sandbox_id + "-")), None) if tag else None
        if owner is None:
            others += 1
        elif owner in existing:
            kept += 1
        else:
            delete.append({"imageDigest": item["imageDigest"], "imageTag": tag})
    return delete, kept, others


def main() -> int:
    connect_server()
    problems = 0

    print("sandboxes")
    print(f"  deleted {delete_all_sandboxes(metadata=dict(OWN))} with metadata example={EXAMPLE}")

    print(f"Pool {POOL_NAME}")
    out = kubectl("-n", SANDBOX_NAMESPACE, "delete", "pools.sandbox.opensandbox.io", POOL_NAME, "--ignore-not-found",
                  "--wait=true", "--timeout=120s", check=False).strip()
    print(f"  {out or 'not found'}")
    pods: list[str] = []
    for _ in range(60):
        pods = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"sandbox.opensandbox.io/pool-name={POOL_NAME}",
                       "-o", "name", check=False).split()
        if not pods:
            break
        time.sleep(2)
    print(f"  pool pods left: {len(pods)}")
    problems += bool(pods)

    print(f"pods and Secrets labelled example={EXAMPLE} (06's demo pod, 02b's pull pods and Secret copy)")
    # Sandbox metadata becomes pod labels, so this selector also matches those two scripts' sandbox pods;
    # their sandboxes were deleted above, so what is left here is the demo pod, the pull pods and the
    # Secret copy (and any sandbox pod still terminating).
    out = kubectl("-n", SANDBOX_NAMESPACE, "delete", "pods,secrets", "-l",
                  f"example={EXAMPLE},script in (06_failure,02b_token_fallback)", "--ignore-not-found",
                  "--wait=false", check=False).strip()
    print(f"  {'none' if not out or out.startswith('No resources') else out.replace(chr(10), '; ')}")
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    for node in nodes:
        name = node["metadata"]["name"]
        if CORDON_MARK in (node["metadata"].get("annotations") or {}):
            kubectl("uncordon", name)
            kubectl("annotate", "node", name, f"{CORDON_MARK}-")
            print(f"  uncordoned {name} (06_failure.py had cordoned it)")
    print("  nodes: " + ", ".join(f"{n['metadata']['name']} {'cordoned' if n['spec'].get('unschedulable') else 'schedulable'}"
                                  for n in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]))

    print(f"ECR {SNAPSHOT_REPO_NAME} (images of this directory's sandboxes only)")
    try:
        images = aws("ecr", "list-images", "--repository-name", SNAPSHOT_REPO_NAME).get("imageIds", [])
    except RuntimeError as error:
        if "RepositoryNotFound" not in str(error):
            raise
        print("  no repository (00_snapshot_config.sh has not run)")
        images = None
    if images is not None:
        with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
            existing = {sandbox_id for sandbox_id, _ in list_sandbox_ids(manager)}
        existing |= set(kubectl("-n", SANDBOX_NAMESPACE, "get", "batchsandboxes.sandbox.opensandbox.io", "-o",
                                "jsonpath={.items[*].metadata.name}").split())
        ours = our_sandbox_ids()
        doomed, kept, others = deletable_images(images, ours, existing)
        failed = 0
        for start in range(0, len(doomed), 100):
            result = aws("ecr", "batch-delete-image", "--repository-name", SNAPSHOT_REPO_NAME,
                         "--image-ids", json.dumps(doomed[start:start + 100]))
            failed += len(result.get("failures", []))
        print(f"  deleted {len(doomed) - failed} image(s) of our deleted sandboxes; kept {kept} of ours whose sandbox "
              f"still exists; left {others} that are not ours")
        problems += failed
        if CREATED_LOG.is_file():  # forget the ids that have nothing left
            left_tags = [i.get("imageTag", "") for i in aws("ecr", "list-images", "--repository-name",
                                                            SNAPSHOT_REPO_NAME).get("imageIds", [])]
            keep = [i for i in CREATED_LOG.read_text().split()
                    if i in existing or any(t.startswith(i + "-") for t in left_tags)]
            CREATED_LOG.write_text("".join(i + "\n" for i in keep))

    with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
        mine = list_sandbox_ids(manager, metadata=dict(OWN))
    print(f"sandboxes with example={EXAMPLE} still listed: {len(mine)}")
    problems += bool(mine)
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:  # kubectl or aws failed: one line
        sys.exit(f"cleanup stopped: {error}")
