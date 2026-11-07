"""Remove what this directory's scripts leave behind, and nothing else; then turn Container
Insights off.

  1. every sandbox with the metadata example=opensandbox-stress (all of them, any state)
  2. the SigV4 rung's sandboxes, which part 5's broker made: metadata
     example=opensandbox-aws-credentials and script=stress-05_egress_ladder (only those)
  3. the BatchSandboxes 03_latency.py made by hand (label osb-stress-claim=true)
  4. the Pool osb-stress-pool and its pods
  5. 06_control_plane.py's Job and ConfigMap osb-stress-api-load (label example=opensandbox-stress),
     05_egress_ladder.py's echo server echo-stress (Deployment, Service, ConfigMap) and
     02_density.py's node-side VM sampler pods (each by its own labels)
  6. the snapshot images of this directory's sandboxes in ECR opensandbox-eks/snapshots: the ids in
     results/created-sandboxes.txt, and only once the sandbox no longer exists (other parts' images,
     untagged ones and those of a sandbox still Paused stay)
  7. the server's settings back to runc, the base egress image and dns+nft (with the gateway), if a
     killed script left them changed; checked with server_config.py --show --json
  8. Container Insights off: ./01_metrics.sh off (the same checked plan as turning it on);
     --keep-metrics skips this
  9. the log group /aws/containerinsights/<cluster>/performance, where the agent sends its
     performance events (the metrics stay in CloudWatch Metrics); --keep-log-group keeps it
 10. a scan of results/ and charts/, which are published: no account ID, ARN with one, IP address,
     node name with an IP or home directory path may be in them
It keeps part 4's echo server, part 5's broker and image, and part 3's snapshot repository, Secret and
controller settings. Exit 1 if anything of ours is left or the scan finds something.
"""

from __future__ import annotations

import json
import subprocess
import sys

from checks import BASELINE, deletable_images, helm_problems, helm_state, server_problems
from measure import scan_published
from common import (
    AWS_REGION,
    BATCHSANDBOXES,
    CLUSTER_NAME,
    ECHO_SELECTOR,
    CREATED_LOG,
    EXAMPLE,
    HERE,
    OWN,
    POOL,
    POOL_NAME,
    RESULTS,
    SANDBOX_NAMESPACE,
    SNAPSHOT_REPO_NAME,
    SYSTEM_NAMESPACE,
    VMM_SELECTOR,
    Plan,
    census,
    cli,
    connect_server,
    delete_all_sandboxes,
    echo_down,
    helm_history,
    heading,
    kubectl,
    restore_baseline,
    server_show,
    show,
    wait_no_pods,
)

LADDER_BROKER = {"example": "opensandbox-aws-credentials", "script": "stress-05_egress_ladder"}
CLAIM_LABEL = "osb-stress-claim=true"
LOG_GROUP = f"/aws/containerinsights/{CLUSTER_NAME}/performance"


def arguments(parser) -> None:
    parser.add_argument("--keep-metrics", action="store_true", help="leave Container Insights on")
    parser.add_argument("--keep-log-group", action="store_true", help=f"keep {LOG_GROUP}")


def aws(*args: str, check: bool = True) -> dict:
    done = subprocess.run(["aws", *args, "--region", AWS_REGION, "--output", "json"], capture_output=True, text=True,
                          timeout=180)
    if done.returncode != 0:
        if not check:
            return {"error": done.stderr}
        code = done.stderr.split("(", 1)[1].split(")", 1)[0] if "(" in done.stderr else show(done.stderr.strip()[:120])
        raise RuntimeError(f"aws {args[0]} {args[1]}: {code}")
    return json.loads(done.stdout or "{}")


def main() -> int:
    args = cli(__doc__, arguments)
    plan = Plan("99_cleanup.py: this directory's sandboxes, Pool, Job, hand-made BatchSandboxes and snapshot images; "
                "the server baseline; Container Insights off")
    plan.step("server_config.py --baseline (always; no port-forward needed), then read back with --show --json "
              "and Helm's history", 1)
    plan.step("delete every sandbox with example=opensandbox-stress (server API)", 1)
    plan.step("delete the ladder's broker-made sandboxes (example=opensandbox-aws-credentials, script=stress-05_egress_ladder)", 0.5)
    plan.step(f"kubectl delete {BATCHSANDBOXES} -l {CLAIM_LABEL}; the Pool {POOL_NAME}; the API load Job, the latency "
              "client pod and their ConfigMaps; "
              f"echo-stress ({ECHO_SELECTOR}); VM sampler pods ({VMM_SELECTOR})", 1)
    plan.step(f"ECR {SNAPSHOT_REPO_NAME}: delete the images of the sandboxes in results/created-sandboxes.txt that no longer exist", 0.5)
    if not args.keep_metrics:
        plan.step("./01_metrics.sh off (plan, check: only the add-on's resources are deleted; apply)", 3)
    if not args.keep_log_group:
        plan.step(f"aws logs delete-log-group {LOG_GROUP} (if it exists)", 0.1)
    plan.step("scan results/ and charts/ (published) for an account ID, ARN, IP, node name or home path")
    plan.print()
    if args.dry_run:
        return 0

    steps: dict[str, bool] = {}

    def step(name: str, fn) -> None:
        """Run one cleanup step; a failure (an error, or a helper's exit such as connect_server()'s)
        is counted and the next step runs. Ctrl-C, SIGTERM and SIGHUP still stop the script."""
        heading(name)
        try:
            steps[name] = fn() is not False
        except KeyboardInterrupt:
            raise
        except SystemExit as error:
            if isinstance(error.code, int) and error.code >= 128:   # a signal (common's handler)
                raise
            print(f"  FAIL  {show(str(error.code))[:240]}")
            steps[name] = False
        except Exception as error:  # noqa: BLE001 - every step is attempted whatever another did
            print(f"  FAIL  {show(f'{type(error).__name__}: {error}')[:240]}")
            steps[name] = False

    def server() -> bool:
        # First, and without the port-forward: server_config.py works through kubectl exec. After a
        # failed or interrupted change the API may be down, and this is the step that repairs it.
        left = restore_baseline(force=True)
        for item in left:
            print(f"  FAIL  {show(item)}")
        if not left:
            print(f"  ok    {json.dumps(BASELINE)}; Helm's latest revision is deployed")
        return not left

    def sandboxes() -> bool:
        connect_server()
        print(f"  deleted {delete_all_sandboxes(metadata=dict(OWN))} with metadata example={EXAMPLE}")
        print(f"  deleted {delete_all_sandboxes(metadata=dict(LADDER_BROKER))} of the ladder's SigV4 rung (broker-made)")

    def objects() -> bool:
        ok = True
        out = kubectl("-n", SANDBOX_NAMESPACE, "delete", BATCHSANDBOXES, "-l", CLAIM_LABEL, "--ignore-not-found",
                      "--wait=true", "--timeout=120s", check=False).strip()
        print(f"  BatchSandboxes: {out or 'none'}")
        out = kubectl("-n", SANDBOX_NAMESPACE, "delete", POOL, "--ignore-not-found", "--wait=true", "--timeout=180s",
                      check=False).strip()
        print(f"  Pool: {out or 'not found'}")
        if not wait_no_pods(f"sandbox.opensandbox.io/pool-name={POOL_NAME}", timeout=300):
            print("  FAIL  pool pods are still there after 5 minutes")
            ok = False
        out = kubectl("-n", SYSTEM_NAMESPACE, "delete", "pods,jobs,configmaps", "-l", f"example={EXAMPLE}",
                      "--ignore-not-found", "--cascade=foreground", "--wait=true", "--timeout=120s", check=False).strip()
        print(f"  API load Job, latency client and their ConfigMaps: "
              f"{'none' if not out or out.startswith('No resources') else out.replace(chr(10), '; ')}")
        echo_down()
        out = kubectl("-n", SANDBOX_NAMESPACE, "delete", "pods", "-l", VMM_SELECTOR, "--ignore-not-found", "--wait=true",
                      "--timeout=60s", check=False).strip()
        print(f"  echo-stress deleted; VM sampler pods: {'none' if not out else out.replace(chr(10), '; ')}")
        return ok

    def images() -> bool:
        try:
            found = aws("ecr", "list-images", "--repository-name", SNAPSHOT_REPO_NAME).get("imageIds", [])
        except RuntimeError as error:
            if "RepositoryNotFound" not in str(error):
                raise
            print("  no repository (part 3's 00_snapshot_config.sh has not run)")
            return True
        connect_server()
        existing = {s["id"] for s in census()}
        existing |= set(kubectl("-n", SANDBOX_NAMESPACE, "get", BATCHSANDBOXES, "-o",
                                "jsonpath={.items[*].metadata.name}", check=False).split())
        ours = set(CREATED_LOG.read_text().split()) if CREATED_LOG.is_file() else set()
        doomed, kept, others = deletable_images(found, ours, existing)
        failed = 0
        for first in range(0, len(doomed), 100):
            result = aws("ecr", "batch-delete-image", "--repository-name", SNAPSHOT_REPO_NAME,
                         "--image-ids", json.dumps(doomed[first:first + 100]))
            failed += len(result.get("failures", []))
        print(f"  deleted {len(doomed) - failed} image(s) of our deleted sandboxes; kept {kept} of ours whose sandbox "
              f"still exists; left {others} that are not ours")
        if CREATED_LOG.is_file():
            tags = [i.get("imageTag", "") for i in aws("ecr", "list-images", "--repository-name",
                                                       SNAPSHOT_REPO_NAME).get("imageIds", [])]
            keep = [i for i in CREATED_LOG.read_text().split() if i in existing or any(t.startswith(i + "-") for t in tags)]
            CREATED_LOG.write_text("".join(i + "\n" for i in keep))
        return failed == 0

    def metrics_off() -> bool:
        done = subprocess.run([str(HERE / "01_metrics.sh"), "off"], cwd=str(HERE))
        if done.returncode != 0:
            print("  FAIL  01_metrics.sh off did not finish; run it again")
        return done.returncode == 0

    def log_group() -> bool:
        groups = aws("logs", "describe-log-groups", "--log-group-name-prefix", LOG_GROUP).get("logGroups", [])
        group = next((g for g in groups if g.get("logGroupName") == LOG_GROUP), None)
        if group:
            aws("logs", "delete-log-group", "--log-group-name", LOG_GROUP)
            print(f"  deleted ({group.get('storedBytes', 0) / 1e6:.1f} MB stored)")
        else:
            print("  not found")

    def what_is_left() -> bool:
        problems = 0
        try:
            connect_server()
            mine = [s for s in census() if (s["metadata"].get("example") == EXAMPLE
                                            or s["metadata"].get("script") == LADDER_BROKER["script"])]
            listed = f"sandboxes of ours still listed: {len(mine)}"
        except BaseException as error:  # noqa: BLE001
            mine, listed = [None], f"sandboxes: could not ask the server ({show(str(error))[:120]})"
        claims = kubectl("-n", SANDBOX_NAMESPACE, "get", BATCHSANDBOXES, "-l", CLAIM_LABEL, "-o", "name", check=False).split()
        pool = kubectl("-n", SANDBOX_NAMESPACE, "get", POOL, "-o", "name", check=False).strip()
        agent = kubectl("-n", "amazon-cloudwatch", "get", "daemonset", "cloudwatch-agent", "-o", "name", check=False).strip()
        echo = kubectl("-n", SANDBOX_NAMESPACE, "get", "deployments,services,configmaps", "-l", ECHO_SELECTOR, "-o",
                       "name", check=False).split()
        samplers = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", VMM_SELECTOR, "-o", "name", check=False).split()
        system = kubectl("-n", SYSTEM_NAMESPACE, "get", "pods,jobs,configmaps", "-l", f"example={EXAMPLE}", "-o", "name",
                         check=False).split()
        leaked = scan_published(RESULTS, HERE / "charts")
        state = server_show()
        helm = helm_problems(helm_state(helm_history()))
        for ok, text in ((not mine, listed),
                         (not claims, f"hand-made BatchSandboxes: {len(claims)}"),
                         (not pool, f"Pool {POOL_NAME}: {'present' if pool else 'gone'}"),
                         (not echo, f"echo-stress objects: {len(echo)}"),
                         (not samplers, f"VM sampler pods: {len(samplers)}"),
                         (not system, f"objects of ours in {SYSTEM_NAMESPACE}: {len(system)}"),
                         (not leaked, "results/ and charts/ scanned: " + (
                             "; ".join(f"{name}: {kind}" for name, kind in leaked[:5]) if leaked else
                             "no account ID, ARN, IP, endpoint, load balancer, node name or home path")),
                         (args.keep_metrics or not agent, "Container Insights agent: " + ("running" if agent else "gone")),
                         (not server_problems(state, BASELINE) and not helm,
                          f"server: {state['runtime']}, {state['egress_image']}, {state['egress_mode']}, "
                          f"{state['ingress']}" + (f"; {helm[0]}" if helm else ""))):
            print(f"  {'ok  ' if ok else 'FAIL'}  {show(text)}")
            problems += not ok
        return problems == 0

    # Each step runs whatever the others did; the server first (it needs no port-forward), Container
    # Insights off always attempted.
    step("the server's settings (server_config.py --baseline, no port-forward needed)", server)
    step("sandboxes", sandboxes)
    step("hand-made BatchSandboxes, the Pool, the API load Job, the latency client, the echo server, the VM samplers",
         objects)
    step(f"ECR {SNAPSHOT_REPO_NAME} (images of this directory's sandboxes only)", images)
    if not args.keep_metrics:
        step("Container Insights off (./01_metrics.sh off)", metrics_off)
    if not args.keep_log_group:
        step(f"log group {LOG_GROUP}", log_group)
    step("what is left", what_is_left)
    failed = [name for name, ok in steps.items() if not ok]
    if failed:
        print(f"\n{len(failed)} step(s) failed: " + "; ".join(failed))
        return 1
    print("\nall cleanup steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
