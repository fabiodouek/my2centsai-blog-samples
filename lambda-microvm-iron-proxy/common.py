"""Shared helpers for the iron-proxy tests: Terraform outputs, image build, run() on the sandbox VPC's egress
connector (transparent by default: no proxy settings), a VM wrapper around the in-VM agent, the iron-proxy
audit log, the Resolver query log, rollout-aware `terraform apply`, and redaction of account-specific values.

Every script writes what it prints to results/<script>.log, redacted, and ends with a list of checks."""

import atexit
import hashlib
import io
import json
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
import time
import zipfile
from functools import cache
from pathlib import Path

import boto3
import httpx
from botocore.config import Config
from dotenv import load_dotenv

HERE = Path(__file__).parent
RESULTS = HERE / "results"
load_dotenv(HERE / ".env")

PREFIX = "my2cents-ip"
REGION = os.environ.get("AWS_REGION", "us-east-1")
MANAGED = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:"
HTTP_INGRESS = MANAGED + "HTTP_INGRESS"
BASE_IMAGE = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
IMAGE = f"{PREFIX}-sandbox"
REPO = os.environ.get("GITHUB_REPO", "fabiodouek/my2centsai-sandbox-test")

# Placeholder AWS keys: the CLI and SDKs refuse to send a request without something to sign with;
# aws_auth throws the signature away and re-signs with the proxy's task role.
AWS_PLACEHOLDER = {"AWS_ACCESS_KEY_ID": "AKIAPLACEHOLDER00000", "AWS_SECRET_ACCESS_KEY": "placeholder-secret-not-real",
                   "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION, "AWS_EC2_METADATA_DISABLED": "true"}

# Where each client looks for the CA that signs iron-proxy's certificates (the image adds it to the system
# store). The Dockerfile sets these too; the payload repeats them so every shell that sources
# /etc/sandbox.env gets them. Node adds to its own roots; the others replace theirs with the system bundle.
CA_ENV = {"NODE_EXTRA_CA_CERTS": "/usr/local/share/ca-certificates/iron-proxy-demo.crt",
          "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
          "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
          "AWS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt"}

SAFETY_IDLE = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 1800, "suspendedDurationSeconds": 600}
SAFETY_MAX_DURATION = 3600

HOOKS = {
    "port": 9001,
    "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 30, "resume": "ENABLED", "resumeTimeoutInSeconds": 30,
                     "suspend": "ENABLED", "suspendTimeoutInSeconds": 30, "terminate": "ENABLED", "terminateTimeoutInSeconds": 30},
    "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 300, "validate": "ENABLED", "validateTimeoutInSeconds": 300},
}

# RunMicrovm is limited to ~1 TPS per account: run() backs off on its own, so its client doesn't retry.
# Everything else (get, list, suspend, terminate...) retries, since other projects may share the API limits.
_mvm_run = boto3.client("lambda-microvms", config=Config(region_name=REGION, retries={"max_attempts": 1, "mode": "standard"}))
mvm = boto3.client("lambda-microvms", config=Config(region_name=REGION, retries={"max_attempts": 10, "mode": "adaptive"}))
logs = boto3.client("logs", config=Config(region_name=REGION))
s3 = boto3.client("s3", config=Config(region_name=REGION))
sm = boto3.client("secretsmanager", config=Config(region_name=REGION))
ecs = boto3.client("ecs", config=Config(region_name=REGION))
ec2 = boto3.client("ec2", config=Config(region_name=REGION))
elb = boto3.client("elbv2", config=Config(region_name=REGION))
r53r = boto3.client("route53resolver", config=Config(region_name=REGION))


# ---------- Terraform outputs ----------

def tf(refresh: bool = False) -> dict:
    if refresh:
        _tf.cache_clear()
    return _tf()


@cache
def _tf() -> dict:
    out = subprocess.run(["terraform", f"-chdir={HERE / 'infra'}", "output", "-json"],
                         capture_output=True, text=True, check=True).stdout
    return {k: v["value"] for k, v in json.loads(out).items()}


@cache
def account_id() -> str:
    return boto3.client("sts", config=Config(region_name=REGION)).get_caller_identity()["Account"]


# ---------- redaction and logging ----------

def anon(name: str, kind: str = "bucket") -> str:
    """A stable, meaningless stand-in for a resource name (bucket-3f2a)."""
    return f"{kind}-{hashlib.sha256(name.encode()).hexdigest()[:4]}"


_SECRET_ARN = re.compile(r"arn:aws:secretsmanager:[a-z0-9-]+:(?:\d{12}|<acct>):secret:" + PREFIX + r"/([a-z]+)-[A-Za-z0-9-]+")


def redact(text) -> str:
    """Hide the account ID, this repo's bucket names, the NAT's public IP, secret ARNs and real secret values.
    REDACT=0 in .env shows everything except the real secret values."""
    s = text if isinstance(text, str) else json.dumps(text, indent=2, default=str)
    for k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        v = os.environ.get(k)
        if v and len(v) > 8:
            s = s.replace(v, f"<REAL {k} VALUE>")
    if os.environ.get("REDACT", "1") == "0":
        return s
    try:
        s = s.replace(account_id(), "<acct>")
    except Exception:
        s = re.sub(r"\b\d{12}\b", "<acct>", s)
    s = _SECRET_ARN.sub(lambda m: f"<secret-arn:{m.group(1)}>", s)
    try:
        t = tf()
        s = (s.replace(t["artifacts_bucket"], "<artifacts-bucket>").replace(t["demo_bucket"], "<demo-bucket>")
             .replace(t["nat_public_ip"], "<nat-eip>"))
    except Exception:
        pass
    return s


class _Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        s = redact(s) if s.strip() else s
        for st in self.streams:
            st.write(s)
            st.flush()
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def start_log(name: str) -> Path:
    """Tee everything printed (redacted) to results/<name>.log."""
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{name}.log"
    f = open(path, "w")
    sys.stdout = _Tee(sys.__stdout__, f)
    print(f"# {name}  {time.strftime('%Y-%m-%d %H:%M:%S %Z')}  region {REGION}")
    return path


def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def err(e: Exception) -> str:
    code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
    return redact(f"{code or type(e).__name__}: {str(e)[:260]}")


def median(xs):
    return statistics.median(xs) if xs else float("nan")


def rand_label(n: int = 8) -> str:
    return "r" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    """Record one pass/fail result. They're printed together by summary(), so the test output above stays
    readable."""
    CHECKS.append((name, bool(ok), str(detail)))
    return bool(ok)


def summary() -> None:
    """Print the checks and exit 1 if any failed."""
    banner("Checks")
    for name, ok, detail in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail[:160]})" if detail else ""))
    failed = sum(not ok for _, ok, _ in CHECKS)
    print(f"{len(CHECKS) - failed} passed, {failed} failed")
    sys.stdout.flush()
    if failed:
        sys.exit(1)


# ---------- the shared MicroVM quota lock (only when MVM_LOCK is set) ----------
# Several projects can share one account's MicroVM memory quota. With MVM_LOCK=<dir>, run() takes the lock
# (mkdir) before creating a MicroVM, and terminate() releases it when this project has none left.

LOCK = os.environ.get("MVM_LOCK")
LOCK_OWNER = f"lambda-microvm-iron-proxy ({PREFIX})"


def _lock_acquire() -> None:
    if not LOCK:
        return
    while True:
        try:
            os.mkdir(LOCK)
            Path(LOCK, "owner").write_text(f"{LOCK_OWNER} {time.strftime('%Y-%m-%d %H:%M:%S %Z')} pid {os.getpid()}\n")
            break
        except FileExistsError:
            try:
                owner = Path(LOCK, "owner").read_text()
            except OSError:
                owner = ""
            if owner.startswith(LOCK_OWNER):
                break
            print(f"[lock] held by: {owner.strip() or '?'}; waiting 60 s", file=sys.__stderr__)
            time.sleep(60)
    Path(LOCK, "refs").mkdir(exist_ok=True)


def _lock_ref(name: str, add: bool) -> None:
    if not LOCK:
        return
    p = Path(LOCK, "refs", name)
    if add:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(os.getpid()))
        return
    p.unlink(missing_ok=True)
    try:
        if Path(LOCK, "owner").read_text().startswith(LOCK_OWNER) and not any(Path(LOCK, "refs").iterdir()):
            subprocess.run(["rm", "-rf", LOCK], check=False)
    except OSError:
        pass


# ---------- image ----------

def image_arn() -> str:
    return f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{IMAGE}"


def build_image() -> dict:
    """Build images/sandbox with the demo CA baked in. The build uses the default INTERNET_EGRESS connector
    (apt, npm, GitHub releases); the sandbox VPC's connector is only attached at run time."""
    ca = tf()["ca_cert_pem"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((HERE / "images" / "sandbox").iterdir()):
            if f.is_file() and f.name != "ca.crt":
                z.write(f, f.name)
        z.writestr("ca.crt", ca)
    (HERE / "images" / "sandbox" / "ca.crt").write_text(ca)
    key = f"{IMAGE}-{int(time.time())}.zip"
    s3.put_object(Bucket=tf()["artifacts_bucket"], Key=key, Body=buf.getvalue())
    params = {"codeArtifact": {"uri": f"s3://{tf()['artifacts_bucket']}/{key}"}, "baseImageArn": BASE_IMAGE,
              "buildRoleArn": tf()["build_role_arn"], "resources": [{"minimumMemoryInMiB": 2048}], "hooks": HOOKS}
    _lock_acquire()
    _lock_ref("image-build", True)
    t0 = time.perf_counter()
    try:
        try:
            mvm.get_microvm_image(imageIdentifier=image_arn())
            mvm.update_microvm_image(imageIdentifier=image_arn(), **params)
        except mvm.exceptions.ResourceNotFoundException:
            mvm.create_microvm_image(name=IMAGE, tags={"demo": "my2cents-iron-proxy"}, **params)
        while (d := mvm.get_microvm_image(imageIdentifier=image_arn()))["state"] in ("CREATING", "UPDATING"):
            time.sleep(5)
    finally:
        _lock_ref("image-build", False)
    return {"image": d, "seconds": round(time.perf_counter() - t0, 1)}


# ---------- per-MicroVM settings ----------

def settings(mode: str = "transparent", extra_env: dict | None = None, placeholders: bool = True) -> dict:
    """What the run hook payload tells the agent. Nothing secret.

    transparent (default): no proxy variables at all. DNS sends every name to iron-proxy.
    explicit: HTTPS_PROXY to the NLB on 8080, only for the comparison in 11 (14 uses curl -x from a
    transparent MicroVM).

    Both get the CA settings and, unless placeholders=False, the placeholder credentials."""
    t = tf()
    env = dict(CA_ENV)
    if mode == "explicit":
        proxy = f"http://{t['nlb_ips'][0]}:8080"
        env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, https_proxy=proxy, http_proxy=proxy,
                   NO_PROXY="localhost,127.0.0.1,169.254.169.254", no_proxy="localhost,127.0.0.1,169.254.169.254")
    elif mode != "transparent":
        raise ValueError(f"mode must be transparent or explicit, not {mode!r}")
    if placeholders:
        p = t["placeholders"]
        env.update(ANTHROPIC_API_KEY=p["anthropic"], GH_TOKEN=p["github"], DEMO_TOKEN=p["demo"], **AWS_PLACEHOLDER)
    env.update(extra_env or {})
    return {"env": env, "nameserver": None}


# ---------- running MicroVMs ----------

_LIVE: dict[str, "VM"] = {}


def run(test: str = "adhoc", *, mode: str = "transparent", role: str | None = None, idle: dict | None = SAFETY_IDLE,
        max_duration: int = SAFETY_MAX_DURATION, extra_env: dict | None = None, placeholders: bool = True) -> "VM":
    """run_microvm on the sandbox VPC's egress connector, settings in the run hook payload, and no execution
    role unless `role` names one (only 08_leaks.py's contrast run uses role="exec_logs")."""
    payload = settings(mode, extra_env, placeholders)
    payload["test"] = test
    params = {
        "imageIdentifier": image_arn(),
        "ingressNetworkConnectors": [HTTP_INGRESS],
        "egressNetworkConnectors": [tf()["connector_arn"]],
        "maximumDurationInSeconds": max_duration,
        "runHookPayload": json.dumps(payload),
    }
    if idle:
        params["idlePolicy"] = idle
    if role:
        params["executionRoleArn"] = tf()[f"{role}_role_arn"]
    _lock_acquire()
    pending = f"pending-{os.getpid()}-{time.time_ns()}"
    _lock_ref(pending, True)
    try:
        for attempt in range(8):
            t0 = time.perf_counter()
            try:
                r = _mvm_run.run_microvm(**params)
            except _mvm_run.exceptions.ThrottlingException:
                time.sleep(min(8, 0.5 * 2**attempt) + random.random() * 0.3)
                continue
            vm = VM(r["microvmId"], r["endpoint"], t0)
            _lock_ref(vm.id, True)
            _LIVE[vm.id] = vm
            vm.run_api_s = time.perf_counter() - t0
            vm.running_s = vm.wait("RUNNING") + vm.run_api_s
            vm.ready()
            vm.ready_s = time.perf_counter() - t0
            return vm
        raise RuntimeError("RunMicrovm still throttled after 8 attempts")
    finally:
        _lock_ref(pending, False)


class VM:
    def __init__(self, vm_id: str, endpoint: str, t0: float):
        self.id, self.endpoint, self.t0 = vm_id, endpoint, t0
        self._tok = None
        self.http = httpx.Client(timeout=httpx.Timeout(900, connect=15))

    def state(self) -> str:
        return mvm.get_microvm(microvmIdentifier=self.id)["state"]

    def wait(self, *want: str, timeout: float = 300, poll: float = 0.25) -> float:
        start = time.perf_counter()
        while (s := self.state()) not in want:
            if s == "TERMINATED":
                d = mvm.get_microvm(microvmIdentifier=self.id)
                raise RuntimeError(f"{self.id} TERMINATED: {d.get('terminationMessage')}")
            if time.perf_counter() - start > timeout:
                raise TimeoutError(f"{self.id} still {s}")
            time.sleep(poll)
        return time.perf_counter() - start

    def token(self) -> str:
        if not self._tok or self._tok[1] < time.time() + 60:
            r = mvm.create_microvm_auth_token(microvmIdentifier=self.id, expirationInMinutes=60, allowedPorts=[{"port": 8080}])
            self._tok = (r["authToken"]["X-aws-proxy-auth"], time.time() + 3600)
        return self._tok[0]

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, f"https://{self.endpoint}{path}", headers={"X-aws-proxy-auth": self.token()}, **kw)

    def ready(self, timeout: float = 60) -> None:
        start = time.perf_counter()
        while time.perf_counter() - start < timeout:
            try:
                if self.request("GET", "/health", timeout=10).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise TimeoutError(f"{self.id} agent not answering")

    def exec(self, cmd: str, timeout: int = 120) -> dict:
        r = self.request("POST", "/exec", json={"cmd": cmd, "timeout": timeout}, timeout=timeout + 30)
        r.raise_for_status()
        return r.json()

    def sh(self, cmd: str, timeout: int = 120) -> str:
        r = self.exec(cmd, timeout)
        return redact((r["stdout"] + r["stderr"]).strip())

    def put(self, path: str, data: bytes | str) -> None:
        self.request("PUT", f"/files?path={path}", content=data if isinstance(data, bytes) else data.encode()).raise_for_status()

    def terminate(self, wait: bool = False) -> float:
        t0 = time.perf_counter()
        try:
            mvm.terminate_microvm(microvmIdentifier=self.id)
            if wait:
                while self.state() != "TERMINATED" and time.perf_counter() - t0 < 120:
                    time.sleep(0.25)
        except Exception as e:
            print(f"terminate {self.id}: {err(e)}")
        _LIVE.pop(self.id, None)
        _lock_ref(self.id, False)
        return time.perf_counter() - t0


@atexit.register
def _terminate_leftovers() -> None:
    for vm in list(_LIVE.values()):
        print(f"[atexit] terminating {vm.id}", file=sys.__stderr__)
        vm.terminate()


def claude(vm: VM, prompt: str, flags: str = "", timeout: int = 600) -> dict:
    """Run Claude Code headless in the MicroVM and return its JSON result."""
    out = vm.exec(f"cd /root && claude -p {shlex.quote(prompt)} --output-format json {flags} < /dev/null", timeout=timeout)
    lines = [l for l in out["stdout"].splitlines() if l.startswith("{")]
    if not lines:
        raise SystemExit(f"no JSON result from claude (rc={out['rc']}):\n{redact(out['stdout'][-1500:] + out['stderr'][-1500:])}")
    return json.loads(lines[-1])


def our_microvms() -> list[dict]:
    out, kw = [], {}
    while True:
        r = mvm.list_microvms(**kw)
        out += [m for m in r.get("items", []) if f":microvm-image:{PREFIX}-" in (m.get("imageArn") or "") and m.get("state") != "TERMINATED"]
        if not r.get("nextToken"):
            return out
        kw["nextToken"] = r["nextToken"]


# ---------- CloudWatch Logs ----------

def _filter(group: str, since_ms: int, pattern: str | None = None, until_ms: int | None = None) -> list:
    kw = {"logGroupName": group, "startTime": since_ms}
    if until_ms:
        kw["endTime"] = until_ms
    if pattern:
        kw["filterPattern"] = pattern
    events = []
    while True:
        r = logs.filter_log_events(**kw)
        events += r["events"]
        if not r.get("nextToken"):
            return events
        kw["nextToken"] = r["nextToken"]


def _settled(fetch, wait: float, min_count: int = 1) -> list:
    """Poll `fetch` until the count stops changing (log delivery lags by up to a minute or two)."""
    deadline, last, events = time.time() + wait, -1, []
    while True:
        events = fetch()
        if (len(events) >= min_count and len(events) == last) or time.time() > deadline:
            return events
        last = len(events)
        time.sleep(15)


def proxy_audit(since_ms: int, host_filter: str | None = None, wait: float = 120, until_ms: int | None = None) -> list[dict]:
    """iron-proxy's JSON audit lines from CloudWatch since `since_ms` (and before `until_ms`, if given)."""
    events = _settled(lambda: _filter(tf()["proxy_log_group"], since_ms, f'"{host_filter}"' if host_filter else None, until_ms), wait)
    out = []
    for e in sorted(events, key=lambda e: e["timestamp"]):
        try:
            out.append(json.loads(e["message"]))
        except ValueError:
            out.append({"raw": e["message"][:200]})
    return out


def audit_line(rec: dict) -> str:
    """One readable line per audit record: action, status, method, host+path, client address as the proxy
    sees it, what rejected it, and annotations from transforms that did something (e.g. secrets swapped)."""
    a = rec.get("audit") or rec
    fired = []
    for t in (rec.get("request_transforms") or []):
        ann = t.get("annotations") or {}
        if ann:
            fired.append(f"{t.get('name')}:{json.dumps(ann, separators=(',', ':'))[:140]}")
    return redact(f"{a.get('action', '?'):6} {a.get('status_code', '-')!s:>3} {a.get('method', ''):7} {a.get('host', '')}"
                  f"{a.get('path', '')}  from {a.get('remote_addr', '?')} {rec.get('rejected_by') or ''} {' '.join(fired)}".strip())


def is_request(rec: dict) -> bool:
    return bool((rec.get("audit") or {}).get("host"))


def is_tunnel(rec: dict) -> bool:
    """Did this request arrive through an explicit-mode CONNECT tunnel (or is it the CONNECT itself)?"""
    return (rec.get("audit") or {}).get("method") == "CONNECT" or bool(rec.get("tunnel"))


def query_log(since_ms: int, needle: str | None = None, wait: float = 150, min_count: int = 1,
              until_ms: int | None = None) -> list[dict]:
    """The sandbox VPC's Resolver query log since `since_ms` (only queries the Resolver didn't answer from cache)."""
    group = tf()["query_log_group"]
    events = _settled(lambda: _filter(group, since_ms, f'"{needle}"' if needle else None, until_ms), wait, min_count)
    out = []
    for e in sorted(events, key=lambda e: e["timestamp"]):
        try:
            out.append(json.loads(e["message"]))
        except ValueError:
            pass
    return out


def qlog_line(q: dict) -> str:
    ans = ",".join(f"{a.get('Type')}:{a.get('Rdata')}" for a in (q.get("answers") or []))
    return (f"{q.get('query_type', '?'):5} {q.get('query_name', '?'):40} {q.get('rcode', '?'):8} "
            f"firewall={q.get('firewall_rule_action', '-')} answers=[{ans}] via={q.get('transport', '?')}")


# ---------- infra ----------

def target_health() -> dict:
    lb = elb.describe_load_balancers(Names=[tf()["nlb_name"]])["LoadBalancers"][0]
    out = {}
    for g in elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]:
        h = elb.describe_target_health(TargetGroupArn=g["TargetGroupArn"])["TargetHealthDescriptions"]
        out[f"{g['Protocol']}:{g['Port']}"] = [(x["Target"]["Id"], x["TargetHealth"]["State"]) for x in h]
    return out


def proxy_task_ips() -> list[str]:
    t = tf()
    arns = ecs.list_tasks(cluster=t["ecs_cluster"], serviceName=t["ecs_service"])["taskArns"]
    if not arns:
        return []
    ips = []
    for task in ecs.describe_tasks(cluster=t["ecs_cluster"], tasks=arns)["tasks"]:
        for att in task.get("attachments", []):
            for d in att.get("details", []):
                if d["name"] == "privateIPv4Address":
                    ips.append(d["value"])
    return ips


def wait_proxy_ready(t0: float, timeout: float = 900) -> float:
    """Wait until the iron-proxy service has exactly one completed deployment and every target group has a
    healthy target. ECS's services-stable waiter can return while the old task is still serving."""
    t = tf(refresh=True)
    while time.perf_counter() - t0 < timeout:
        svc = ecs.describe_services(cluster=t["ecs_cluster"], services=[t["ecs_service"]])["services"][0]
        deps = svc["deployments"]
        if len(deps) == 1 and deps[0].get("rolloutState") == "COMPLETED" and svc["runningCount"] == 1:
            th = target_health()
            if th and all(any(s == "healthy" for _, s in v) and all(s in ("healthy", "draining") for _, s in v) for v in th.values()):
                return time.perf_counter() - t0
        time.sleep(10)
    raise TimeoutError("iron-proxy rollout did not complete")


def redeploy_proxy() -> float:
    """Force a new iron-proxy task (it re-reads config and secrets) and wait until it alone is serving."""
    t0 = time.perf_counter()
    ecs.update_service(cluster=tf()["ecs_cluster"], service=tf()["ecs_service"], forceNewDeployment=True)
    time.sleep(15)
    return wait_proxy_ready(t0)


def terraform(*args: str, log: Path | None = None) -> float:
    """Run terraform in infra/ (output to `log`, or discarded) and return how long it took."""
    t0 = time.perf_counter()
    with open(log or os.devnull, "a") as f:
        subprocess.run(["terraform", f"-chdir={HERE / 'infra'}", *args, "-input=false", "-no-color"],
                       check=True, stdout=f, stderr=subprocess.STDOUT)
    return time.perf_counter() - t0


def apply_infra(wait_proxy: bool = True, **tfvars) -> float:
    """terraform apply with the given variables (github_repo defaults to GITHUB_REPO from .env); then, by
    default, wait until the new iron-proxy task is the only one serving."""
    t0 = time.perf_counter()
    tfvars.setdefault("github_repo", REPO)
    args = [f"-var={k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in tfvars.items()]
    (HERE / "infra" / "logs").mkdir(exist_ok=True)  # unredacted Terraform output, gitignored
    terraform("apply", "-auto-approve", *args, log=HERE / "infra" / "logs" / "apply.log")
    tf(refresh=True)
    return wait_proxy_ready(t0) if wait_proxy else time.perf_counter() - t0


def firewall_association(assoc_id: str | None = None) -> dict | None:
    """The sandbox VPC's DNS Firewall association (Status, MutationProtection...), or None if it's gone."""
    try:
        return r53r.get_firewall_rule_group_association(
            FirewallRuleGroupAssociationId=assoc_id or tf()["firewall_association_id"])["FirewallRuleGroupAssociation"]
    except r53r.exceptions.ResourceNotFoundException:
        return None


# ---------- DNS inside the MicroVM ----------

def digs(vm: VM, queries: list[str], timeout: int = 120) -> dict[str, dict]:
    """Run several `dig` invocations in one exec. Each query is the argument string for dig; returns
    {query: {"status", "answers" [(name, type, rdata)], "server", "ms", "raw"}}."""
    script = "\n".join(f"echo '@@@ {q}'; dig +time=3 +tries=1 {q} 2>&1" for q in queries)
    return parse_digs(vm.sh(script, timeout=timeout))


def parse_digs(out: str) -> dict[str, dict]:
    res = {}
    for chunk in out.split("@@@ ")[1:]:
        q, _, body = chunk.partition("\n")
        status = re.search(r"status: ([A-Z]+)", body)
        server = re.search(r";; SERVER: (\S+)", body)
        ms = re.search(r";; Query time: (\d+) msec", body)
        answers = []
        if ";; ANSWER SECTION:" in body:
            sect = body.split(";; ANSWER SECTION:", 1)[1].split("\n\n", 1)[0]
            for line in sect.strip().splitlines():
                parts = line.split(None, 4)
                if len(parts) == 5:
                    answers.append((parts[0], parts[3], parts[4]))
        if status:
            st = status.group(1)
        elif "timed out" in body or "no servers could be reached" in body:
            st = "TIMEOUT"
        else:
            st = (body.strip().splitlines() or ["?"])[-1][:60]
        res[q.strip()] = {"status": st, "answers": answers, "server": server.group(1) if server else None,
                          "ms": int(ms.group(1)) if ms else None, "raw": body.strip()}
    return res


def fmt_answers(answers: list) -> str:
    return ", ".join(f"{t} {r}" for _, t, r in answers) or "(no answer)"


def laptop(cmd: str, timeout: int = 30) -> str:
    """Run a command on this machine (for comparisons with what the MicroVM sees)."""
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return redact((p.stdout + p.stderr).strip())


def real_ip(name: str) -> str | None:
    """A name's real IPv4 address, looked up from this machine through 1.1.1.1 (the MicroVM can't: every
    name it resolves is the proxy)."""
    out = laptop(f"dig +short +time=3 +tries=1 @1.1.1.1 {shlex.quote(name)} A")
    ips = [l for l in out.splitlines() if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", l.strip())]
    return ips[0] if ips else None
