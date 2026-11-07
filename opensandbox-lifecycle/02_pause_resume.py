"""Pause a sandbox to ECR and resume it: what comes back and what does not.

Needs 00_snapshot_config.sh to have run in the last 12 hours (the ECR token in the Secret
opensandbox-snapshot-ecr; the commit Job cannot push without it).

The sandbox is created with an outbound network policy and the credential proxy, because the
Credential Vault lives in the egress sidecar that the policy adds. Before the pause it gets:
  /root/marker.txt       a file in the container's root filesystem
  /dev/shm/marker.txt    a file in memory (tmpfs)
  sleep 1000 &           a running process
  a Credential Vault entry: a placeholder apiKey for api.example.com (not a real credential)
Pause commits each container's root filesystem to an image in opensandbox-eks/snapshots and
deletes the pod; resume starts a new pod from those images. The script checks, and exits 1 if any
check fails:
  - the vault exists before the pause (otherwise the test proves nothing)
  - no pod while Paused, while the server still lists the sandbox as Paused
  - one snapshot image per container, all in the one repository
  - after resume: a new pod (another uid), every container on an opensandbox-eks/snapshots@sha256 image
  - /root/marker.txt came back with the same value; /dev/shm/marker.txt and the sleep did not
  - the vault is gone: the egress sidecar answers, and says 404 for the vault (a sidecar that does
    not answer is a failure, not "gone"); then the script creates it again, the way a trusted
    control plane has to after every resume, and checks it is there
It prints the pause and resume times and the size of each snapshot image. The sandbox is deleted
at the end; its snapshot images stay in ECR until 99_cleanup.py (this directory records the
sandbox id in results/created-sandboxes.txt, so 99_cleanup.py deletes this sandbox's images and
no one else's).
"""

import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from opensandbox import SandboxSync
from opensandbox.models.sandboxes import (
    Credential,
    CredentialBinding,
    CredentialProxyConfig,
    NetworkPolicy,
    NetworkRule,
    SandboxFilter,
)
from opensandbox.sync import SandboxManagerSync

from common import (
    AWS_REGION,
    ECR_REGISTRY,
    SANDBOX_NAMESPACE,
    SNAPSHOT_REPO_NAME,
    SNAPSHOT_SECRET,
    connect_server,
    connection,
    delete_all_sandboxes,
    ensure_port_forward,
    heading,
    image,
    kubectl,
    own_metadata,
    record_created,
    snapshot_token_status,
)

SCRIPT = "02_pause_resume"
SERVER_CREATE_TIMEOUT = 60      # sandbox_create_timeout_seconds; pause/resume are not bound by it
STATE_LIMIT = 600               # seconds we wait for Paused, and for Running again
VAULT_HOST = "api.example.com"  # placeholder destination; nothing is sent to it
# The image has no pgrep or ps: look for the sleep's pid (saved in a file) in /proc.
PROC = ('p=$(cat /root/sleep.pid); if grep -q sleep /proc/$p/cmdline 2>/dev/null; '
        'then echo "sleep pid $p running"; else echo "sleep pid $p not running"; fi')
meta = own_metadata(SCRIPT)
t0 = time.monotonic()


def say(text: str) -> None:
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] +{time.monotonic() - t0:5.1f}s  {text}", flush=True)


def vault_request() -> dict:
    """What a trusted control plane keeps, to create the vault again after every resume."""
    return {
        "credentials": [Credential(name="demo-api-key", source={"value": "placeholder-not-a-real-key"})],
        "bindings": [CredentialBinding(
            name="demo-api",
            match={"schemes": ["https"], "hosts": [VAULT_HOST], "methods": ["GET"], "paths": ["/v1/*"]},
            auth={"type": "apiKey", "name": "x-api-key", "credential": "demo-api-key"},
        )],
    }


def vault_state(sb: SandboxSync, wait: float = 0) -> tuple[str, str]:
    """("present" | "wiped" | "unreachable", detail). "wiped" only when the sidecar answers and says 404
    (no vault); a connection error, a timeout or a 5xx is "unreachable", retried for up to `wait` seconds."""
    deadline = time.monotonic() + wait
    while True:
        try:
            state = sb.credential_vault.get()
            names = [c.name for c in state.credentials]
            if not names:
                return "wiped", f"revision {state.revision}, no credentials"
            return "present", (f"revision {state.revision}, credentials {names}, "
                               f"bindings {[b.name for b in state.bindings]}")
        except Exception as error:
            status = getattr(error, "status_code", None)
            if status == 404:
                return "wiped", "404 from the egress sidecar: no vault"
            detail = f"{type(error).__name__}{f' {status}' if status else ''}: {str(error)[:100]}"
        if time.monotonic() >= deadline:
            return "unreachable", detail
        time.sleep(2)


def run(sb: SandboxSync, command: str) -> str:
    execution = sb.commands.run(command)
    return "".join(part.text for part in execution.logs.stdout).strip()


def snapshot_of(owner_uid: str) -> dict | None:
    """The SandboxSnapshot the controller made for this BatchSandbox (it owns it)."""
    items = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "sandboxsnapshots.sandbox.opensandbox.io", "-o", "json"))["items"]
    owned = [s for s in items if any(r.get("uid") == owner_uid for r in s["metadata"].get("ownerReferences", []))]
    return max(owned, key=lambda s: s["metadata"]["creationTimestamp"]) if owned else None


def wait_for(manager: SandboxManagerSync, sandbox_id: str, target: str, owner_uid: str) -> float:
    """Print each state of the sandbox (and of its snapshot) until it is `target`; seconds taken."""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < STATE_LIMIT:
        ensure_port_forward()
        info = manager.get_sandbox_info(sandbox_id)
        snap = snapshot_of(owner_uid) if target == "Paused" else None
        line = f"sandbox {info.status.state}" + (f" | snapshot {snap.get('status', {}).get('phase', 'Pending')}"
                                                   if snap else "")
        if line != last:
            say(line)
            last = line
        if info.status.state == target:
            return time.monotonic() - started
        if info.status.state == "Failed":
            raise RuntimeError(f"sandbox Failed: {info.status.reason}: {info.status.message}")
        time.sleep(0.5)
    raise RuntimeError(f"not {target} after {STATE_LIMIT} s")


def ecr_images(sandbox_id: str) -> tuple[list[dict], int]:
    """(this sandbox's snapshot images, number of images in the repository)."""
    done = subprocess.run(["aws", "ecr", "describe-images", "--region", AWS_REGION, "--repository-name",
                           SNAPSHOT_REPO_NAME, "--output", "json"], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"aws ecr describe-images failed: {done.stderr.strip()[:160]}")
    details = json.loads(done.stdout)["imageDetails"]
    mine = [d for d in details if any(t.startswith(sandbox_id + "-") for t in d.get("imageTags", []))]
    return mine, len(details)


def main() -> int:
    connect_server()
    answer, issued = snapshot_token_status()
    if not issued:
        sys.exit(f"Secret {SANDBOX_NAMESPACE}/{SNAPSHOT_SECRET} not found: run ./00_snapshot_config.sh first")
    if answer != 200:
        sys.exit(f"ECR answers {answer or 'nothing'} to the token in {SNAPSHOT_SECRET}: run ./00_snapshot_config.sh first")
    if datetime.fromisoformat(issued.replace("Z", "+00:00")) <= datetime.now(timezone.utc) + timedelta(minutes=10):
        sys.exit(f"the ECR token in {SNAPSHOT_SECRET} expires at {issued} (less than 10 min): "
                 "run ./00_snapshot_config.sh first")
    print(f"ECR token in {SNAPSHOT_SECRET} accepted by the registry, valid until {issued}; sandboxes tagged run={meta['run']}")
    failures: list[str] = []
    times: dict[str, float] = {}
    marker = uuid.uuid4().hex[:16]
    sb = None

    def check(ok: bool, text: str) -> None:
        print(f"  {'ok  ' if ok else 'FAIL'}  {text}", flush=True)
        if not ok:
            failures.append(text)

    with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
        try:
            heading("create, with a network policy (it adds the egress sidecar that holds the vault)")
            started = time.monotonic()
            sb = SandboxSync.create(
                image(), timeout=timedelta(minutes=30), ready_timeout=timedelta(seconds=180), metadata=meta,
                network_policy=NetworkPolicy(default_action="deny", egress=[NetworkRule(action="allow", target=VAULT_HOST)]),
                credential_proxy=CredentialProxyConfig(enabled=True), connection_config=connection("proxy"))
            record_created(sb.id)
            say(f"created {sb.id[:8]}... in {time.monotonic() - started:.1f} s")
            bs = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "batchsandboxes.sandbox.opensandbox.io", sb.id,
                                    "-o", "json"))
            owner_uid = bs["metadata"]["uid"]
            pod = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", f"{sb.id}-0", "-o", "json"))
            pod_uid = pod["metadata"]["uid"]
            containers = [c["name"] for c in pod["spec"]["containers"]]
            say("pod containers: " + ", ".join(c["name"] + " " + c["image"].split("/")[-1] for c in pod["spec"]["containers"]))

            heading("state before the pause")
            print("  " + "  ".join(run(sb, f"echo {marker} > /root/marker.txt; echo {marker} > /dev/shm/marker.txt; "
                                 "sleep 1000 >/dev/null 2>&1 & echo $! > /root/sleep.pid; sleep 0.2; "
                                 "echo \"rootfs: $(cat /root/marker.txt)  tmpfs: $(cat /dev/shm/marker.txt)\"; "
                                 + PROC).splitlines()))
            sb.credential_vault.create(**vault_request())
            kind, detail = vault_state(sb)
            check(kind == "present" and "demo-api-key" in detail, f"vault before the pause: {detail}")
            if failures:
                raise RuntimeError("no vault before the pause: the test below would prove nothing")

            heading("pause")
            started = time.monotonic()
            sb.pause()
            say(f"pause() accepted in {time.monotonic() - started:.1f} s")
            wait_for(manager, sb.id, "Paused", owner_uid)
            times["pause"] = time.monotonic() - started
            snap = snapshot_of(owner_uid) or {}
            job = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "job", f"{snap['metadata']['name']}-commit", "-o",
                                     "json", check=False) or "{}") if snap else {}
            js = job.get("status", {})
            if js.get("startTime") and js.get("completionTime"):
                took = (datetime.fromisoformat(js["completionTime"].replace("Z", "+00:00"))
                        - datetime.fromisoformat(js["startTime"].replace("Z", "+00:00"))).total_seconds()
                say(f"commit Job: {took:.0f} s on {snap['status'].get('sourceNodeName')}")
            pods_left = []
            for _ in range(120):
                pods_left = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"opensandbox.io/id={sb.id}", "-o",
                                    "name", check=False).split()
                if not pods_left:
                    break
                time.sleep(1)
            check(not pods_left, f"pods of the sandbox while Paused: {len(pods_left)}")
            listed = {i.id: str(i.status.state) for i in manager.list_sandbox_infos(
                SandboxFilter(metadata={"run": meta["run"]})).sandbox_infos}
            check(listed.get(sb.id) == "Paused", f"the server still lists it while Paused: {listed.get(sb.id, 'not listed')}")

            heading(f"ECR {SNAPSHOT_REPO_NAME}")
            mine, total = ecr_images(sb.id)
            for detail_ in mine:
                tag = detail_["imageTags"][0]
                print(f"  :{tag.replace(sb.id, '<id>')}   {detail_['imageSizeInBytes'] / 1e6:6.1f} MB   "
                      f"pushed {detail_['imagePushedAt'][11:19]}")
            tagged = sorted(t.removeprefix(sb.id + "-").split("-snap-")[0] for d in mine for t in d["imageTags"])
            check(tagged == sorted(containers), f"{len(mine)} image(s) for this sandbox, one per container "
                  f"({', '.join(containers)}); {total} in the repository, every snapshot in this one repository")
            for container in (snap.get("status") or {}).get("containers", []):
                print(f"  snapshot status: {container['containerName']} -> "
                      f"{container['imageUri'].replace(ECR_REGISTRY + '/', '').replace(sb.id, '<id>')}")
            times["sizes"] = sum(d["imageSizeInBytes"] for d in mine) / 1e6

            heading("resume")
            sb.close()
            started = time.monotonic()
            resumed: dict = {}

            def do_resume() -> None:
                try:
                    resumed["sb"] = SandboxSync.resume(sb.id, connection_config=connection("proxy"),
                                                       resume_timeout=timedelta(seconds=STATE_LIMIT))
                except Exception as error:
                    resumed["error"] = error

            thread = threading.Thread(target=do_resume)
            thread.start()
            wait_for(manager, sb.id, "Running", owner_uid)
            times["running"] = time.monotonic() - started
            thread.join(STATE_LIMIT)
            if "error" in resumed or "sb" not in resumed:
                raise RuntimeError(f"resume failed: {resumed.get('error', 'timed out')}")
            sb = resumed["sb"]
            times["resume"] = time.monotonic() - started
            say(f"SandboxSync.resume() returned (sandbox healthy) {times['resume']:.1f} s after the call")
            first = run(sb, "cat /root/marker.txt")
            times["command"] = time.monotonic() - started
            say(f"first command answered {times['command']:.1f} s after the call")
            pod = json.loads(kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", f"{sb.id}-0", "-o", "json"))
            check(pod["metadata"]["uid"] != pod_uid, "a new pod (its uid differs from the paused one's)")
            for c in pod["spec"]["containers"]:
                ref = c["image"].replace(ECR_REGISTRY + "/", "")
                check(ref.startswith(f"{SNAPSHOT_REPO_NAME}@sha256:"), f"container {c['name']} runs {ref}")
            pulls = [s.get("name") for s in pod["spec"].get("imagePullSecrets", [])]
            print(f"  imagePullSecrets on the resumed pod: {pulls or 'none'}")

            heading("what came back")
            check(first == marker, f"/root/marker.txt (root filesystem): {first or 'empty'}"
                  + ("  (same as before)" if first == marker else f"  (expected {marker})"))
            shm = run(sb, "cat /dev/shm/marker.txt 2>/dev/null || echo gone")
            check(shm == "gone", f"/dev/shm/marker.txt (memory) did not come back: {shm}")
            proc = run(sb, PROC)
            check(proc.endswith("not running"), f"the sleep process did not come back: {proc} (its pid file is in "
                  "the root filesystem, so the file came back)")
            policy = sb.get_egress_policy()
            print(f"  the egress sidecar answers: policy defaultAction {policy.default_action}, "
                  f"{len(policy.egress or [])} rule(s)")
            kind, detail = vault_state(sb, wait=60)
            check(kind == "wiped", f"Credential Vault did not come back: {detail}"
                  + ("" if kind == "wiped" else f" ({kind})"))
            sb.credential_vault.create(**vault_request())
            kind, detail = vault_state(sb)
            check(kind == "present" and "demo-api-key" in detail, f"re-created from the trusted side: {detail}")
        except Exception as error:  # SDK, kubectl or aws trouble: one line, then clean up
            failures.append(f"{type(error).__name__}: {str(error)[:240]}")
        finally:
            if sb is not None:
                sb.close()
            delete_all_sandboxes(metadata={"run": meta["run"]})

    heading("times")
    for key, label in (("pause", "pause() to Paused (commit and push included)"), ("running", "resume() to Running"),
                       ("resume", "resume() to a healthy sandbox"), ("command", "resume() to the first command")):
        if key in times:
            over = "  OVER the 60 s create timeout (it does not apply to resume)" if times[key] > SERVER_CREATE_TIMEOUT else ""
            print(f"  {label:<46} {times[key]:6.1f} s{over}")
    if "sizes" in times:
        print(f"  {'snapshot images, total compressed size':<46} {times['sizes']:6.1f} MB")
    if failures:
        print("\n" + "\n".join(f"  FAIL  {item}" for item in failures))
        return 1
    print("\nall checks passed: the pod was replaced by one running the snapshot images; the root filesystem came "
          "back; /dev/shm, the process and the vault did not, and the vault was created again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
