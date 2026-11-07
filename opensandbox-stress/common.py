"""Shared helpers for the part 6 scripts (the stress test).

Where things come from:
  - ../opensandbox-eks/common.py (part 2), loaded by path, not copied: the server address
    (127.0.0.1:18080), the API key from ../opensandbox-eks/.env, env.sh, the workload image
    python:3.12-slim, connection(), require_server(), delete_all_sandboxes(), list_sandbox_ids()
  - ../opensandbox-lifecycle/common.py (part 3), loaded by path: ensure_port_forward(), the
    keeper that restarts `kubectl port-forward` to the server whenever /health stops answering
    (the forward ends each time a server change replaces the pod), and snapshot_token_status()
  - ../opensandbox-lifecycle/server_config.py (part 3): every server change goes through it
  - measure.py and checks.py in this directory: the statistics, the Sampler, the CSV writer and
    the pure decisions, kept apart so the tests run without a cluster

What this module adds:
  RUNS                       how many times each measurement runs (env RUNS, default 3); the
                             scripts report the median and the spread of the repeats
  own_metadata(script)       {"example": "opensandbox-stress", "script": ..., "run": ...}: every
                             sandbox made here carries it, and 99_cleanup.py deletes only those
  Sampler(name)              measure.Sampler wired to `kubectl get --raw .../stats/summary`
  server_show() / server_config(...) / restore_baseline() / server_session()
                             the server's settings, changed through server_config.py, put back
                             to runc + base egress + dns+nft + gateway in a finally
  preconditions(...)         refuses to measure on a cluster that is not quiet (see checks.py)
  create_sandbox(), run_in(), put_file(), delete_run(), pool helpers, kubectl()
  Plan                       what a script will do, printed by --dry-run and at the start of a run

Output: the terminal gets masked lines only (no account ID, no IP, no home directory; node names
become node-a and node-b). Full command output goes to logs/ (gitignored).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import measure
from checks import (
    BASELINE,
    EXAMPLE,
    helm_problems,
    helm_state,
    leftover_problems,
    other_pods,
    precondition_problems,
    put_back,
)
from measure import (  # noqa: F401  (re-exported for the scripts)
    RESULTS,
    any_bound,
    bound,
    CsvSink,
    NodeAliases,
    Result,
    describe,
    mask,
    parallel,
    pct,
    peaks_by_pod,
    read_csv,
    spread,
    window_cpu,
    window_stats,
)

HERE = Path(__file__).resolve().parent
EKS_DIR = HERE.parent / "opensandbox-eks"
LIFECYCLE_DIR = HERE.parent / "opensandbox-lifecycle"
EGRESS_DIR = HERE.parent / "opensandbox-egress"
AWS_CREDENTIALS_DIR = HERE.parent / "opensandbox-aws-credentials"
SERVER_CONFIG = LIFECYCLE_DIR / "server_config.py"
# OSB_STRESS_LOGS exists for the tests (tests/test_common.py's hang-up test).
LOGS = Path(os.environ.get("OSB_STRESS_LOGS") or HERE / "logs")
LOADGEN = HERE / "loadgen"


def load_module(name: str, path: Path):
    """Load a sibling directory's module by path (not copied), registered under `name`."""
    if not path.is_file():
        sys.exit(f"../{path.parent.name}/{path.name} not found: this directory needs the earlier parts' "
                 "companion code next to it")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if not (EKS_DIR / "env.sh").is_file():
    sys.exit("../opensandbox-eks/env.sh not found: run ../opensandbox-eks/01_cluster.sh first")
_eks = load_module("osb_eks_common", EKS_DIR / "common.py")
_lc = load_module("osb_lifecycle_common", LIFECYCLE_DIR / "common.py")

from opensandbox import SandboxSync  # noqa: E402
from opensandbox.exceptions import SandboxConnectionException  # noqa: E402
from opensandbox.models.execd import RunCommandOpts  # noqa: E402
from opensandbox.models.filesystem import WriteEntry  # noqa: E402
from opensandbox.models.sandboxes import CredentialProxyConfig, NetworkPolicy, NetworkRule, SandboxFilter  # noqa: E402
from opensandbox.sync import SandboxManagerSync  # noqa: E402

DOMAIN = _eks.DOMAIN
connection = _eks.connection
require_server = _eks.require_server
delete_all_sandboxes = _eks.delete_all_sandboxes
list_sandbox_ids = _eks.list_sandbox_ids
env_sh = _eks.env_sh
image = _eks.image
ensure_port_forward = _lc.ensure_port_forward
stop_port_forward = _lc.stop_port_forward
connect_server = _lc.connect_server
snapshot_token_status = _lc.snapshot_token_status
KUBE_CONTEXT = _lc.KUBE_CONTEXT
AWS_REGION = _lc.AWS_REGION
ECR_REGISTRY = _lc.ECR_REGISTRY
ECR_PREFIX = _lc.ECR_PREFIX
SANDBOX_NAMESPACE = _lc.SANDBOX_NAMESPACE
SYSTEM_NAMESPACE = _lc.SYSTEM_NAMESPACE
SNAPSHOT_REPO_NAME = _lc.SNAPSHOT_REPO_NAME
SNAPSHOT_SECRET = _lc.SNAPSHOT_SECRET
CLUSTER_NAME = env_sh("CLUSTER_NAME")
OWN = {_eks.OWN_KEY: EXAMPLE}

_runs = os.environ.get("RUNS", "3")
if not _runs.isdigit() or int(_runs) < 1:
    sys.exit(f"RUNS must be a positive whole number (got {_runs!r})")
RUNS = int(_runs)

POOL_NAME = "osb-stress-pool"
POOL = f"pools.sandbox.opensandbox.io/{POOL_NAME}"
BATCHSANDBOXES = "batchsandboxes.sandbox.opensandbox.io"
SNAPSHOTS = "sandboxsnapshots.sandbox.opensandbox.io"
# This directory's echo server (manifests/echo-stress.yaml: part 4's echo program, sized for load):
# plain HTTP on port 80. 05_egress_ladder.py creates it and deletes it; 02 only names it in a policy.
ECHO_HOST = "echo-stress.opensandbox.svc.cluster.local"
OWN_SELECTOR = f"example={EXAMPLE}"
ECHO_SELECTOR = "app=echo-stress,example=opensandbox-stress"
CLIENT_SELECTOR = "app=osb-stress-client,example=opensandbox-stress"
VMM_SELECTOR = "app=osb-stress-vmm,example=opensandbox-stress"
# Pinned by digest: the image runs privileged with the node's process table.
VMM_IMAGE = "public.ecr.aws/docker/library/busybox:stable@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
# The namespaces the Sampler keeps (every pod of the series, the server, the controller, the
# CloudWatch agent); the node totals are always kept.
SAMPLED_NAMESPACES = ("opensandbox", "opensandbox-system", "opensandbox-broker", "amazon-cloudwatch")
CREATED_LOG = RESULTS / "created-sandboxes.txt"
_T0 = time.monotonic()


def _exit_on_signal(signum, _frame) -> None:
    """SIGHUP (the terminal closed) and SIGTERM end the script like an exception, so every finally
    runs: sandboxes deleted, sampler pods deleted, the server put back.

    After a hang-up the terminal is gone, and the first print of a finally would fail with EIO and
    end it there. So for SIGHUP, stdout and stderr are pointed at logs/hangup.log first: the
    clean-up runs to the end and its output is in that file. A closed terminal sends SIGHUP twice
    (the kernel to the foreground group, the shell to its jobs): later ones are ignored, or the
    second would abort the clean-up the first one started. SIGTERM and SIGKILL still stop it."""
    if signum == signal.SIGHUP:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            fd = os.open(LOGS / "hangup.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.close(fd)
            print(f"--- {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} the terminal hung up: "
                  "cleaning up, output follows", flush=True)
        except OSError:
            pass
    raise SystemExit(128 + signum)


for _sig in (signal.SIGHUP, signal.SIGTERM):
    signal.signal(_sig, _exit_on_signal)


# --- output --------------------------------------------------------------------------------------

def show(text: str) -> str:
    """The text as it may be printed: no registry host, account ID, IP or home directory."""
    return mask(str(text), registry=ECR_REGISTRY)


def heading(text: str) -> None:
    print(f"\n\033[1m== {show(text)}\033[0m", flush=True)


def say(text: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{stamp}] +{time.monotonic() - _T0:6.1f}s  {show(text)}", flush=True)


def log(name: str, text: str) -> None:
    """Append to logs/<name>.log (gitignored, unmasked)."""
    LOGS.mkdir(exist_ok=True)
    with (LOGS / f"{name}.log").open("a") as out:
        out.write(f"--- {measure.utc_now()}\n{text}\n")


def save(name: str, data) -> Path | None:
    """results/<name>.json, every string masked (measure.save). Never raises: a result that cannot be
    written is reported in one line and kept in logs/."""
    try:
        path = measure.save(name, data, RESULTS, registry=ECR_REGISTRY)
    except (ValueError, OSError) as error:
        print(f"  FAIL  results/{name}.json not written: {show(str(error))[:200]}", flush=True)
        return None
    print(f"  results/{path.name}", flush=True)
    return path


class Failures:
    """Collects FAIL lines; report() prints them and returns the exit code."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text: str) -> None:
        self.items.append(show(text))
        print(f"  FAIL  {show(text)}", flush=True)

    def check(self, ok: bool, text: str) -> bool:
        if ok:
            print(f"  ok    {show(text)}", flush=True)
        else:
            self.add(text)
        return ok

    def report(self) -> int:
        if self.items:
            print(f"\n{len(self.items)} failure(s):")
            for item in self.items:
                print(f"  - {item}")
            return 1
        print("\nall steps passed")
        return 0


# --- the plan a script prints (--dry-run prints only this) ------------------------------------------

class Plan:
    """What a script will do: its steps (with an estimate), what it changes on the server and what
    it puts back. --dry-run prints this and touches nothing; a real run prints it first."""

    def __init__(self, title: str):
        self.title = title
        self.steps: list[tuple[str, float | None]] = []
        self.server_changes: list[str] = []
        self.restores: list[str] = []
        self.checks: list[str] = []
        self.writes: list[str] = []

    def step(self, text: str, minutes: float | None = None) -> None:
        self.steps.append((text, minutes))

    def minutes(self) -> float:
        return sum(m for _, m in self.steps if m)

    def print(self) -> None:
        print(f"\033[1m{self.title}\033[0m")
        if self.checks:
            print("  checks first:")
            for item in self.checks:
                print(f"    - {item}")
        print("  steps:")
        for i, (text, minutes) in enumerate(self.steps, 1):
            print(f"    {i:2}. {text}" + (f"  (~{minutes:g} min)" if minutes else ""))
        print("  changes to the server: " + ("; ".join(self.server_changes) if self.server_changes else "none"))
        print("  puts back at the end:  " + ("; ".join(self.restores) if self.restores else "nothing to put back"))
        if self.writes:
            print("  writes: " + ", ".join(self.writes))
        print(f"  estimated wall clock: about {self.minutes():.0f} min (RUNS={RUNS})", flush=True)


def cli(doc: str, add_arguments=None) -> argparse.Namespace:
    """argparse with the flags every measurement script has: --dry-run."""
    parser = argparse.ArgumentParser(description=(doc or "").split("\n\n")[0],
                                     epilog="RUNS=<n> in the environment repeats each measurement n times (default 3).",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan (steps, server changes, estimates) and touch nothing")
    if add_arguments:
        add_arguments(parser)
    return parser.parse_args()


# --- kubectl and the nodes -----------------------------------------------------------------------

def kubectl(*args: str, check: bool = True, timeout: float = 120, stdin: str | None = None) -> str:
    """kubectl pinned to the cluster's context. Returns stdout; raises RuntimeError (masked) on failure."""
    try:
        done = subprocess.run(["kubectl", "--context", KUBE_CONTEXT, *args], capture_output=True, text=True,
                              timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} did not finish within {timeout:.0f} s") from None
    if check and done.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {show((done.stderr or done.stdout).strip()[:300])}")
    return done.stdout


def kube_json(*args: str, check: bool = True, timeout: float = 120) -> dict:
    out = kubectl(*args, "-o", "json", check=check, timeout=timeout)
    return json.loads(out) if out.strip() else {}


_aliases: NodeAliases | None = None


def node_names() -> list[str]:
    return sorted(n.split("/", 1)[1] for n in kubectl("get", "nodes", "-o", "name").split())


def aliases() -> NodeAliases:
    """node-a, node-b for the node names (kept in logs/nodes.json, which is gitignored)."""
    global _aliases
    if _aliases is None:
        _aliases = NodeAliases(node_names())
        LOGS.mkdir(exist_ok=True)
        (LOGS / "nodes.json").write_text(json.dumps(_aliases.map, indent=1))
    return _aliases


def node_alias(name: str) -> str:
    return aliases()(name)


def stats_summary(node: str) -> dict:
    return json.loads(kubectl("get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary", timeout=20))


class Sampler(measure.Sampler):
    """measure.Sampler on this cluster: every node's kubelet stats/summary through the API server,
    every `every` seconds, into results/<name>.csv (t,node,pod,cpu_m,mem_mib)."""

    def __init__(self, name: str, every: float = 5.0, containers: bool = False):
        super().__init__(name, every, nodes=node_names(), fetch=stats_summary, alias=node_alias,
                         containers=containers, namespaces=SAMPLED_NAMESPACES, results=RESULTS)


def pod_node(pod: str) -> str:
    """The alias of the node a pod in the sandbox namespace runs on ("" when not placed)."""
    name = kubectl("-n", SANDBOX_NAMESPACE, "get", "pod", pod, "-o", "jsonpath={.spec.nodeName}", check=False).strip()
    return node_alias(name) if name else ""


def wait_until(condition, timeout: float, every: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(every)


def wait_no_pods(selector: str, timeout: float = 300) -> bool:
    """Wait until no pod in the sandbox namespace matches the label selector (deleted, not only
    Terminating: a terminating pod still holds its CPU, memory and pod slot)."""
    return wait_until(lambda: not kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", selector, "-o", "name",
                                          check=False).split(), timeout)


# --- the server's settings: everything through server_config.py -----------------------------------

class Child(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


def run_child(cmd: list[str], *, timeout: float, cwd: str | None = None) -> Child:
    """Run a command that changes the cluster (server_config.py, build_egress.sh) in its own session,
    so a Ctrl-C (or SIGHUP, SIGTERM) meant for this script never reaches it: killing it mid-upgrade
    would leave Helm running with server_config.py's lock released and the release pending. If this
    script is interrupted while it waits, it keeps waiting for the child to finish, as
    server_config.py's own run_upgrade() waits for helm, and then raises the interruption."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
                            start_new_session=True)
    interrupted: BaseException | None = None
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                proc.kill()
                out, err = proc.communicate()
                raise RuntimeError(f"{Path(cmd[1] if cmd[0] == sys.executable else cmd[0]).name} did not finish "
                                   f"within {timeout:.0f} s") from None
        except (KeyboardInterrupt, SystemExit) as error:
            if interrupted is None:
                print(f"\n  interrupted: waiting for {Path(cmd[1] if cmd[0] == sys.executable else cmd[0]).name} to "
                      "finish, so the server is not left mid-change", flush=True)
            interrupted = error
    if interrupted is not None:
        raise interrupted
    return Child(proc.returncode, out, err)


def server_show() -> dict:
    """`server_config.py --show --json`: {runtime, egress_image, egress_mode, ingress, gateway_values, revision}."""
    done = run_child([sys.executable, str(SERVER_CONFIG), "--show", "--json"], timeout=180)
    if done.returncode != 0:
        raise RuntimeError(f"server_config.py --show failed: {show(done.stderr.strip()[:300])}")
    return json.loads(done.stdout)


def helm_history() -> list[dict]:
    """`helm history` of the server release (read-only)."""
    done = subprocess.run(["helm", "--kube-context", KUBE_CONTEXT, "-n", SYSTEM_NAMESPACE, "history",
                           "opensandbox-server", "--max", "20", "-o", "json"], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"helm history failed: {show(done.stderr.strip()[:200])}")
    return json.loads(done.stdout or "[]")


_touched = False


def server_config(*flags: str) -> None:
    """Change the server with ../opensandbox-lifecycle/server_config.py (it keeps every setting not
    passed, holds its lock and checks /health, /version, the config and the gateway afterwards).
    Prints its lines masked; the full output goes to logs/server_config.log. The change replaces
    the server pod, which ends the port-forward: ensure_port_forward() starts a new one.

    Marks the run as having touched the server *before* it starts, so server_session() puts the
    baseline back even when this change fails half-way (a failed Helm upgrade leaves its objects
    live, and --show still reads the previous deployed revision)."""
    global _touched
    _touched = True
    say(f"server_config.py {' '.join(flags)}")
    started = time.monotonic()
    done = run_child([sys.executable, str(SERVER_CONFIG), *flags], timeout=1800)
    log("server_config", f"$ server_config.py {' '.join(flags)}\n{done.stdout}{done.stderr}")
    for line in (done.stdout + done.stderr).splitlines():
        if line.strip().startswith(("ok", "FAIL", "server now", "server wanted", "helm upgrade", "config.toml")):
            print(f"      {show(line.strip())[:180]}")
    ensure_port_forward()
    if done.returncode != 0:
        raise RuntimeError(f"server_config.py {' '.join(flags)} failed (exit {done.returncode}): see logs/server_config.log")
    say(f"server changed in {time.monotonic() - started:.0f} s")


def mark_server_touched() -> None:
    """For a server change made outside server_config() (build_egress.sh switches the image itself):
    server_session() then always runs --baseline."""
    global _touched
    _touched = True


def restore_baseline(force: bool = False) -> list[str]:
    """Put the server back to runc + base egress image + dns+nft + gateway (checks.put_back): after
    any attempted change (or with force) --baseline always runs; otherwise only when the Helm release
    is not settled on a deployed revision or --show differs. A pending Helm operation is waited for
    first. Returns what is still wrong (empty when the baseline holds)."""
    return put_back(touched=_touched or force, run_baseline=lambda: server_config("--baseline"), show=server_show,
                    history=helm_history)


_restore_failed = False


@contextmanager
def server_session():
    """with server_session(): ... changes the server freely; the finally always puts the baseline
    back after any attempted change and reads it back (--show --json and Helm's history). A failed
    restore makes restore_failed() true (exit 1). Nothing that happens during the restore, a second
    Ctrl-C included, skips the report."""
    global _restore_failed
    try:
        yield
    finally:
        try:
            heading("server back to the baseline (runc, base egress image, dns+nft, gateway)")
            problems = restore_baseline()
        except BaseException as error:  # noqa: BLE001 - reported, and the run fails
            problems = [f"{type(error).__name__}: {error}"]
        if problems:
            _restore_failed = True
            for item in problems:
                print(f"  FAIL  {show(item)}")
            print("  put it back by hand: python3 ../opensandbox-lifecycle/server_config.py --baseline")
        else:
            print(f"  ok    server_config.py --show --json: {json.dumps(BASELINE)}; Helm's latest revision is deployed")


def restore_failed() -> bool:
    return _restore_failed


# --- the sandboxes on the cluster and the preconditions --------------------------------------------

def census() -> list[dict]:
    """Every sandbox the server lists, all pages: [{id, state, metadata}]."""
    found: list[dict] = []
    with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
        page = 1
        while True:
            result = manager.list_sandbox_infos(SandboxFilter(page_size=100, page=page))
            found += [{"id": i.id, "state": str(i.status.state), "metadata": dict(i.metadata or {})}
                      for i in result.sandbox_infos]
            if not result.pagination.has_next_page:
                return found
            page += 1


def preconditions(want_server: dict | None = None, *, snapshots: bool = False) -> None:
    """Refuse to measure (exit 1, one line per problem) unless the cluster is quiet: no sandboxes
    (ours or another part's), no BatchSandbox the server does not list, no Pool, the server on
    `want_server` (default the baseline) and two nodes Ready. snapshots=True also needs part 3's
    snapshot registry Secret with a token valid for an hour."""
    heading("preconditions")
    connect_server()
    want = want_server or BASELINE
    state = server_show()
    nodes = kube_json("get", "nodes")
    sandboxes = census()
    batch = len(kubectl("-n", SANDBOX_NAMESPACE, "get", BATCHSANDBOXES, "-o", "name", check=False).split())
    pools = [p.split("/", 1)[1] for p in kubectl("-n", SANDBOX_NAMESPACE, "get", "pools.sandbox.opensandbox.io",
                                                 "-o", "name", check=False).split()]
    problems = precondition_problems(server_state=state, want_server=want, nodes=nodes, sandboxes=sandboxes,
                                     batchsandboxes=batch, pools=pools)
    problems += helm_problems(helm_state(helm_history()))
    ours = []
    for namespace in (SANDBOX_NAMESPACE, SYSTEM_NAMESPACE):
        found = kube_json("-n", namespace, "get", "pods,deployments,jobs,configmaps", "-l", OWN_SELECTOR)
        # Sandbox pods carry the label too; the sandbox check above covers them.
        ours += [o for o in found.get("items", []) if "opensandbox.io/id" not in (o["metadata"].get("labels") or {})]
    problems += leftover_problems(ours)
    strangers = other_pods(kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", check=False))
    if strangers:
        print(f"  note  other pods in {SANDBOX_NAMESPACE}, measured along with everything else: {', '.join(strangers[:6])}")
    if snapshots:
        answer, expires = snapshot_token_status()
        if not expires:
            problems.append(f"Secret {SANDBOX_NAMESPACE}/{SNAPSHOT_SECRET} not found: run ../opensandbox-lifecycle/00_snapshot_config.sh")
        elif answer != 200:
            problems.append(f"ECR answers {answer or 'nothing'} to the snapshot token: run ../opensandbox-lifecycle/00_snapshot_config.sh")
        elif datetime.fromisoformat(expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc) + timedelta(hours=1):
            problems.append(f"the snapshot token expires at {expires} (within the hour): run ../opensandbox-lifecycle/00_snapshot_config.sh")
    if problems:
        for item in problems:
            print(f"  FAIL  {show(item)}")
        sys.exit("preconditions not met: nothing was measured")
    print(f"  ok    no sandboxes, no Pools, none of this directory's objects; server {state['runtime']}, egress "
          f"{state['egress_image']} {state['egress_mode']}, ingress {state['ingress']} (revision {state['revision']}, "
          f"Helm's latest); {len(nodes.get('items', []))} nodes Ready")


# --- sandboxes -------------------------------------------------------------------------------------

def own_metadata(script: str, run_id: str | None = None) -> dict[str, str]:
    """{"example": "opensandbox-stress", "script": <script>, "run": <run id>}. The metadata becomes pod
    labels: list and delete calls filter on it, and 99_cleanup.py deletes example=opensandbox-stress."""
    return {**_eks.own_metadata(script, run_id), _eks.OWN_KEY: EXAMPLE}


def new_run_id() -> str:
    return uuid.uuid4().hex[:10]


def deny_policy(*hosts: str) -> NetworkPolicy:
    """defaultAction deny plus an allow rule per host."""
    return NetworkPolicy(default_action="deny", egress=[NetworkRule(action="allow", target=h) for h in hosts] or None)


def create_sandbox(meta: dict[str, str], *, image_ref: str | None = None, resource: dict | None = None,
                   resource_requests: dict | None = None, policy: NetworkPolicy | None = None, proxy: bool = False,
                   extensions: dict | None = None, env: dict | None = None, ttl_minutes: int = 30,
                   ready_seconds: int = 180, retry: bool = True) -> SandboxSync:
    """Create a sandbox with our metadata. retry=True retries once when the call failed because the
    port-forward died (a measurement passes retry=False: a retried create is not one sample)."""
    def once() -> SandboxSync:
        return SandboxSync.create(
            image_ref or image(), metadata=meta, resource=resource, resource_requests=resource_requests,
            network_policy=policy, credential_proxy=CredentialProxyConfig(enabled=True) if proxy else None,
            extensions=extensions, env=env, timeout=timedelta(minutes=ttl_minutes),
            ready_timeout=timedelta(seconds=ready_seconds), connection_config=connection("proxy"))

    try:
        return once()
    except SandboxConnectionException:
        if not retry or _lc._healthy():
            raise
        ensure_port_forward()
        return once()


class Out(NamedTuple):
    rc: int
    text: str


_RC = re.compile(r"@@rc=(\d+)\s*$")


def run_in(sb: SandboxSync, command: str, timeout: int | None = None, background: bool = False) -> Out:
    """Run shell text in the sandbox; (exit code, stdout and stderr together). 124: no exit code came
    back. timeout defaults to 120 s. One retry if the port-forward died.

    background=True starts it and returns at once (rc 0), and needs an explicit timeout: execd
    applies the timeout to a background command too, and kills its whole process group when it runs
    out (so a background job meant to last 15 minutes needs at least that)."""
    if timeout is None:
        if background:
            raise ValueError("run_in(background=True) needs timeout=: execd kills a background command's "
                             "process group when its timeout runs out")
        timeout = 120
    wrapped = command if background else f"( {command}\n) 2>&1; echo \"@@rc=$?\""
    opts = RunCommandOpts(timeout=timedelta(seconds=timeout), background=background)
    for attempt in (1, 2):
        try:
            execution = sb.commands.run(wrapped, opts=opts)
            break
        except Exception:
            if attempt == 2 or _lc._healthy():
                raise
            ensure_port_forward()
    if background:
        return Out(0, "")
    text = "".join(part.text for part in execution.logs.stdout)
    match = _RC.search(text)
    if match:
        return Out(int(match.group(1)), text[: match.start()].rstrip("\n"))
    return Out(124, text.rstrip("\n"))


def put_file(sb: SandboxSync, path: str, text: str) -> None:
    sb.files.write_files([WriteEntry(path=path, data=text, mode=644)])


def put_loadgen(sb: SandboxSync, name: str) -> str:
    """Copy loadgen/<name> into the sandbox's /tmp; returns the path there."""
    target = f"/tmp/{name}"
    put_file(sb, target, (LOADGEN / name).read_text())
    return target


def last_json(text: str) -> dict:
    """The last line of command output that is a JSON object (the load generators print one)."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise ValueError(f"no JSON result in the output: {show(text[-300:])}")


def delete_run(run_id: str) -> int:
    """Delete one run's sandboxes (metadata example=opensandbox-stress, run=<run id>). An error, or the
    helper's own exit after 2 minutes, is reported and not raised; an interruption (Ctrl-C, or the
    SystemExit >= 128 that SIGTERM and SIGHUP become) is raised, so the script stops measuring."""
    try:
        ensure_port_forward()
        return delete_all_sandboxes(metadata={**OWN, "run": run_id})
    except KeyboardInterrupt:
        raise
    except SystemExit as error:
        if isinstance(error.code, int) and error.code >= 128:
            raise
        print(f"  clean up did not finish: {show(str(error.code))[:200]}; 99_cleanup.py removes the rest", file=sys.stderr)
        return 0
    except Exception as error:  # noqa: BLE001
        print(f"  clean up did not finish: {show(str(error))[:200]}; 99_cleanup.py removes the rest", file=sys.stderr)
        return 0


def record_created(sandbox_id: str) -> None:
    """Remember a sandbox that pauses, so 99_cleanup.py deletes its snapshot images and no others."""
    CREATED_LOG.parent.mkdir(exist_ok=True)
    with CREATED_LOG.open("a") as out:
        out.write(sandbox_id + "\n")


# --- the Pool --------------------------------------------------------------------------------------

def render_manifest(name: str, **values: str) -> str:
    """manifests/<name> with ${ECR_REGISTRY}, ${ECR_PREFIX} and the given values filled in."""
    text = (HERE / "manifests" / name).read_text()
    return measure.render_template(text, {"ECR_REGISTRY": ECR_REGISTRY, "ECR_PREFIX": ECR_PREFIX, **values},
                                   f"manifests/{name}")


def render_pool(*, cpu: str, memory: str, buffer_min: int, buffer_max: int, pool_max: int) -> str:
    """manifests/pool.yaml with the registry, the shape and the capacity filled in."""
    return render_manifest("pool.yaml", POOL_NAME=POOL_NAME, CPU=cpu, MEMORY=memory, BUFFER_MIN=str(buffer_min),
                           BUFFER_MAX=str(buffer_max), POOL_MAX=str(pool_max))


def pool_status() -> dict:
    out = kubectl("-n", SANDBOX_NAMESPACE, "get", POOL, "-o", "jsonpath={.status}", check=False).strip()
    return json.loads(out) if out else {}


def apply_pool(**shape) -> None:
    kubectl("apply", "-f", "-", stdin=render_pool(**shape))


def wait_pool_available(minimum: int, timeout: float = 600) -> float:
    """Seconds until the Pool has `minimum` Ready, unallocated pods. Raises RuntimeError on timeout."""
    started = time.monotonic()
    if wait_until(lambda: pool_status().get("available", 0) >= minimum, timeout, every=1.0):
        return time.monotonic() - started
    raise RuntimeError(f"Pool {POOL_NAME} did not reach {minimum} available pods in {timeout:.0f} s ({pool_status()})")


def wait_pool_full(size: int, timeout: float = 900) -> float:
    """Seconds until the Pool has `size` Ready, unallocated pods *and* none of its released pods is
    still terminating (the kubelet's teardown of those would overlap the next measured round)."""
    started = time.monotonic()
    wait_pool_available(size, timeout=timeout)

    def settled() -> bool:
        pods = kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"sandbox.opensandbox.io/pool-name={POOL_NAME}",
                         check=False)
        return not any(p["metadata"].get("deletionTimestamp") for p in pods.get("items", []))

    if not wait_until(settled, max(30.0, timeout - (time.monotonic() - started)), every=2.0):
        raise RuntimeError(f"Pool {POOL_NAME}: released pods still terminating after {timeout:.0f} s")
    return time.monotonic() - started


def delete_pool() -> None:
    kubectl("-n", SANDBOX_NAMESPACE, "delete", POOL, "--ignore-not-found", "--wait=true", "--timeout=180s", check=False)
    wait_no_pods(f"sandbox.opensandbox.io/pool-name={POOL_NAME}", timeout=300)


class KubeApi:
    """`kubectl proxy` on a free 127.0.0.1 port, for calls that must be timed without a kubectl
    process (and its credential plugin) per call: JSON requests and watches through httpx.
    with KubeApi() as api: api.get("/version")"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.base = ""
        self.client = None

    def __enter__(self) -> "KubeApi":
        import httpx

        LOGS.mkdir(exist_ok=True)
        self.proc = subprocess.Popen(["kubectl", "--context", KUBE_CONTEXT, "proxy", "--port=0", "--address=127.0.0.1"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        line = self.proc.stdout.readline() if self.proc.stdout else ""
        match = re.search(r"127\.0\.0\.1:(\d+)", line)
        if not match:
            self.proc.terminate()
            raise RuntimeError(f"kubectl proxy did not start: {show(line.strip())[:200]}")
        self.base = f"http://127.0.0.1:{match.group(1)}"
        self.client = httpx.Client(timeout=60)
        # Keep reading what the proxy prints (into logs/), so a full pipe never blocks it.
        threading.Thread(target=self._drain, daemon=True).start()
        return self

    def _drain(self) -> None:
        with (LOGS / "kubectl-proxy.log").open("a") as log_file:
            for line in self.proc.stdout:
                log_file.write(line)

    def __exit__(self, *exc) -> None:
        if self.client is not None:
            self.client.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        reply = self.client.request(method, self.base + path, json=body)
        if reply.status_code >= 300 and not (method == "DELETE" and reply.status_code == 404):
            raise RuntimeError(f"{method} {path.split('?')[0]}: HTTP {reply.status_code} {show(reply.text[:200])}")
        return reply.json() if reply.content else {}

    def watch(self, path: str, resource_version: str, done, timeout: float = 120,
              name: str | None = None) -> float | None:
        """Watch the collection at `path` (only the object `name`, if given) from `resource_version`;
        return time.monotonic() of the first event whose object makes done(obj) true, or None after
        `timeout` seconds."""
        deadline = time.monotonic() + timeout
        params = {"watch": "1", "resourceVersion": resource_version, "timeoutSeconds": str(int(timeout)),
                  "allowWatchBookmarks": "false"}
        if name:
            params["fieldSelector"] = f"metadata.name={name}"
        with self.client.stream("GET", self.base + path, params=params, timeout=timeout + 10) as reply:
            for line in reply.iter_lines():
                now = time.monotonic()
                if line.strip():
                    event = json.loads(line)
                    if done(event.get("object", {})):
                        return now
                if now >= deadline:
                    break
        return None


def pod_slots() -> tuple[list[int], int]:
    """(allocatable pod slots of each node, pods placed on the nodes now and not finished)."""
    nodes = kube_json("get", "nodes")
    slots = [int(n["status"]["allocatable"].get("pods", 0)) for n in nodes.get("items", [])]
    pods = kube_json("get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed")
    placed = sum(1 for p in pods.get("items", []) if p.get("spec", {}).get("nodeName"))
    return slots, placed


# --- the echo server for the ladder (manifests/echo-stress.yaml) --------------------------------------

def echo_up() -> None:
    """Apply manifests/echo-stress.yaml and wait for its one pod to be ready."""
    kubectl("apply", "-f", "-", stdin=render_manifest("echo-stress.yaml"))
    kubectl("-n", SANDBOX_NAMESPACE, "rollout", "status", "deploy/echo-stress", "--timeout=180s", timeout=200)


def echo_down() -> None:
    """Delete the echo server (Deployment, Service, ConfigMap; by its labels). Never raises."""
    kubectl("-n", SANDBOX_NAMESPACE, "delete", "deployments,services,configmaps", "-l", ECHO_SELECTOR,
            "--ignore-not-found", "--wait=true", "--timeout=120s", check=False, timeout=150)


# --- the node-side VM sampler (manifests/vmm-sampler.yaml) --------------------------------------------

class VmmSampler:
    """One privileged hostPID pod per node that prints the RSS of every Kata VM's processes (QEMU,
    virtiofsd, the shim) every `every` seconds.

    A thread reads the pods' logs every 20 s while the block runs, so the samples are in hand
    before the pods go (and in logs/vmm-<node>.log). now() reads the last half minute. The exit
    deletes the pods first (--wait=false, never depending on a log read), then takes a last log read
    only if they can still be read, and checks the pods are gone."""

    POLL_S = 20

    def __init__(self, every: int = 5):
        self.every = every
        self.pods: dict[str, str] = {}      # pod name -> node alias
        self._seen: dict[tuple, dict] = {}  # (node, t, kind, pid) -> sample
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def samples(self) -> list[dict]:
        with self._lock:
            return list(self._seen.values())

    def _keep(self, text: str, alias: str) -> list[dict]:
        found = measure.parse_vmm(text, alias)
        with self._lock:
            for item in found:
                self._seen[(item["node"], item["t"], item["kind"], item["pid"])] = item
        return found

    def _read(self, since_s: int | None, timeout: float = 30) -> list[dict]:
        out: list[dict] = []
        for name, alias in list(self.pods.items()):
            args = ["-n", SANDBOX_NAMESPACE, "logs", name] + ([f"--since={since_s}s"] if since_s else [])
            try:
                text = kubectl(*args, check=False, timeout=timeout)
            except (RuntimeError, OSError):
                continue
            LOGS.mkdir(exist_ok=True)
            with (LOGS / f"vmm-{alias}.log").open("a") as handle:
                handle.write(text)
            out += self._keep(text, alias)
        return out

    def _loop(self) -> None:
        while not self._stop.wait(self.POLL_S):
            self._read(self.POLL_S + 10)

    def __enter__(self) -> "VmmSampler":
        try:
            for node in node_names():
                alias = node_alias(node)
                name = f"osb-stress-vmm-{alias}"
                kubectl("-n", SANDBOX_NAMESPACE, "delete", "pod", name, "--ignore-not-found", "--wait=true", check=False)
                self.pods[name] = alias
                kubectl("apply", "-f", "-", stdin=render_manifest("vmm-sampler.yaml", NAME=name, NODE=node,
                                                                  EVERY=str(self.every)))
            for name in self.pods:
                kubectl("-n", SANDBOX_NAMESPACE, "wait", "--for=condition=Ready", f"pod/{name}", "--timeout=180s",
                        timeout=200)
        except BaseException:
            self.__exit__(None, None, None)     # no sampler pod is left behind
            raise
        self._thread = threading.Thread(target=self._loop, name="vmm-logs", daemon=True)
        self._thread.start()
        say(f"node-side VM sampler running on {len(self.pods)} node(s), every {self.every} s")
        return self

    def now(self, since_s: int = 30) -> list[dict]:
        return self._read(since_s, timeout=60)

    def __exit__(self, *exc) -> None:
        self._stop.set()
        try:
            kubectl("-n", SANDBOX_NAMESPACE, "delete", "pods", "-l", VMM_SELECTOR, "--ignore-not-found", "--wait=false",
                    check=False, timeout=30)
        except BaseException as error:  # noqa: BLE001 - retried below, and 99_cleanup.py deletes by label
            print(f"  VM sampler delete: {show(str(error))[:160]}", file=sys.stderr)
        try:
            self._read(self.POLL_S + 15, timeout=15)   # what the last poll missed, if the logs are still there
        except BaseException:  # noqa: BLE001 - the samples gathered while running are kept
            pass
        try:
            if not wait_until(lambda: not kubectl("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", VMM_SELECTOR, "-o",
                                                  "name", check=False, timeout=30).split(), 120, every=3):
                kubectl("-n", SANDBOX_NAMESPACE, "delete", "pods", "-l", VMM_SELECTOR, "--ignore-not-found",
                        "--force", "--grace-period=0", check=False, timeout=30)
                print("  WARN  VM sampler pods were still there after 2 minutes: deleted with --force", file=sys.stderr)
        except BaseException as error:  # noqa: BLE001
            print(f"  FAIL  VM sampler pods may be left: {show(str(error))[:160]}; run uv run python 99_cleanup.py",
                  file=sys.stderr)


def pods_by_sandbox(selector: str) -> dict[str, dict]:
    """{sandbox id: {pod, node}} for the pods matching the selector, by their opensandbox.io/id label
    (not by assuming the pod is named <id>-0)."""
    out: dict[str, dict] = {}
    for pod in kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", selector, timeout=60).get("items", []):
        sandbox = (pod["metadata"].get("labels") or {}).get("opensandbox.io/id")
        if sandbox and not pod["metadata"].get("deletionTimestamp"):
            node = pod.get("spec", {}).get("nodeName")
            out[sandbox] = {"pod": pod["metadata"]["name"], "node": node_alias(node) if node else ""}
    return out
