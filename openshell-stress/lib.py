"""Shared by the part 6 scripts: the cluster and gateway from ../openshell-eks (part 2), the
p6- naming rule, timed creates, bulk deletes, the background sampler, gateway metrics, and the
one place that changes the gateway's Helm values (and always puts them back).

Every sandbox, provider and Pod these scripts create is named p6-..., and every delete goes
through assert_p6(), so a run never touches anything else on the gateway."""

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent / "openshell-eks"
if not (INSTALL / "common.py").exists():
    sys.exit(f"{INSTALL}/common.py not found: run part 2 (../openshell-eks) first")
sys.path.insert(0, str(INSTALL))
# The scripts start subprocesses (kubectl, the CLI) while gRPC threads run; without this gRPC
# prints a fork() warning for each one.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")

from common import (CONTEXT, GATEWAY, NAMESPACE, WORKSPACE, client,  # noqa: E402,F401
                    kubectl_json, phase_name, require_gateway)

PREFIX = "p6-"
RELEASE = "openshell"                                # Helm release from 05_install.sh
CHART = INSTALL / "charts" / "helm-chart-0.1.6.tgz"  # the chart 05_install.sh installed
PF_PORT = int(os.environ.get("LOCAL_PORT", "17680"))  # the CLI's port-forward (part 2's LOCAL_PORT)
PROBES = HERE / "probes"
POLICIES = HERE / "policies"
VALUES = HERE / "values"
ECHO_HOST = "echo.echo.svc.cluster.local"            # ../openshell-egress/manifests/echo.yaml


# --- small things ---------------------------------------------------------------------------

def results_dir(args=None) -> Path:
    """--results, else $P6_RESULTS, else ./results."""
    d = Path(getattr(args, "results", None) or os.environ.get("P6_RESULTS") or HERE / "results")
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_results_arg(p) -> None:
    p.add_argument("--results", help="directory for CSV/JSON output (default: $P6_RESULTS or ./results)")


def handle_signals() -> None:
    """Turn SIGTERM and SIGHUP into SystemExit, so every finally block and context manager
    (deletes, Helm rollback, sampler stop) runs as it does on Ctrl-C."""
    def _exit(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGHUP, _exit)


def say(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m", flush=True)


def short(e: BaseException, n: int = 200) -> str:
    """One line of an exception: gRPC errors carry their detail in .details()."""
    detail = e.details() if hasattr(e, "details") and callable(e.details) else str(e)
    return re.sub(r"\s+", " ", f"{type(e).__name__}: {detail}")[:n]


def pct(values, p: float) -> float:
    """Nearest-rank percentile; NaN for an empty list."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return float("nan")
    k = max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.5)) - 1))
    return v[k]


def summarize(values) -> dict:
    v = [x for x in values if x is not None]
    return {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "p99": pct(v, 99),
            "min": min(v) if v else float("nan"), "max": max(v) if v else float("nan"),
            "mean": statistics.fmean(v) if v else float("nan")}


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> Path:
    fields = fields or list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data, indent=1, default=str) + "\n")
    return path


def best_effort(label: str, fn, *args, **kwargs):
    """Run one cleanup step; report a failure and carry on with the next step."""
    try:
        return fn(*args, **kwargs)
    except BaseException as e:  # noqa: BLE001 (cleanup must go on, even after Ctrl-C)
        print(f"  cleanup step failed ({label}): {short(e)}", file=sys.stderr)
        if isinstance(e, KeyboardInterrupt):
            raise
        return None


def assert_p6(name: str) -> str:
    if not name.startswith(PREFIX):
        raise ValueError(f"refusing to touch {name!r}: not a {PREFIX} name")
    return name


# --- processes: kubectl, the OpenShell CLI, Helm --------------------------------------------

def run(cmd: list[str], *, check: bool = True, timeout: float | None = 300, env=None,
        input: str | bytes | None = None) -> subprocess.CompletedProcess:
    text = not isinstance(input, bytes)
    return subprocess.run(cmd, check=check, capture_output=True, text=text, timeout=timeout,
                          env=env, input=input)


# Errors that mean the request never left the laptop (the connection was refused or the API
# server's name didn't resolve). Only these are retried: a call that failed mid-flight may have
# done its work, so it is reported, never repeated.
NEVER_SENT = re.compile(r"failed to connect to all addresses|Connection refused|tcp connect error|"
                        r"Unable to connect to the server|no such host")


def kubectl(*args: str, check: bool = True, timeout: float = 120, input=None) -> str:
    """kubectl on the osh-eks context. Retried (3 times, 5 s apart) when the API server can't be
    reached at all; other errors are raised as they are."""
    for attempt in range(4):
        r = run(["kubectl", "--context", CONTEXT, *args], check=False, timeout=timeout, input=input)
        if r.returncode == 0 or not NEVER_SENT.search(str(r.stderr)) or attempt == 3:
            break
        time.sleep(5)
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r.stdout


def kraw(path: str, timeout: float = 30) -> dict:
    """GET a raw API path (faster than kubectl get for big lists)."""
    return json.loads(kubectl("get", "--raw", path, timeout=timeout))


def osh(*args: str, check: bool = True, timeout: float = 300, env=None) -> subprocess.CompletedProcess:
    """The OpenShell CLI on the osh-eks gateway (-g before the subcommand). If the gateway can't
    be reached at all (port-forward down), wait for it (wait_gateway) and run the command again."""
    for attempt in range(4):
        r = run(["openshell", "-g", GATEWAY, *args], check=False, timeout=timeout, env=env)
        if r.returncode == 0 or not NEVER_SENT.search(r.stderr + r.stdout) or attempt == 3:
            break
        wait_gateway()
    if check and r.returncode:
        detail = re.sub(r"\s+", " ", (r.stderr or r.stdout).strip())[-500:]
        raise RuntimeError(f"openshell {' '.join(args[:4])} failed (exit {r.returncode}): {detail}")
    return r


# --- the gateway connection: survive a dropped port-forward -----------------------------------

_pf_lock = threading.Lock()
_pf_restarted = 0.0


def gateway_up() -> bool:
    try:
        with client() as c:
            c.health()
        return True
    except Exception:
        return False


def wait_gateway(max_wait: float = float(os.environ.get("P6_GATEWAY_WAIT_S", 600))) -> None:
    """Block until the gateway answers through the port-forward on 127.0.0.1:PF_PORT. With
    P6_RESTART_PORT_FORWARD=1 the port-forward is restarted (at most once a minute); otherwise
    the run waits for whoever owns it. Gives up with a clear error after max_wait seconds."""
    global _pf_restarted
    start, told = time.monotonic(), False
    while not gateway_up():
        if not told:
            print(f"\n  gateway unreachable at 127.0.0.1:{PF_PORT} (port-forward down?); waiting up to "
                  f"{max_wait:.0f} s" + (", restarting it" if os.environ.get("P6_RESTART_PORT_FORWARD") == "1" else
                                         " (set P6_RESTART_PORT_FORWARD=1 to let the script restart it)"),
                  file=sys.stderr, flush=True)
            told = True
        if os.environ.get("P6_RESTART_PORT_FORWARD") == "1":
            with _pf_lock:
                if time.monotonic() - _pf_restarted > 60:
                    _pf_restarted = time.monotonic()
                    with contextlib.suppress(Exception):
                        restart_port_forward()
        if time.monotonic() - start > max_wait:
            raise RuntimeError(f"gateway still unreachable after {max_wait:.0f} s: restart the port-forward "
                               f"(kubectl -n {NAMESPACE} port-forward svc/openshell {PF_PORT}:8080) and run "
                               "./99_cleanup.sh, then this script again")
        time.sleep(5)
    if told:
        print(f"  gateway reachable again after {time.monotonic() - start:.0f} s", file=sys.stderr, flush=True)


class _RetryPager:
    def __init__(self, owner, args, kwargs):
        self.owner, self.args, self.kwargs = owner, args, kwargs

    def all(self):
        return self.owner._retry(lambda c: c.list(*self.args, **self.kwargs).all())


class ResilientClient:
    """A SandboxClient whose calls survive a dropped port-forward: a call that failed because
    the connection was refused (so it never reached the gateway) waits for the gateway, gets a
    new channel, and runs again. Anything else is raised unchanged. Thread-safe."""

    def __init__(self):
        self._c = client()
        self._gen = 0
        self._lock = threading.Lock()

    def _retry(self, fn):
        for attempt in range(4):
            c, gen = self._c, self._gen
            try:
                return fn(c)
            except Exception as e:
                if attempt == 3 or not NEVER_SENT.search(str(e)):
                    raise
                wait_gateway()
                with self._lock:
                    if self._gen == gen:  # one thread replaces the channel; the others reuse it
                        self._c, self._gen = client(), gen + 1

    def list(self, *args, **kwargs):
        return _RetryPager(self, args, kwargs)

    def __getattr__(self, name):
        if not callable(getattr(self._c, name)):
            return getattr(self._c, name)
        return lambda *a, **kw: self._retry(lambda c: getattr(c, name)(*a, **kw))

    def close(self):
        with contextlib.suppress(Exception):
            self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def p6_client() -> ResilientClient:
    return ResilientClient()


def helm(*args: str, timeout: float = 900) -> str:
    return run(["helm", "--kube-context", CONTEXT, *args], timeout=timeout).stdout


# --- sandboxes --------------------------------------------------------------------------------

def list_p6(c) -> list:
    return [r for r in c.list(workspace=WORKSPACE).all() if r.name.startswith(PREFIX)]


def other_sandboxes(c) -> list[str]:
    """Sandboxes that aren't ours. The full runs expect none (an otherwise empty cluster)."""
    return [r.name for r in c.list(workspace=WORKSPACE).all() if not r.name.startswith(PREFIX)]


def delete_sandboxes(c, names, workers: int = 16, timeout: float = 900) -> dict[str, float]:
    """Delete p6- sandboxes in parallel threads (DeleteSandbox blocks about 30 s each on this
    cluster), then wait until none of them is listed. Returns seconds per delete call."""
    names = [assert_p6(n) for n in names]
    took: dict[str, float] = {}
    if not names:
        return took

    def one(n: str) -> None:
        t = time.monotonic()
        try:
            c.delete(n, workspace=WORKSPACE, allow_missing=True)
        except Exception as e:  # recorded; the wait below decides
            print(f"  delete {n}: {short(e)}", file=sys.stderr)
        took[n] = time.monotonic() - t

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(names)))) as ex:
        list(ex.map(one, names))
    start, wanted = time.monotonic(), set(names)
    while True:
        left = sorted(wanted & {r.name for r in list_p6(c)})
        if not left:
            break
        if time.monotonic() - start > timeout:
            raise RuntimeError(f"still listed after {timeout:.0f} s: {', '.join(left)}")
        time.sleep(2)
    reap_orphans(names)
    return took


ORPHANS: list[str] = []   # Sandbox CRs the gateway forgot, found and removed by reap_orphans


def sandbox_crs() -> set[str]:
    return {i["metadata"]["name"] for i in kraw(f"/apis/agents.x-k8s.io/v1beta1/namespaces/{NAMESPACE}/sandboxes")["items"]}


def reap_orphans(names, wait_s: float = 60) -> list[str]:
    """OpenShell 0.1.6: a DeleteSandbox sent right after a sandbox became Ready can return in
    milliseconds without deleting anything in Kubernetes. The gateway forgets the sandbox (it
    is no longer listed, and its name can't be reused: "already exists"), while its Sandbox CR,
    both Pods, Services, Secrets, NetworkPolicy and EBS volume stay. After a delete, wait for
    the CRs of these p6 names to go; delete any that are left through Kubernetes (their owner
    references take the rest with them) and report them."""
    crs = {f"{WORKSPACE}--{assert_p6(n)}" for n in names}
    deadline = time.monotonic() + wait_s
    while (left := crs & sandbox_crs()) and time.monotonic() < deadline:
        time.sleep(3)
    for cr in sorted(left):
        print(f"  WARNING: the gateway no longer lists {cr.split('--', 1)[1]} but its Sandbox CR {cr} is still "
              "there (orphaned by the delete); deleting it through Kubernetes", file=sys.stderr, flush=True)
        kubectl("-n", NAMESPACE, "delete", "sandboxes.agents.x-k8s.io", cr, "--wait=true", "--timeout=180s", timeout=200)
        ORPHANS.append(cr)
    return sorted(left)


def delete_all_p6(c, workers: int = 16) -> int:
    """Every p6 sandbox the gateway lists, plus any p6 Sandbox CR it forgot (see reap_orphans)."""
    names = {r.name for r in list_p6(c)} | {cr.split("--", 1)[1] for cr in sandbox_crs()
                                            if cr.startswith(f"{WORKSPACE}--{PREFIX}")}
    delete_sandboxes(c, sorted(names), workers=workers)
    return len(names)


class PhaseWatcher(threading.Thread):
    """One ListSandboxes call every `interval` seconds records the first time each p6 sandbox
    is seen in each phase, so 40 creates in flight cost one list call, not 40 get calls."""

    def __init__(self, c, interval: float = 0.5):
        super().__init__(daemon=True)
        self.c, self.interval = c, interval
        self.first: dict[str, dict[str, float]] = defaultdict(dict)
        self.current: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.conditions: dict[str, str] = {}
        self.errors = 0
        self.last_ok = time.time()
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                refs = list_p6(self.c)
                now = time.time()
                seen = set()
                for r in refs:
                    ph = phase_name(r.status.phase)
                    seen.add(r.name)
                    self.current[r.name] = ph
                    self.ids[r.name] = r.id
                    self.first[r.name].setdefault(ph, now)
                    if ph != "Ready":
                        self.conditions[r.name] = "; ".join(
                            f"{x.type}={x.status} {x.reason} {x.message}".strip() for x in r.status.conditions)[:400]
                for gone in set(self.current) - seen:
                    self.current[gone] = "(gone)"
                self.last_ok = now
            except Exception:
                self.errors += 1
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10)


def create_rpc(c, name: str, spec=None) -> dict:
    """CreateSandbox only; readiness comes from a PhaseWatcher. Times are epoch seconds."""
    row = {"name": assert_p6(name), "t_submit": time.time(), "rpc_s": None, "id": "", "create_error": ""}
    t = time.monotonic()
    try:
        ref = c.create(workspace=WORKSPACE, name=name, spec=spec)
        row["id"] = ref.id
    except Exception as e:
        row["create_error"] = short(e)
        if NEVER_SENT.search(str(e)):
            row["create_error"] = "transport (gateway unreachable, not a refusal): " + short(e)
    row["rpc_s"] = round(time.monotonic() - t, 3)
    return row


def wait_ready(watch: PhaseWatcher, rows: list[dict], deadline_s: float) -> None:
    """Fill ready_s / final_phase / stuck for rows until each is Ready, Error or past
    deadline_s since its own submit."""
    pending = {r["name"]: r for r in rows if not r["create_error"]}
    blind = 0.0  # seconds the watcher couldn't list (gateway unreachable): not held against anyone
    while pending:
        now = time.time()
        if now - watch.last_ok > 3 * watch.interval + 2:
            blind += 0.25
            time.sleep(0.25)
            continue
        for name, r in list(pending.items()):
            first = watch.first.get(name, {})
            phase = watch.current.get(name, "")
            if "Ready" in first:
                r.update(ready_s=round(first["Ready"] - r["t_submit"], 3), final_phase="Ready", stuck=False)
            elif phase == "Error":
                r.update(final_phase="Error", stuck=False, detail=watch.conditions.get(name, ""))
            elif now - r["t_submit"] - blind > deadline_s:
                r.update(final_phase=phase or "(not listed)", stuck=True, detail=watch.conditions.get(name, ""))
            else:
                continue
            r["phases"] = ";".join(f"{p}@{t - r['t_submit']:.1f}" for p, t in sorted(first.items(), key=lambda x: x[1]))
            r["gateway_blind_s"] = round(blind, 1)
            del pending[name]
        time.sleep(0.25)
    for r in rows:
        if r["create_error"]:
            r.update(final_phase="CreateFailed", stuck=False)


def sh_in(c, name: str, script: str, timeout: int = 120, stdin: bytes | None = None):
    """Run a shell snippet in a sandbox (no login shell)."""
    return c.exec(name, ["sh", "-c", script], workspace=WORKSPACE, no_login_shell=True,
                  timeout_seconds=timeout, stdin=stdin)


def put_file(c, name: str, path: str, data: bytes) -> None:
    r = sh_in(c, name, f"cat > {path}", stdin=data)
    if r.exit_code != 0:
        raise RuntimeError(f"writing {path} in {name}: {r.stderr.strip()}")


def node_selector(node: str) -> str:
    """--driver-config-json that pins a sandbox's workload Pod to one node (part 2 set
    server.drivers.kubernetes.allowDriverConfig=true). The supervisor Pod isn't pinned."""
    return json.dumps({"kubernetes": {"pod": {"node_selector": {"kubernetes.io/hostname": node}}}})


def create_cli(name: str, *extra: str, timeout: float = 300) -> float:
    """openshell sandbox create --detach (returns once the sandbox is Ready). Seconds taken."""
    deadline = time.monotonic() + 90
    while True:
        t = time.monotonic()
        r = osh("sandbox", "create", "--name", assert_p6(name), "--detach", "--no-tty",
                "--no-auto-providers", *extra, check=False, timeout=timeout)
        if r.returncode == 0:
            return time.monotonic() - t
        # Right after a delete the name can still be taken for a few seconds, although the
        # sandbox is no longer listed.
        if "already exists" not in (r.stderr + r.stdout) or time.monotonic() > deadline:
            raise RuntimeError(f"sandbox create {name}: {(r.stderr or r.stdout).strip()[-600:]}")
        time.sleep(3)


def workload_image() -> str:
    """The series' workload image in ECR, from part 2's env.sh. Never printed: the registry
    name holds the AWS account ID."""
    env = {}
    for line in (INSTALL / "env.sh").read_text().splitlines():
        if line.startswith("export ") and "=" in line:
            k, v = line.removeprefix("export ").split("=", 1)
            env[k] = v.strip("'\"")
    return f"{env['ECR_REGISTRY']}/{env['ECR_PREFIX']}/workload:0.1.6"


def pods_of(sandbox_id: str) -> dict[str, str]:
    """{boundary-role: node} for a sandbox's Pods."""
    pods = kubectl_json("-n", NAMESPACE, "get", "pods", "-l", f"openshell.ai/boundary-pair={sandbox_id}")["items"]
    return {p["metadata"]["labels"].get("openshell.ai/boundary-role", "?"): p["spec"].get("nodeName", "")
            for p in pods}


# --- policies ---------------------------------------------------------------------------------

def load_policy(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text())


def set_network_policies(name: str, network_policies: dict, scratch: Path, wait: bool = True) -> dict:
    """Replace only the network rules of a running sandbox: start from its current base policy
    (so the filesystem paths OpenShell added at startup stay), swap network_policies, and
    `openshell policy set --wait`. Returns the revision the gateway reports."""
    base = json.loads(osh("policy", "get", name, "--base", "-o", "json").stdout)
    policy = base.get("policy", base)
    policy["network_policies"] = network_policies
    f = scratch / f"{name}-policy.json"
    f.write_text(json.dumps(policy))
    args = ["policy", "set", name, "--policy", str(f)] + (["--wait", "--timeout", "120"] if wait else [])
    r = osh(*args, check=False, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"policy set {name}: {(r.stderr or r.stdout).strip()[-600:]}")
    return revisions(name)[0] if wait else {}


def revisions(name: str) -> list[dict]:
    """Policy revisions of a sandbox, newest first, with created_at_ms / loaded_at_ms (gateway clock)."""
    return json.loads(osh("policy", "list", name, "-o", "json").stdout).get("revisions", [])


# --- cluster state ----------------------------------------------------------------------------

def nodes() -> list[dict]:
    return kraw("/api/v1/nodes")["items"]


def node_limits() -> dict[str, dict]:
    """max pods (kubelet, VPC CNI) and the EBS CSI attachment count (CSINode) per node."""
    out = {}
    for n in nodes():
        name = n["metadata"]["name"]
        out[name] = {"max_pods": int(n["status"]["allocatable"]["pods"]),
                     "zone": n["metadata"]["labels"].get("topology.kubernetes.io/zone", ""),
                     "mem_alloc_bytes": _bytes(n["status"]["allocatable"]["memory"]),
                     "ebs_limit": None}
    for cn in kraw("/apis/storage.k8s.io/v1/csinodes")["items"]:
        for d in cn["spec"].get("drivers") or []:
            if d["name"] == "ebs.csi.aws.com" and cn["metadata"]["name"] in out:
                out[cn["metadata"]["name"]]["ebs_limit"] = (d.get("allocatable") or {}).get("count")
    return out


def cluster_counts() -> dict:
    """Pods (non-terminal, scheduled), OpenShell sandbox Pods, unscheduled Pods and attached
    EBS volumes, per node."""
    pods = kraw("/api/v1/pods")["items"]
    vas = kraw("/apis/storage.k8s.io/v1/volumeattachments")["items"]
    per: dict[str, dict] = defaultdict(lambda: {"pods": 0, "sandbox_pods": 0, "p6_pods": 0, "volume_attachments": 0})
    pending = 0
    for p in pods:
        if p["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        node = p["spec"].get("nodeName")
        if not node:
            pending += 1
            continue
        per[node]["pods"] += 1
        labels = p["metadata"].get("labels") or {}
        if "openshell.ai/boundary-pair" in labels:
            per[node]["sandbox_pods"] += 1
            if p["metadata"]["name"].startswith(f"{WORKSPACE}--{PREFIX}") or _owner_is_p6(p):
                per[node]["p6_pods"] += 1
    for va in vas:
        if (va.get("status") or {}).get("attached"):
            per[va["spec"]["nodeName"]]["volume_attachments"] += 1
    return {"nodes": dict(per), "pending_pods": pending}


def _owner_is_p6(p: dict) -> bool:
    return any(o.get("name", "").startswith(f"{WORKSPACE}--{PREFIX}") for o in p["metadata"].get("ownerReferences") or [])


def _bytes(q: str) -> int:
    units = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "k": 1000, "M": 1000 ** 2, "G": 1000 ** 3}
    for u, m in units.items():
        if q.endswith(u):
            return int(float(q[: -len(u)]) * m)
    return int(float(q))


# What stopped a create, from the Pods, PVCs and VolumeAttachments of the stuck sandboxes.
CAUSES = [
    (r"Too many pods", "pods per node (kubelet max-pods from the VPC CNI)"),
    (r"max volume count|exceed max volume|AttachmentLimitExceeded|maximum number of attachments",
     "EBS attachments per node (CSINode allocatable)"),
    (r"Insufficient memory", "memory requests"),
    (r"Insufficient cpu", "CPU requests"),
    (r"failed to assign an IP|no available IP|InsufficientFreeAddresses|ipamd", "pod IP addresses (VPC CNI)"),
    (r"MemoryPressure|Evicted|OOMKilled", "node memory"),
    (r"AttachVolume|FailedAttachVolume|FailedMount", "EBS attach or mount"),
    (r"ProvisioningFailed|WaitForFirstConsumer", "EBS provisioning"),
]


def diagnose(stuck: list[dict]) -> dict:
    """Collect the evidence for stuck or failed sandboxes and name the first known cause."""
    evidence: list[str] = []
    for r in stuck:
        if r.get("create_error"):
            evidence.append(f"{r['name']}: CreateSandbox failed: {r['create_error']}")
        if r.get("detail"):
            evidence.append(f"{r['name']}: conditions: {r['detail']}")
    ids = [r["id"] for r in stuck if r.get("id")]
    names = {r["name"] for r in stuck}
    pods = kraw(f"/api/v1/namespaces/{NAMESPACE}/pods")["items"]
    mine = [p for p in pods if (p["metadata"].get("labels") or {}).get("openshell.ai/boundary-pair") in ids
            or any(p["metadata"]["name"] == f"{WORKSPACE}--{n}" for n in names)]
    for p in mine:
        st = p["status"]
        for cond in st.get("conditions") or []:
            if cond.get("status") == "False" and cond.get("message"):
                evidence.append(f"pod {p['metadata']['name']}: {cond['type']}: {cond['message']}")
        for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
            w = (cs.get("state") or {}).get("waiting") or {}
            if w.get("reason"):
                evidence.append(f"pod {p['metadata']['name']}: {cs['name']} waiting {w['reason']}: {w.get('message', '')}")
        if st.get("reason"):
            evidence.append(f"pod {p['metadata']['name']}: {st['reason']}: {st.get('message', '')}")
    uids = {p["metadata"]["uid"] for p in mine}
    pvc_names = {f"workspace-{WORKSPACE}--{n}" for n in names}
    for ev in kraw(f"/api/v1/namespaces/{NAMESPACE}/events")["items"]:
        io = ev.get("involvedObject") or {}
        if io.get("uid") in uids or io.get("name") in pvc_names:
            if ev.get("type") == "Warning" or ev.get("reason") in ("FailedScheduling", "FailedAttachVolume"):
                evidence.append(f"event {io.get('kind')}/{io.get('name')}: {ev.get('reason')}: {ev.get('message', '')}")
    for va in kraw("/apis/storage.k8s.io/v1/volumeattachments")["items"]:
        err = ((va.get("status") or {}).get("attachError") or {}).get("message")
        if err:
            evidence.append(f"volumeattachment {va['metadata']['name']}: {err}")
    for n in nodes():
        for cond in n["status"].get("conditions") or []:
            if cond["type"] in ("MemoryPressure", "DiskPressure", "PIDPressure") and cond["status"] == "True":
                evidence.append(f"node {n['metadata']['name']}: {cond['type']}")
    evidence = list(dict.fromkeys(evidence))
    cause = "unknown (see evidence)"
    for pattern, label in CAUSES:
        if any(re.search(pattern, e) for e in evidence):
            cause = label
            break
    else:
        if any("CreateSandbox failed: transport" in e for e in evidence):
            cause = "transport: the gateway was unreachable (port-forward), not a refusal; rerun"
        elif any("CreateSandbox failed" in e for e in evidence):
            cause = "the gateway refused the create"
    return {"cause": cause, "evidence": evidence[:60]}


# --- the background sampler -------------------------------------------------------------------

@contextlib.contextmanager
def sampling(csv_path: Path, interval: float = 2.0, metrics_csv: Path | None = None):
    """Run sampler.py in the background for the duration of the block; stop it with SIGINT
    (it finishes the current sample and closes its files)."""
    cmd = [sys.executable, str(HERE / "sampler.py"), "--out", str(csv_path), "--interval", str(interval)]
    if metrics_csv:
        cmd += ["--metrics-out", str(metrics_csv)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(str(csv_path) + ".log", "w"))
    time.sleep(1)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()


# --- gateway metrics (Prometheus text on the gateway Pod's port 9090) -------------------------

class GatewayMetrics:
    """Scrape openshell-0:9090 through a port-forward of our own (not the CLI's), restarting
    it if the gateway Pod was replaced. The metrics port is unauthenticated, so it is never
    exposed beyond 127.0.0.1."""

    def __init__(self, port: int = 19090):
        self.port = port
        self.proc: subprocess.Popen | None = None

    def _start(self) -> None:
        self.close()
        self.proc = subprocess.Popen(
            ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, "port-forward", "pod/openshell-0",
             f"{self.port}:9090", "--address", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

    def scrape(self) -> dict[str, float]:
        import urllib.request
        for attempt in range(2):
            if self.proc is None or self.proc.poll() is not None or attempt:
                self._start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/metrics", timeout=5) as r:
                    return parse_prometheus(r.read().decode())
            except Exception:
                continue
        return {}

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def parse_prometheus(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_:][\w:]*(?:\{[^}]*\})?)\s+(\S+)", line)
        if m:
            try:
                out[m.group(1)] = float(m.group(2))
            except ValueError:
                pass
    return out


def metric_sum(m: dict[str, float], name: str, **labels: str) -> float:
    """Sum every series of `name` whose labels include `labels`."""
    total = 0.0
    for k, v in m.items():
        base, _, rest = k.partition("{")
        if base != name:
            continue
        if all(f'{lk}="{lv}"' in rest for lk, lv in labels.items()):
            total += v
    return total


# --- the gateway's Helm values: change, then always put back ----------------------------------

# What each overlay must change in the gateway's rendered configuration (ConfigMap
# openshell-config, key gateway.toml), checked after the upgrade.
EXPECT_IN_TOML = {
    "shared-supervisor.yaml": 'supervisor_topology = "gateway"',
    "push-delivery.yaml": 'config_delivery_mode = "push"',
    "workspace-1gi.yaml": 'workspace_default_storage_size = "1Gi"',
}


def gateway_toml() -> str:
    return kubectl("-n", NAMESPACE, "get", "configmap", "openshell-config", "-o", "jsonpath={.data.gateway\\.toml}")


def gateway_pod_uid() -> str:
    return kubectl("-n", NAMESPACE, "get", "pod", "openshell-0", "-o", "jsonpath={.metadata.uid}", check=False).strip()


def restart_port_forward() -> None:
    """The CLI and SDK reach the gateway through `kubectl port-forward svc/openshell PF_PORT:8080`
    (part 2's 05_install.sh), which dies with the gateway Pod. Same pid file and log as
    ../openshell-lifecycle/lib.sh restart_pf."""
    pidf, log = INSTALL / ".port-forward.pid", INSTALL / "logs" / "port-forward.log"
    if pidf.exists():
        with contextlib.suppress(Exception):
            os.kill(int(pidf.read_text().strip()), signal.SIGTERM)
    kubectl("-n", NAMESPACE, "rollout", "status", "statefulset/openshell", "--timeout=5m", timeout=330)
    log.parent.mkdir(exist_ok=True)
    proc = subprocess.Popen(["kubectl", "--context", CONTEXT, "-n", NAMESPACE, "port-forward", "svc/openshell",
                             f"{PF_PORT}:8080"], stdout=open(log, "a"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    pidf.write_text(f"{proc.pid}\n")
    for _ in range(60):
        try:
            with client() as c:
                c.health()
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("gateway not reachable after restarting the port-forward")


def _wait_gateway_replaced(old_uid: str) -> None:
    """Changing gatewayConfig changes the ConfigMap checksum on the gateway Pod template, so
    the StatefulSet replaces the Pod. If it didn't (no rendered change), restart it so the
    gateway reads gateway.toml again."""
    kubectl("-n", NAMESPACE, "rollout", "status", "statefulset/openshell", "--timeout=10m", timeout=630)
    for _ in range(30):
        if gateway_pod_uid() not in ("", old_uid):
            return
        time.sleep(2)
    kubectl("-n", NAMESPACE, "rollout", "restart", "statefulset/openshell")
    kubectl("-n", NAMESPACE, "rollout", "status", "statefulset/openshell", "--timeout=10m", timeout=630)


def wait_pool_ready(max_s: float = 300) -> float:
    """After switching to the shared supervisor pool, the gateway refuses creates ("the shared
    supervisor has no capacity for another sandbox instance") until the pool members have
    connected and the gateway can place instances, which takes longer than the Deployment's
    rollout. Create a canary sandbox every 10 s until one is accepted and Ready, delete it, and
    return the seconds it took."""
    t = time.monotonic()
    last = ""
    while time.monotonic() - t < max_s:
        try:
            create_cli("p6-canary", timeout=180)
            osh("sandbox", "delete", "p6-canary", check=False, timeout=180)
            reap_orphans(["p6-canary"])
            return time.monotonic() - t
        except RuntimeError as e:
            last = short(e)
            osh("sandbox", "delete", "p6-canary", check=False, timeout=180)
            time.sleep(10)
    raise RuntimeError(f"the shared supervisor pool accepted no sandbox within {max_s:.0f} s: {last}")


def helm_commands(files: list[Path], revision: int | str = "<revision before>") -> tuple[list[str], list[str]]:
    up = ["helm", "--kube-context", CONTEXT, "upgrade", RELEASE, str(CHART), "-n", NAMESPACE, "--reuse-values",
          *[a for f in files for a in ("-f", str(f))], "--wait", "--timeout", "10m"]
    down = ["helm", "--kube-context", CONTEXT, "rollback", RELEASE, str(revision), "-n", NAMESPACE,
            "--wait", "--timeout", "10m"]
    return up, down


@contextlib.contextmanager
def gateway_values(files: list[Path], label: str, print_only: bool = False):
    """Apply chart values on top of the release's own (helm upgrade --reuse-values -f ...) for
    the duration of the block, then roll the release back to the exact revision it had before,
    also on Ctrl-C, SIGTERM or an error. On the way out p6 sandboxes go first: sandboxes
    created under a shared supervisor pool would lose their supervisor when the pool goes.

    Changing gatewayConfig restarts the gateway Pod, so the CLI's port-forward is restarted
    after each step."""
    if not files:
        yield
        return
    up, down = helm_commands(files)
    if print_only:
        print("  would run:  " + " ".join(up))
        print("  revert:     " + " ".join(down))
        yield
        return
    hist = json.loads(helm("history", RELEASE, "-n", NAMESPACE, "--max", "1", "-o", "json"))
    rev = int(hist[-1]["revision"])
    before = json.loads(helm("get", "values", RELEASE, "-n", NAMESPACE, "-o", "json") or "{}")
    up, down = helm_commands(files, rev)
    say(f"gateway change ({label}): helm upgrade --reuse-values " + " ".join(f"-f {f.name}" for f in files))
    print(f"  to undo by hand: {' '.join(down)}", flush=True)
    toml_before = gateway_toml()
    try:
        uid = gateway_pod_uid()
        t = time.monotonic()
        run(up, timeout=900)
        toml = gateway_toml()
        missing = [EXPECT_IN_TOML[f.name] for f in files if f.name in EXPECT_IN_TOML and EXPECT_IN_TOML[f.name] not in toml]
        if missing:
            raise RuntimeError(f"the rendered gateway.toml lacks {missing} after the upgrade")
        _wait_gateway_replaced(uid)
        if any(f.name == "shared-supervisor.yaml" for f in files):
            kubectl("-n", NAMESPACE, "rollout", "status", "deployment/openshell-supervisor", "--timeout=5m", timeout=330)
        restart_port_forward()
        if any(f.name == "shared-supervisor.yaml" for f in files):
            print(f"  shared supervisor pool accepting sandboxes after {wait_pool_ready():.0f} s more (canary p6-canary)", flush=True)
        print(f"  applied in {time.monotonic() - t:.0f} s (gateway.toml has "
              f"{', '.join(EXPECT_IN_TOML[f.name] for f in files if f.name in EXPECT_IN_TOML)}; gateway Pod replaced)")
        yield
    finally:
        say(f"reverting the gateway change ({label}): helm rollback {RELEASE} {rev}")
        with contextlib.suppress(Exception):
            with p6_client() as c:
                if n := delete_all_p6(c):
                    print(f"  deleted {n} p6 sandbox(es) first")
        uid = gateway_pod_uid()
        run(down, timeout=900)
        _wait_gateway_replaced(uid)
        restart_port_forward()
        after = json.loads(helm("get", "values", RELEASE, "-n", NAMESPACE, "-o", "json") or "{}")
        if after != before or gateway_toml() != toml_before:
            print("  WARNING: the release values or the rendered gateway.toml differ from before the change; check "
                  f"`helm get values {RELEASE} -n {NAMESPACE}` and ConfigMap openshell-config", file=sys.stderr)
        else:
            print("  release values and the rendered gateway.toml back to what they were")
