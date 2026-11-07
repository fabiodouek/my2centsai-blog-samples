"""The first sandbox on EKS: create times (cold, warm, five warm), then a command and a file
round trip through the server proxy and through the ingress gateway, then `osb sandbox list`.

Needs the port-forward (see common.py):
  kubectl -n opensandbox-system port-forward svc/opensandbox-server 18080:80

Cold means the python image is not on the node. The script removes it from every node first,
with `crictl rmi` in a privileged debug pod (kubectl debug --profile=sysadmin). Set
SKIP_COLD_PREP=1 to skip that and time whatever the nodes have cached.

The script kills only the sandboxes it created (they carry the metadata run=<id>); sandboxes of
other scripts, or of the console, are left alone. Exit code 1 if any step fails.
"""

import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.models.filesystem import WriteEntry

from common import (
    DOMAIN,
    HERE,
    api_key,
    connection,
    delete_all_sandboxes,
    env_sh,
    image,
    own_metadata,
    require_server,
)

SCRIPT = "07_first_sandbox"
DEBUG_IMAGE = "public.ecr.aws/amazonlinux/amazonlinux:2023"  # same as 01_cluster.sh
SANDBOX_NAMESPACE = "opensandbox"
SERVER_CREATE_TIMEOUT = 60  # sandbox_create_timeout_seconds, the server's default
READY_TIMEOUT = timedelta(seconds=180)  # the SDK's own wait; the server limit is what we report on
EXECD_PORT = 44772
MAX_WARM_TRIES = 14

require_server()
CONTEXT = env_sh("KUBE_CONTEXT")
IMAGE = image()
META = own_metadata(SCRIPT)
RUN_ID = META["run"]
failures: list[str] = []
rows: list[tuple[str, float, str, str]] = []  # (case, seconds, node, note)
script_start = time.monotonic()
warm: list[float] = []
first_sandbox = None


def kubectl(*args: str, check: bool = True) -> str:
    """kubectl pinned to the cluster's context. Returns stdout."""
    done = subprocess.run(["kubectl", "--context", CONTEXT, *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def heading(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def fail(text: str) -> None:
    failures.append(text)
    print(f"  FAIL  {text}", flush=True)


def node_names() -> list[str]:
    return kubectl("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}").split()


def pod_node(sandbox_id: str) -> str:
    """The node a sandbox's pod runs on, by the opensandbox.io/id label."""
    node = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"opensandbox.io/id={sandbox_id}",
                   "-o", "jsonpath={.items[0].spec.nodeName}", check=False).strip()
    return node or "?"


def remove_image_from_nodes() -> None:
    """Delete the python image from every node, in a privileged debug pod (kubectl debug
    --profile=sysadmin mounts the node's root at /host). The EKS AL2023 AMI has no crictl, so this
    uses the node's own ctr in containerd's k8s.io namespace, and removes the tag and the digest
    reference (with --sync, so the layers go too). Each debug pod is deleted when it finishes;
    other node-debugger pods are not touched."""
    repo = IMAGE.rsplit(":", 1)[0]
    script = (f'refs=$(ctr -n k8s.io images ls -q 2>/dev/null | grep -F "{repo}"); '
              'if [ -z "$refs" ]; then echo NOT_CACHED; else ctr -n k8s.io images rm --sync $refs >/dev/null 2>&1 && echo REMOVED; fi')
    for node in node_names():
        started = time.monotonic()
        done = subprocess.run(
            ["kubectl", "--context", CONTEXT, "debug", "-n", "default", f"node/{node}", "--profile=sysadmin",
             f"--image={DEBUG_IMAGE}", "--", "chroot", "/host", "sh", "-c", script],
            capture_output=True, text=True, timeout=120)
        pod = ""
        for line in done.stdout.splitlines():
            if line.startswith("Creating debugging pod "):
                pod = line.split()[3]
        if not pod:
            fail(f"{node}: kubectl debug failed: {(done.stderr or done.stdout).strip()[:200]}")
            continue
        phase = ""
        for _ in range(60):
            phase = kubectl("-n", "default", "get", "pod", pod, "-o", "jsonpath={.status.phase}", check=False).strip()
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        text = kubectl("-n", "default", "logs", pod, check=False).strip().replace("\n", " ")
        kubectl("-n", "default", "delete", "pod", pod, "--wait=false", check=False)
        if phase == "Succeeded" and "REMOVED" in text:
            verdict = "image removed"
        elif phase == "Succeeded" and "NOT_CACHED" in text:
            verdict = "image was not cached"
        else:
            verdict = f"could not remove the image ({phase or 'no status'}): {text[:160]}"
        print(f"  {node}: {verdict} ({time.monotonic() - started:.0f} s), debug pod deleted", flush=True)
        if verdict.startswith("could not"):
            fail(f"{node}: {verdict}")


def pull_note(sandbox_id: str) -> str:
    """What kubelet did with the python image for this sandbox's pod, from the pod's events."""
    messages = kubectl("-n", SANDBOX_NAMESPACE, "get", "events", "--field-selector",
                       f"involvedObject.name={sandbox_id}-0,reason=Pulled",
                       "-o", "jsonpath={range .items[*]}{.message}{\"\\n\"}{end}", check=False)
    for message in messages.splitlines():
        if "/python:" not in message:
            continue
        if "already present" in message:
            return "image already on the node"
        if "Successfully pulled" in message:
            took = message.split(" in ", 1)[1].split(" ", 1)[0] if " in " in message else "?"
            size = message.split("Image size: ", 1)[1].split(" ", 1)[0] if "Image size: " in message else ""
            return f"kubelet pulled the image in {took}" + (f" ({int(size) / 1e6:.0f} MB)" if size.isdigit() else "")
    return "no pull event found"


def timed_create(case: str) -> tuple[SandboxSync, str] | None:
    """Create a sandbox from the ECR image, record how long create() took (it returns when the
    sandbox answers its health check), and return it with its node. None if the create failed."""
    started = time.monotonic()
    try:
        sb = SandboxSync.create(IMAGE, timeout=timedelta(minutes=10), ready_timeout=READY_TIMEOUT,
                                metadata=META, connection_config=connection("proxy"))
    except Exception as error:  # report one line, keep going to cleanup
        took = time.monotonic() - started
        rows.append((case, took, "-", f"FAILED after {took:.0f} s"))
        fail(f"{case}: create failed after {took:.1f} s: {str(error)[:200]}")
        return None
    took = time.monotonic() - started
    node = pod_node(sb.id)
    flag = took > SERVER_CREATE_TIMEOUT
    rows.append((case, took, node, pull_note(sb.id) + (", OVER THE LIMIT" if flag else "")))
    print(f"  {case:<22} {took:6.1f} s   node {node}   {rows[-1][3]}", flush=True)
    return sb, node


def kill(sb: SandboxSync) -> None:
    try:
        sb.kill()
    finally:
        sb.close()


def command_and_file(label: str, sandbox_id: str, via: str) -> bool:
    """Run a command and write, read and compare a file through one path. Prints the endpoint used."""
    try:
        sb = SandboxSync.connect(sandbox_id, connection_config=connection(via), connect_timeout=timedelta(seconds=30))
    except Exception as error:
        fail(f"{label}: could not connect: {str(error)[:200]}")
        return False
    try:
        endpoint = sb.get_endpoint(EXECD_PORT).endpoint
        print(f"  {label}: endpoint {endpoint}")
        execution = sb.commands.run("echo hello from $(hostname); python3 --version")
        out = "".join(part.text for part in execution.logs.stdout).strip().replace("\n", " | ")
        print(f"  {label}: command -> {out}")
        text = f"round trip through the {label}\n"
        sb.files.write_files([WriteEntry(path="/tmp/hello.txt", data=text, mode=644)])
        back = sb.files.read_file("/tmp/hello.txt")
        if back != text:
            fail(f"{label}: file came back different: {back!r}")
            return False
        print(f"  {label}: file round trip ok")
        return True
    except Exception as error:
        fail(f"{label}: {str(error)[:200]}")
        return False
    finally:
        sb.close()


def osb_list() -> None:
    """osb sandbox list, with the osb CLI from PyPI (uvx) and a config file written here (gitignored)."""
    config = HERE / "osb.toml"
    config.write_text(f'[connection]\ndomain = "{DOMAIN}"\nprotocol = "http"\n')  # the key goes in the environment
    env = {**os.environ, "OPEN_SANDBOX_API_KEY": api_key()}
    cmd = ["uvx", "--from", "opensandbox-cli==1.2.0", "osb", "--config", str(config), "sandbox", "list", "--metadata", f"run={RUN_ID}"]
    print(f"  $ osb sandbox list --metadata run={RUN_ID}   (domain {DOMAIN} from osb.toml)")
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    print("\n".join("  " + line for line in (done.stdout + done.stderr).rstrip().splitlines()))
    if done.returncode != 0:
        fail(f"osb sandbox list exited {done.returncode}")


def prepare_cold() -> None:
    if os.environ.get("SKIP_COLD_PREP") == "1":
        print("  SKIP_COLD_PREP=1: not removing the image; the first create is cold only if the nodes lack it")
        return
    if shutil.which("kubectl") is None:
        sys.exit("kubectl is not installed (see README.md)")
    remove_image_from_nodes()


try:
    print(f"image   {IMAGE}")
    print(f"server  {DOMAIN} (port-forward), sandboxes tagged run={RUN_ID}")

    heading("1. cold create: the image is on no node")
    prepare_cold()
    cached_nodes: set[str] = set()
    made = timed_create("cold")
    if made:
        sandbox, node = made
        cached_nodes.add(node)
        first_id = sandbox.id
        # This sandbox stays up for the proxy and gateway tests below.
        first_sandbox = sandbox
    else:
        first_sandbox = None

    heading("2. warm creates: the image is on the node")
    # A create can land on the node that does not have the image yet; that one is a second cold
    # start (it is reported as such and caches the image there). Keep going until five land on a
    # node that had it.
    tries = 0
    while len(warm) < 5 and tries < MAX_WARM_TRIES and first_sandbox is not None:
        tries += 1
        made = timed_create(f"warm {len(warm) + 1}")
        if not made:
            break
        sandbox, node = made
        if node in cached_nodes:
            warm.append(rows[-1][1])
        else:
            cached_nodes.add(node)
            case, seconds, n, note = rows[-1]
            rows[-1] = ("cold, 2nd node", seconds, n, note)
            print(f"    (landed on {node}, which did not have the image: counted as a cold create)")
        kill(sandbox)
    if len(warm) < 5 and first_sandbox is not None:
        fail(f"only {len(warm)} of 5 warm creates after {tries} tries")

    heading("3. command and file round trip, through the server proxy and through the gateway")
    if first_sandbox is None:
        print("  skipped: no sandbox")
    else:
        print(f"  sandbox {first_id}")
        command_and_file("server proxy", first_id, "proxy")
        command_and_file("gateway", first_id, "gateway")

    heading("4. osb sandbox list")
    try:
        osb_list()
    except (OSError, subprocess.SubprocessError) as error:  # uvx missing, or it timed out
        fail(f"osb sandbox list: {type(error).__name__}: {str(error)[:160]}")
except Exception as error:  # SDK, httpx or kubectl trouble mid-run: report one line, then clean up below
    fail(f"stopped by {type(error).__name__}: {str(error)[:200]}")
finally:
    heading("5. clean up (only the sandboxes of this run)")
    try:
        print(f"  deleted {delete_all_sandboxes(metadata={'run': RUN_ID})}")
    except (SystemExit, Exception) as error:
        fail(f"clean up: {error}")
    left = kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"run={RUN_ID}", "-o", "name", check=False).split()
    print(f"  pods of this run still listed (terminating): {len(left)}")

heading("Timing table")
print(f"  {'case':<22}{'create':>9}   {'node':<34}note")
for case, seconds, node, note in rows:
    print(f"  {case:<22}{seconds:8.1f}s   {node:<34}{note}")
if warm:
    print(f"\n  warm median of {len(warm)}: {statistics.median(warm):.1f} s   (min {min(warm):.1f}, max {max(warm):.1f})")
over = [row for row in rows if row[1] > SERVER_CREATE_TIMEOUT]
if over:
    print(f"  {len(over)} create(s) took longer than the server's {SERVER_CREATE_TIMEOUT} s sandbox_create_timeout_seconds:"
          " pre-pull the image or raise the timeout in values/server.yaml")
else:
    print(f"  no create took longer than the server's {SERVER_CREATE_TIMEOUT} s sandbox_create_timeout_seconds")
print(f"  total {time.monotonic() - script_start:.0f} s")

if failures:
    print(f"\n{len(failures)} problem(s):")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("\nall steps passed")
