#!/usr/bin/env python3
"""Change the lifecycle server's config.toml with a Helm upgrade, one setting at a time.

Every server change in parts 3 to 6 goes through this script: the secure runtime (part 3, Kata),
the egress sidecar image (part 5) and the egress mode (part 6). It rebuilds the opensandbox-server
release from the same values part 2's 04_install.sh uses, so nothing set there is lost:

  1. ../opensandbox-eks/values/server.yaml, with ${ECR_REGISTRY} and ${ECR_PREFIX} filled in
  2. ../opensandbox-eks/values/server-gateway.yaml (05_gateway.sh: the ingress gateway; the chart
     turns it into the [ingress] section and server.gateway)
  3. configToml: server.yaml's config.toml with the [egress] image and mode lines set, and the
     [secure_runtime] block from values/runtime-<name>.toml appended (runc: no block)
  4. the two pod annotations 04_install.sh sets (API key Secret version, template hash)

A setting you don't pass keeps the value of the last *deployed* Helm revision (a failed upgrade
is never taken as the current state), so --runtime keeps part 5's egress image and
--egress-image keeps the runtime. --baseline puts back what 04_install.sh installs: runc, the
base egress image, dns+nft.

Safety:
  - One change at a time: a lock file (logs/server_config.lock next to this script) is held from
    reading the current state until the new server is verified, and the release's revision is
    read again just before the upgrade; if another client changed it meanwhile, nothing is
    upgraded.
  - The gateway is never dropped by accident: when the running release has server.gateway and
    values/server-gateway.yaml is missing, the script refuses (--drop-gateway removes it on
    purpose). The composed values are rendered with `helm template` first and must contain the
    wanted [egress], [secure_runtime] and [ingress]; after the upgrade the config.toml mounted
    in the new server pod is read back and checked the same way.
  - Every run makes a new Helm revision, also when nothing changes (that repairs drift; the
    manifests are then identical and the pod is not restarted).

usage:
  ./server_config.py --show [--json]                 print what the server runs with, change nothing
  ./server_config.py [--runtime runc|kata-qemu|kata-clh] [--egress-image base|IMAGE]
                     [--egress-mode dns|dns+nft] [--drop-gateway] [--dry-run]
  ./server_config.py --baseline [--dry-run]

The server Deployment uses Recreate: a config change replaces the pod, the API is down for a few
seconds, and a kubectl port-forward to the old pod ends. The checks at the end go through
`kubectl exec` (no port-forward needed). Exit 0 when the server answers /health and /version with
the requested settings, 1 on failure, 2 on bad arguments.

Python 3.11 or later, standard library only (callers from other directories run it as is);
kubectl, helm, jq and the AWS CLI on PATH. Prints no account ID, key or NLB name.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit(f"server_config.py needs Python 3.11 or later (for tomllib); python3 here is {sys.version.split()[0]}")

import argparse  # noqa: E402
import difflib  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import tomllib  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
# OSB_EKS_DIR and OSB_LIFECYCLE_LOGS exist for the tests (tests/test_server_config.py).
EKS_DIR = Path(os.environ.get("OSB_EKS_DIR") or HERE.parent / "opensandbox-eks")
CHART = EKS_DIR / ".src/OpenSandbox/manifests/charts/server"
SERVER_VALUES = EKS_DIR / "values/server.yaml"
GATEWAY_VALUES = EKS_DIR / "values/server-gateway.yaml"
RUNTIME_DIR = HERE / "values"
LOGS = Path(os.environ.get("OSB_LIFECYCLE_LOGS") or HERE / "logs")
LOCK_WAIT = float(os.environ.get("SERVER_CONFIG_LOCK_WAIT", "900"))  # seconds to wait for another run
NS = "opensandbox-system"
RELEASE = "opensandbox-server"
KEY_SECRET = "opensandbox-server-auth"
TEMPLATE_CM = "opensandbox-batchsandbox-template"
MOUNTED_CONFIG = "/etc/opensandbox/config.toml"   # the chart mounts it with subPath: fixed at pod start
OSB_VERSION = "1.2.0"
HELM_TIMEOUT = "5m"
MODES = ("dns", "dns+nft")
ENV: dict[str, str] = {}


def die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


def load_env() -> None:
    path = EKS_DIR / "env.sh"
    if not path.is_file():
        die("../opensandbox-eks/env.sh not found: run ../opensandbox-eks/01_cluster.sh first")
    for line in path.read_text().splitlines():
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if match:
            ENV[match.group(1)] = match.group(2).strip().strip("'\"")
    for name in ("KUBE_CONTEXT", "ECR_REGISTRY", "ECR_PREFIX", "AWS_REGION"):
        if not ENV.get(name):
            die(f"../opensandbox-eks/env.sh has no {name}: run ../opensandbox-eks/01_cluster.sh again")


def run(cmd: list[str], *, check: bool = True, stdin: str | None = None, timeout: float = 120) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        die(f"{' '.join(cmd[:4])} ... did not finish within {timeout:.0f} s")
    if check and done.returncode != 0:
        die(f"{' '.join(cmd[:4])} ... failed: {short((done.stderr or done.stdout).strip()[:400])}")
    return done.stdout


def kubectl(*args: str, check: bool = True, stdin: str | None = None) -> str:
    return run(["kubectl", "--context", ENV["KUBE_CONTEXT"], *args], check=check, stdin=stdin)


def helm(*args: str, check: bool = True, timeout: float = 120) -> str:
    return run(["helm", "--kube-context", ENV["KUBE_CONTEXT"], *args], check=check, timeout=timeout)


def short(text: str) -> str:
    """Text without the registry host (it holds the account ID)."""
    registry = ENV.get("ECR_REGISTRY")
    if not registry:
        return text
    return text.replace(registry + "/", "").replace(registry, "<registry>")


# --- the runtimes and the live state ------------------------------------------------------------

def runtimes() -> dict[str, str | None]:
    """runtime name -> its [secure_runtime] block (None for runc), from values/runtime-*.toml."""
    found: dict[str, str | None] = {"runc": None}
    for path in sorted(RUNTIME_DIR.glob("runtime-*.toml")):
        found[path.stem.removeprefix("runtime-")] = path.read_text()
    return found


def runtime_class(block: str | None) -> str | None:
    if block is None:
        return None
    try:
        return tomllib.loads(block)["secure_runtime"]["k8s_runtime_class"]
    except (tomllib.TOMLDecodeError, KeyError) as error:
        die(f"a values/runtime-*.toml file has no valid [secure_runtime] k8s_runtime_class: {error}")


def release_revisions() -> dict:
    """{deployed, latest, latest_status} from helm history. Dies when nothing is deployed or an
    upgrade is still pending."""
    history = json.loads(helm("-n", NS, "history", RELEASE, "--max", "50", "-o", "json", check=False) or "[]")
    if not history:
        die(f"release {RELEASE} is not installed: run ../opensandbox-eks/04_install.sh first")
    latest = history[-1]
    if latest["status"].startswith("pending-"):
        die(f"release {RELEASE} is stuck in {latest['status']} (revision {latest['revision']}; an earlier helm run was "
            f"killed or is still running): helm --kube-context {ENV['KUBE_CONTEXT']} -n {NS} rollback {RELEASE}, "
            "then run this again")
    deployed = [h for h in history if h["status"] == "deployed"]
    if not deployed:
        die(f"release {RELEASE} has no deployed revision in its last {len(history)}: roll back to a working one first")
    return {"deployed": deployed[-1]["revision"], "latest": latest["revision"], "latest_status": latest["status"]}


def revision_values(revision: int) -> dict:
    return json.loads(helm("-n", NS, "get", "values", RELEASE, "--revision", str(revision), "-o", "json"))


def state_of(values: dict, revision: int) -> dict:
    """{runtime, egress_image, egress_mode, gateway} of a revision's values. A missing [egress] key is None
    (never ""), so the caller can refuse to carry it forward."""
    text = values.get("configToml")
    if not text:
        die(f"revision {revision} of {RELEASE} has no configToml value: it was not installed by 04_install.sh "
            "or this script; run ../opensandbox-eks/04_install.sh")
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        die(f"revision {revision}'s configToml does not parse: {error}")
    secure = config.get("secure_runtime") or {}
    runtime = "runc"
    if secure.get("type"):
        cls = secure.get("k8s_runtime_class")
        names = [name for name, block in runtimes().items() if runtime_class(block) == cls]
        if not names:
            die(f"revision {revision} runs [secure_runtime] type={secure.get('type')!r} k8s_runtime_class={cls!r}, "
                "which no values/runtime-*.toml describes: pass --runtime explicitly")
        runtime = names[0]
    egress = config.get("egress") or {}
    gateway = (values.get("server") or {}).get("gateway") or {}
    return {"runtime": runtime, "egress_image": egress.get("image") or None, "egress_mode": egress.get("mode") or None,
            "gateway": {"enabled": bool(gateway.get("enabled")), "host": gateway.get("host"),
                        "route": gateway.get("gatewayRouteMode")}}


def show_line(state: dict, base_image: str) -> str:
    image = state["egress_image"]
    image = "base" if image == base_image else (short(image) if image else "MISSING")
    return (f"runtime={state['runtime']} egress_image={image} egress_mode={state['egress_mode'] or 'MISSING'} "
            f"ingress={'gateway' if state['gateway']['enabled'] else 'direct'}")


# --- the values ---------------------------------------------------------------------------------

def rendered_server_yaml() -> str:
    text = SERVER_VALUES.read_text()
    text = text.replace("${ECR_REGISTRY}", ENV["ECR_REGISTRY"]).replace("${ECR_PREFIX}", ENV["ECR_PREFIX"])
    if "${" in text:
        die(f"{SERVER_VALUES}: a ${{...}} other than ECR_REGISTRY and ECR_PREFIX is left unfilled")
    return text


def base_config_toml(server_yaml: str) -> str:
    """The configToml block of values/server.yaml: the lines after `configToml: |`, indented by 2."""
    lines = server_yaml.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("configToml: |"))
    except StopIteration:
        die(f"{SERVER_VALUES} has no top-level `configToml: |` block")
    body = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break  # the next top-level key
        body.append(line[2:] if line.startswith("  ") else line.lstrip())
    text = "\n".join(body).strip("\n") + "\n"
    tomllib.loads(text)  # it must be valid TOML as it stands
    return text


def set_key(text: str, section: str, key: str, value: str) -> str:
    """Set `key = "value"` inside [section] (the line must exist there)."""
    lines = text.splitlines()
    current, hits = None, 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            current = stripped.strip("[]").strip()
            continue
        if current == section and re.match(rf"^{re.escape(key)}\s*=", stripped):
            lines[i] = f"{key} = {json.dumps(value)}"
            hits += 1
    if hits != 1:
        die(f"values/server.yaml configToml: expected one `{key} =` line in [{section}], found {hits}")
    return "\n".join(lines) + "\n"


def build_config_toml(base: str, runtime: str, egress_image: str, egress_mode: str) -> str:
    if "secure_runtime" in tomllib.loads(base):
        die("values/server.yaml configToml already has a [secure_runtime] section: remove it there; "
            "this script adds it from values/runtime-<name>.toml")
    text = set_key(base, "egress", "image", egress_image)
    text = set_key(text, "egress", "mode", egress_mode)
    block = runtimes()[runtime]
    if block is not None:
        # The block without the file's comment lines, under a one-line note saying where it came from.
        header = re.search(r"^\[secure_runtime\]\s*$", block, re.M)
        if header is None:
            die(f"values/runtime-{runtime}.toml has no [secure_runtime] line")
        table = block[header.start():].strip("\n")
        text = text.rstrip("\n") + f"\n\n# from opensandbox-lifecycle/values/runtime-{runtime}.toml\n" + table + "\n"
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        die(f"the new config.toml does not parse ({error}): check values/runtime-{runtime}.toml")
    return text


def gateway_file() -> dict | None:
    """server.gateway from values/server-gateway.yaml (a flat file 05_gateway.sh writes), or None."""
    if not GATEWAY_VALUES.is_file():
        return None
    text = GATEWAY_VALUES.read_text()

    def field(name: str) -> str | None:
        match = re.search(rf"^\s+{name}:\s*(\S+)\s*$", text, re.M)
        return match.group(1).strip("'\"") if match else None

    return {"enabled": field("enabled") == "true", "host": field("host"), "route": field("gatewayRouteMode")}


def pod_annotations() -> list[str]:
    """The two --set-string flags 04_install.sh passes, computed the same way, so this script does
    not restart the server unless the config changes, and 04_install.sh does not either afterwards."""
    key_version = kubectl("-n", NS, "get", "secret", KEY_SECRET, "-o", "jsonpath={.metadata.resourceVersion}").strip()
    data = run(["jq", "-cS", ".data"], stdin=kubectl("-n", NS, "get", "configmap", TEMPLATE_CM, "-o", "json"))
    template_sha = hashlib.sha256(data.encode()).hexdigest()[:16]
    return ["--set-string", f"server.podAnnotations.opensandbox-eks/api-key-version={key_version}",
            "--set-string", f"server.podAnnotations.opensandbox-eks/template-sha256={template_sha}"]


def config_from_configmap_yaml(rendered: str) -> dict:
    """The config.toml inside `helm template --show-only templates/configmap.yaml` output."""
    lines = rendered.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "config.toml: |")
    except StopIteration:
        die("helm template rendered no config.toml")
    body = []
    for line in lines[start + 1:]:
        if line and not line.startswith("    "):
            break
        body.append(line[4:])
    return tomllib.loads("\n".join(body))


def problems_with(config: dict, want: dict, where: str) -> list[str]:
    """What in a parsed config.toml differs from the wanted settings."""
    found = []
    egress = config.get("egress") or {}
    if egress.get("image") != want["egress_image"]:
        found.append(f"{where}: [egress] image is {short(str(egress.get('image')))}, wanted {short(want['egress_image'])}")
    if egress.get("mode") != want["egress_mode"]:
        found.append(f"{where}: [egress] mode is {egress.get('mode')}, wanted {want['egress_mode']}")
    cls = (config.get("secure_runtime") or {}).get("k8s_runtime_class")
    if cls != runtime_class(runtimes()[want["runtime"]]):
        found.append(f"{where}: [secure_runtime] k8s_runtime_class is {cls}, wanted {want['runtime']}")
    ingress = config.get("ingress") or {}
    if want["gateway"]["enabled"]:
        if ingress.get("mode") != "gateway":
            found.append(f"{where}: [ingress] mode is {ingress.get('mode')}, wanted gateway (the gateway would be lost)")
        elif (ingress.get("gateway") or {}).get("address") != want["gateway"]["host"]:
            found.append(f"{where}: [ingress] gateway.address is not the host in values/server-gateway.yaml")
    elif ingress.get("mode") == "gateway":
        found.append(f"{where}: [ingress] mode is gateway, wanted direct (--drop-gateway)")
    return found


# --- checks ---------------------------------------------------------------------------------------

def server_pod() -> dict[str, str] | None:
    pods = json.loads(kubectl("-n", NS, "get", "pods", "-l", "app.kubernetes.io/instance=" + RELEASE, "-o", "json"))
    live = [p for p in pods["items"] if not p["metadata"].get("deletionTimestamp")]
    if not live:
        return None
    pod = live[0]
    statuses = pod.get("status", {}).get("containerStatuses") or []
    return {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
            "ready": str(bool(statuses) and all(s.get("ready") for s in statuses)).lower()}


def probe() -> tuple[str, str]:
    """/health and /version from inside the server pod (kubectl exec goes through the kubelet; the
    API server's service proxy cannot reach port 80 here, see 04_install.sh)."""
    code = ('import urllib.request as u\n'
            'for p in ("/health", "/version"):\n'
            '    print(u.urlopen("http://127.0.0.1" + p, timeout=5).read().decode())')
    for _ in range(20):
        out = kubectl("-n", NS, "exec", f"deploy/{RELEASE}", "-c", "main", "--", "python", "-c", code, check=False)
        lines = out.strip().splitlines()
        if len(lines) == 2:
            return lines[0], lines[1]
        time.sleep(3)
    return "", ""


def mounted_config() -> dict | None:
    """The config.toml the running server pod was started with (a subPath mount: it never changes in a pod)."""
    out = kubectl("-n", NS, "exec", f"deploy/{RELEASE}", "-c", "main", "--", "cat", MOUNTED_CONFIG, check=False)
    try:
        return tomllib.loads(out) if out.strip() else None
    except tomllib.TOMLDecodeError:
        return None


def diagnose() -> None:
    print("--- server pods and their last events / log lines", file=sys.stderr)
    print(kubectl("-n", NS, "get", "pods", "-l", "app.kubernetes.io/instance=" + RELEASE, "-o", "wide", check=False),
          file=sys.stderr)
    events = kubectl("-n", NS, "get", "events", "--sort-by=.lastTimestamp", check=False).splitlines()
    print("\n".join(line for line in events[-15:] if RELEASE in line), file=sys.stderr)
    print(short(kubectl("-n", NS, "logs", f"deploy/{RELEASE}", "-c", "main", "--tail=15", check=False)), file=sys.stderr)


def check_image(image: str) -> None:
    """For an image in this account's ECR registry, check that it exists before the server uses it."""
    if not image.startswith(ENV["ECR_REGISTRY"] + "/"):
        print(f"  egress image {image} is not in this ECR registry: not checked")
        return
    rest = short(image)
    if "@" in rest:
        repo, digest = rest.split("@", 1)
        image_id = f"imageDigest={digest}"
    else:
        repo, _, tag = rest.rpartition(":")
        image_id = f"imageTag={tag}"
    try:
        done = subprocess.run(["aws", "ecr", "describe-images", "--region", ENV["AWS_REGION"], "--repository-name", repo,
                               "--image-ids", image_id, "--query", "imageDetails[0].imagePushedAt", "--output", "text"],
                              capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        die("aws ecr describe-images did not answer within 60 s")
    if done.returncode != 0:
        code = re.search(r"\(([A-Za-z]+Exception|[A-Za-z]+NotFound[A-Za-z]*)\)", done.stderr)
        die(f"egress image {rest} is not in ECR ({code.group(1) if code else 'aws ecr describe-images failed'})")


@contextmanager
def change_lock():
    """An exclusive lock for the whole read-modify-write-verify, shared by every caller of this file."""
    LOGS.mkdir(parents=True, exist_ok=True)
    handle = open(LOGS / "server_config.lock", "a+")
    deadline = time.monotonic() + LOCK_WAIT
    told = False
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                die(f"another server_config.py still holds {LOGS / 'server_config.lock'} after {LOCK_WAIT:.0f} s")
            if not told:
                handle.seek(0)
                holder = handle.read().strip() or "?"
                print(f"  waiting for another server_config.py ({holder}) to finish", flush=True)
                told = True
            time.sleep(1)
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid {os.getpid()}, since {time.strftime('%H:%M:%S')}\n")
    handle.flush()
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def run_upgrade(cmd: list[str]) -> int:
    """helm upgrade --wait, output to logs/helm-server.log. Ctrl-C does not kill helm (that would leave the
    release pending-upgrade): the script waits for it to finish and then reports."""
    with open(LOGS / "helm-server.log", "w") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                print(f"  interrupted: waiting for helm to finish (at most --timeout {HELM_TIMEOUT}; another Ctrl-C waits "
                      "too) so the release is not left pending-upgrade", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Change the lifecycle server's config.toml (see the header).")
    parser.add_argument("--runtime", choices=sorted(runtimes()))
    parser.add_argument("--egress-image", metavar="base|IMAGE")
    parser.add_argument("--egress-mode", choices=MODES)
    parser.add_argument("--baseline", action="store_true", help="runc, base egress image, dns+nft")
    parser.add_argument("--drop-gateway", action="store_true", help="upgrade without values/server-gateway.yaml")
    parser.add_argument("--show", action="store_true", help="print the current settings and exit")
    parser.add_argument("--json", action="store_true", help="with --show: one JSON object")
    parser.add_argument("--dry-run", action="store_true", help="print the config.toml change, upgrade nothing")
    args = parser.parse_args(argv)
    if args.baseline and (args.runtime or args.egress_image or args.egress_mode):
        parser.error("--baseline sets all three settings; pass it alone")

    load_env()
    for tool in ("kubectl", "helm", "jq", "aws"):
        if shutil.which(tool) is None:
            die(f"{tool} is not installed (see README.md)")
    if not SERVER_VALUES.is_file():
        die(f"{SERVER_VALUES} not found: this directory needs part 2's directory next to it")
    if kubectl("get", "--raw", "/readyz", check=False).strip() != "ok":
        die(f"cannot reach context '{ENV['KUBE_CONTEXT']}': is the cluster up? (../opensandbox-eks/08_scale.sh up)")
    server_yaml = rendered_server_yaml()
    base = base_config_toml(server_yaml)
    base_image = tomllib.loads(base)["egress"]["image"]
    base_mode = tomllib.loads(base)["egress"]["mode"]

    if args.show:
        revisions = release_revisions()
        if revisions["latest"] != revisions["deployed"]:
            print(f"note: the latest revision {revisions['latest']} is {revisions['latest_status']}; showing revision "
                  f"{revisions['deployed']}, the last deployed one", file=sys.stderr)
        state = state_of(revision_values(revisions["deployed"]), revisions["deployed"])
        if args.json:
            image = state["egress_image"]
            print(json.dumps({"runtime": state["runtime"],
                              "egress_image": "base" if image == base_image else (short(image) if image else None),
                              "egress_mode": state["egress_mode"],
                              "ingress": "gateway" if state["gateway"]["enabled"] else "direct",
                              "gateway_values": GATEWAY_VALUES.is_file(), "revision": revisions["deployed"]}))
        else:
            print(show_line(state, base_image))
        return 0

    if args.dry_run:  # reads only: no need to wait for another run's lock
        return change(args, server_yaml, base, base_image, base_mode)
    with change_lock():
        return change(args, server_yaml, base, base_image, base_mode)


def change(args, server_yaml: str, base: str, base_image: str, base_mode: str) -> int:
    revisions = release_revisions()
    if revisions["latest"] != revisions["deployed"]:
        print(f"  note: the latest revision {revisions['latest']} is {revisions['latest_status']}: the settings not "
              f"given here come from revision {revisions['deployed']}, the last deployed one")
    live_values = revision_values(revisions["deployed"])
    before = state_of(live_values, revisions["deployed"])

    if args.baseline:
        args.runtime, args.egress_image, args.egress_mode = "runc", "base", base_mode
    runtime = args.runtime or before["runtime"]
    egress_image = before["egress_image"] if args.egress_image is None else args.egress_image
    egress_image = base_image if egress_image == "base" else egress_image
    egress_mode = args.egress_mode or before["egress_mode"]
    for name, value, flag in (("image", egress_image, "--egress-image"), ("mode", egress_mode, "--egress-mode")):
        if not value:
            die(f"revision {revisions['deployed']} has no [egress] {name} to keep: pass {flag} (or --baseline)")

    file_gateway = gateway_file()
    if args.drop_gateway:
        gateway = {"enabled": False, "host": None, "route": None}
    elif file_gateway is not None:
        gateway = file_gateway
        if before["gateway"]["enabled"] and not (file_gateway["enabled"] and file_gateway["host"]):
            die("refusing to upgrade: the server runs with the ingress gateway (server.gateway in revision "
                f"{revisions['deployed']}), but ../opensandbox-eks/values/server-gateway.yaml does not enable one with "
                "a host. Fix the file (../opensandbox-eks/05_gateway.sh writes it), or pass --drop-gateway to remove "
                "the gateway on purpose")
        if before["gateway"]["enabled"] and file_gateway["host"] != before["gateway"]["host"]:
            print("  note: values/server-gateway.yaml names another gateway host than the running release "
                  "(05_gateway.sh rewrote it); the file wins")
    elif before["gateway"]["enabled"]:
        die("the server runs with the ingress gateway (server.gateway in revision "
            f"{revisions['deployed']}), but ../opensandbox-eks/values/server-gateway.yaml is missing: an upgrade "
            "without it would remove the gateway. Restore the file (../opensandbox-eks/05_gateway.sh writes it), "
            "or pass --drop-gateway to remove the gateway on purpose")
    else:
        gateway = {"enabled": False, "host": None, "route": None}
    want = {"runtime": runtime, "egress_image": egress_image, "egress_mode": egress_mode, "gateway": gateway}

    print(f"server now:    {show_line(before, base_image)}   (revision {revisions['deployed']})")
    print(f"server wanted: {show_line(want, base_image)}")
    if runtime != "runc":
        cls = runtime_class(runtimes()[runtime])
        if not kubectl("get", "runtimeclass", cls, "-o", "name", check=False).strip():
            die(f"RuntimeClass {cls} does not exist (the server refuses to start without it): "
                "run ../opensandbox-eks/02_kata.sh")
    if egress_image != base_image:
        check_image(egress_image)

    config_toml = build_config_toml(base, runtime, egress_image, egress_mode)
    diff = list(difflib.unified_diff(short(live_values.get("configToml") or "").splitlines(),
                                     short(config_toml).splitlines(),
                                     "running config.toml", "new config.toml", lineterm="", n=1))
    print("  config.toml: " + ("unchanged" if not diff else "changes:"))
    for line in diff:
        print("    " + line)
    if before["gateway"]["enabled"] and not gateway["enabled"]:
        print("  --drop-gateway: the [ingress] gateway settings are removed")

    chart_version = re.search(r"^version: (.+)$", helm("show", "chart", str(CHART)), re.M)
    if not chart_version or chart_version.group(1).strip() != OSB_VERSION:
        die(f"{CHART} is not the {OSB_VERSION} chart: run ../opensandbox-eks/04_install.sh to fetch it")

    LOGS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "server.yaml").write_text(server_yaml)
        # JSON is YAML: a values file with the one key, no YAML library needed.
        (Path(tmp) / "config.json").write_text(json.dumps({"configToml": config_toml}))
        values = ["-f", f"{tmp}/server.yaml"]
        if file_gateway is not None and not args.drop_gateway:
            values += ["-f", str(GATEWAY_VALUES)]
        values += ["-f", f"{tmp}/config.json"]

        # The composed values, rendered: they must hold what we want before anything is upgraded.
        rendered = helm("template", RELEASE, str(CHART), "-n", NS, *values, "--show-only", "templates/configmap.yaml")
        found = problems_with(config_from_configmap_yaml(rendered), want, "composed values")
        if found:
            die("refusing to upgrade:\n  " + "\n  ".join(found))
        if args.dry_run:
            print("  composed values checked with helm template")
            print("--dry-run: nothing upgraded")
            return 0

        # Another client may have changed the release since we read it (the lock covers callers of this
        # file only): read the revisions again, right before the upgrade.
        now = release_revisions()
        if now["latest"] != revisions["latest"]:
            die(f"the release changed while this script ran (revision {revisions['latest']} -> {now['latest']}, "
                "another client): nothing upgraded; run this again")

        pod_before = server_pod()
        started = time.monotonic()
        cmd = ["helm", "--kube-context", ENV["KUBE_CONTEXT"], "upgrade", RELEASE, str(CHART), "-n", NS, *values,
               *pod_annotations(), "--wait", "--timeout", HELM_TIMEOUT]
        returncode = run_upgrade(cmd)
    took = time.monotonic() - started
    if returncode != 0:
        log = LOGS / "helm-server.log"
        print(short(log.read_text()[-1500:] if log.is_file() else "(no helm output)"), file=sys.stderr)
        diagnose()
        die(f"helm upgrade failed after {took:.0f} s. The release's last deployed revision is still "
            f"{revisions['deployed']}; this script reads its settings from there. Put the documented baseline back "
            "with ./server_config.py --baseline")

    after_revisions = release_revisions()
    revision = after_revisions["latest"]
    same = (helm("-n", NS, "get", "manifest", RELEASE, "--revision", str(revisions["deployed"]))
            == helm("-n", NS, "get", "manifest", RELEASE, "--revision", str(revision)))
    pod_after = server_pod()
    print(f"  helm upgrade: revision {revisions['deployed']} -> {revision} ({after_revisions['latest_status']}) in "
          f"{took:.0f} s, manifests {'identical' if same else 'changed'}")
    if pod_after is None:
        die("no server pod after the upgrade")
    replaced = bool(pod_before and pod_before["uid"] != pod_after["uid"])
    print(f"  server pod {pod_after['name']} " + ("replaced " + pod_before["name"] if replaced else "unchanged")
          + f", ready={pod_after['ready']}")

    failures = 0

    def check(ok: bool, text: str) -> None:
        nonlocal failures
        print(("  ok    " if ok else "  FAIL  ") + text)
        failures += not ok

    health, version = probe()
    check('"healthy"' in health, f"server /health: {health or 'no answer'}")
    check(f'"{OSB_VERSION}"' in version, f"server /version: {version or 'no answer'}")
    now_state = state_of(revision_values(revision), revision)
    check(after_revisions["latest_status"] == "deployed", f"release revision {revision} is {after_revisions['latest_status']}")
    mounted = mounted_config()
    found = ["the server pod's mounted config.toml could not be read"] if mounted is None else \
        problems_with(mounted, want, "the server pod's config.toml")
    for item in found:
        check(False, item)
    if not found:
        check(True, f"the server pod runs with: {show_line({**want, 'gateway': now_state['gateway']}, base_image)}")
    if before["gateway"]["enabled"] or gateway["enabled"]:
        kept = now_state["gateway"]["enabled"] == gateway["enabled"]
        check(kept, ("gateway kept: server.gateway and [ingress] mode = gateway, host from values/server-gateway.yaml"
                     if gateway["enabled"] else "gateway removed (--drop-gateway)"))
    if replaced:
        print("  The server pod was replaced: a kubectl port-forward started before this has ended. The Python")
        print("  scripts in this directory start their own; restart yours if you use one.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
