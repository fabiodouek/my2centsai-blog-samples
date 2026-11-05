"""Shared helpers: Terraform outputs, boto3 clients, image builds, a run() with safety timers,
a VM wrapper around the in-VM agent, timing, and redaction of account-specific values."""

import io
import json
import os
import random
import subprocess
import time
import zipfile
from contextlib import contextmanager
from functools import cache
from pathlib import Path

import boto3
import httpx
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent
PREFIX = "my2cents-lc"
REGION = os.environ.get("AWS_REGION", "us-east-1")
MANAGED = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:"
HTTP_INGRESS, SHELL_INGRESS, NO_INGRESS = MANAGED + "HTTP_INGRESS", MANAGED + "SHELL_INGRESS", MANAGED + "NO_INGRESS"
INTERNET_EGRESS = MANAGED + "INTERNET_EGRESS"
BASE_IMAGE = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"

# Safety nets for anything a crashed script leaves behind: suspend after 10 idle minutes,
# terminate after 10 suspended minutes, and never live longer than an hour.
SAFETY_IDLE = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 600, "suspendedDurationSeconds": 600}
SAFETY_MAX_DURATION = 3600

HOOKS = {
    "port": 9001,
    "microvmHooks": {
        "run": "ENABLED", "runTimeoutInSeconds": 30,
        "resume": "ENABLED", "resumeTimeoutInSeconds": 30,
        "suspend": "ENABLED", "suspendTimeoutInSeconds": 30,
        "terminate": "ENABLED", "terminateTimeoutInSeconds": 30,
    },
    "microvmImageHooks": {
        "ready": "ENABLED", "readyTimeoutInSeconds": 300,
        "validate": "ENABLED", "validateTimeoutInSeconds": 300,
    },
}

_cfg = Config(region_name=REGION, retries={"max_attempts": 1, "mode": "standard"})
mvm = boto3.client("lambda-microvms", config=_cfg)
logs = boto3.client("logs", config=Config(region_name=REGION))
s3 = boto3.client("s3", config=Config(region_name=REGION))


# ---------- Terraform outputs and redaction ----------

@cache
def tf() -> dict:
    out = subprocess.run(["terraform", f"-chdir={HERE / 'infra'}", "output", "-json"],
                         capture_output=True, text=True, check=True).stdout
    return {k: v["value"] for k, v in json.loads(out).items()}


@cache
def account_id() -> str:
    return boto3.client("sts", config=Config(region_name=REGION)).get_caller_identity()["Account"]


def redact(text) -> str:
    """Hide the account ID and bucket name in anything printed (set REDACT=0 to show them)."""
    s = text if isinstance(text, str) else json.dumps(text, indent=2, default=str)
    if os.environ.get("REDACT", "1") == "0":
        return s
    s = s.replace(account_id(), "<acct>")
    try:
        s = s.replace(tf()["bucket"], "<bucket>")
    except Exception:
        pass
    return s


def show(obj, keys=None) -> None:
    if keys:
        obj = {k: obj.get(k) for k in keys}
    obj = {k: v for k, v in obj.items() if k != "ResponseMetadata"} if isinstance(obj, dict) else obj
    print(redact(obj))


# ---------- output helpers ----------

def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


@contextmanager
def timed(label: str, results: dict | None = None):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    if results is not None:
        results[label] = elapsed
    print(f"[{elapsed:6.2f}s] {label}")


def err(e: Exception) -> str:
    code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
    return redact(f"{code or type(e).__name__}: {str(e)[:220]}")


# ---------- images ----------

def image_name(short: str) -> str:
    return f"{PREFIX}-{short}"


def image_arn(short: str) -> str:
    return f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{image_name(short)}"


def _zip(short: str) -> bytes:
    """Zip images/<short>/ (Dockerfile at the root). Every image gets images/sandbox/agent.py."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((HERE / "images" / short).iterdir()):
            z.write(f, f.name)
        if short != "sandbox":
            z.write(HERE / "images" / "sandbox" / "agent.py", "agent.py")
    return buf.getvalue()


def image_exists(short: str) -> bool:
    try:
        mvm.get_microvm_image(imageIdentifier=image_arn(short))
        return True
    except mvm.exceptions.ResourceNotFoundException:
        return False


def build_image(short: str, memory_mib: int = 2048, hooks: dict | None = HOOKS, wait: bool = True, **extra) -> dict:
    """Create the image (or a new version of it) from images/<short>/ and wait for the build.
    Returns timings and the final image description."""
    key = f"{image_name(short)}-{int(time.time())}.zip"
    s3.put_object(Bucket=tf()["bucket"], Key=key, Body=_zip(short))
    params = {
        "codeArtifact": {"uri": f"s3://{tf()['bucket']}/{key}"},
        "baseImageArn": BASE_IMAGE,
        "buildRoleArn": tf()["build_role_arn"],
        "resources": [{"minimumMemoryInMiB": memory_mib}],
        **({"hooks": hooks} if hooks else {}),
        **extra,
    }
    t0 = time.perf_counter()
    if image_exists(short):
        r = mvm.update_microvm_image(imageIdentifier=image_arn(short), **params)
        action = "update"
    else:
        r = mvm.create_microvm_image(name=image_name(short), tags={"demo": "my2cents-lifecycle"}, **params)
        action = "create"
    out = {"action": action, "submitted": r}
    if not wait:
        return out
    states = []
    while True:
        d = mvm.get_microvm_image(imageIdentifier=image_arn(short))
        if not states or states[-1][0] != d["state"]:
            states.append((d["state"], round(time.perf_counter() - t0, 1)))
        if d["state"] not in ("CREATING", "UPDATING"):
            break
        time.sleep(5)
    out.update(image=d, states=states, seconds=round(time.perf_counter() - t0, 1))
    return out


# ---------- running MicroVMs ----------

THROTTLES = {"run": 0}


def run(image: str = "sandbox", test: str = "adhoc", *, ingress=(HTTP_INGRESS,), egress=(INTERNET_EGRESS,),
        role: str | None = "exec_logs", idle: dict | None = SAFETY_IDLE,
        max_duration: int | None = SAFETY_MAX_DURATION, payload: str | None = None, version: str | None = None,
        raw: dict | None = None) -> "VM":
    """run_microvm with this repo's defaults. MicroVMs can't be tagged, so the test name travels in
    the run hook payload. Retries ThrottlingException (new accounts get 1 RunMicrovm per second)."""
    if raw is not None:
        params = raw
    else:
        params = {"imageIdentifier": image_arn(image)}
        if version:
            params["imageVersion"] = version
        if ingress is not None:
            params["ingressNetworkConnectors"] = list(ingress)
        if egress is not None:
            params["egressNetworkConnectors"] = list(egress)
        if role:
            params["executionRoleArn"] = tf()[f"{role}_role_arn"]
        if idle is not None:
            params["idlePolicy"] = idle
        if max_duration is not None:
            params["maximumDurationInSeconds"] = max_duration
        params["runHookPayload"] = payload if payload is not None else json.dumps({"demo": "my2cents-lifecycle", "test": test})
    for attempt in range(8):
        t0 = time.perf_counter()
        try:
            r = mvm.run_microvm(**params)
            return VM(r["microvmId"], r["endpoint"], t0, r)
        except mvm.exceptions.ThrottlingException:
            THROTTLES["run"] += 1
            time.sleep(min(8, 0.5 * 2**attempt) + random.random() * 0.3)
    raise RuntimeError("RunMicrovm still throttled after 8 attempts")


class VM:
    """A running MicroVM plus a client for the agent inside it."""

    def __init__(self, vm_id: str, endpoint: str, t0: float, run_response: dict | None = None):
        self.id, self.endpoint, self.t0 = vm_id, endpoint, t0
        self.run_response = run_response or {}
        self._tokens: dict = {}
        self.http = httpx.Client(timeout=httpx.Timeout(180, connect=15))

    @classmethod
    def attach(cls, vm_id: str) -> "VM":
        d = mvm.get_microvm(microvmIdentifier=vm_id)
        return cls(vm_id, d["endpoint"], time.perf_counter(), d)

    def describe(self) -> dict:
        return mvm.get_microvm(microvmIdentifier=self.id)

    def state(self) -> str:
        return self.describe()["state"]

    def wait(self, *want: str, timeout: float = 300, poll: float = 0.25) -> float:
        """Poll get_microvm until the state is one of `want`; return seconds waited."""
        start = time.perf_counter()
        while (s := self.state()) not in want:
            if s == "TERMINATED" and "TERMINATED" not in want:
                d = self.describe()
                raise RuntimeError(f"{self.id} TERMINATED: {d.get('terminationMessage') or d.get('stateReason')}")
            if time.perf_counter() - start > timeout:
                raise TimeoutError(f"{self.id} still {s} after {timeout}s")
            time.sleep(poll)
        return time.perf_counter() - start

    def token(self, port: int = 8080, minutes: int = 60) -> str:
        cached = self._tokens.get(port)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        r = mvm.create_microvm_auth_token(microvmIdentifier=self.id, expirationInMinutes=minutes,
                                          allowedPorts=[{"port": port}])
        tok = r["authToken"]["X-aws-proxy-auth"]
        self._tokens[port] = (tok, time.time() + minutes * 60)
        return tok

    def request(self, method: str, path: str, port: int = 8080, **kw) -> httpx.Response:
        headers = {"X-aws-proxy-auth": self.token(port), "X-aws-proxy-port": str(port), **kw.pop("headers", {})}
        return self.http.request(method, f"https://{self.endpoint}{path}", headers=headers, **kw)

    def ready(self, timeout: float = 60) -> float:
        """Wait for the agent to answer through the endpoint; return seconds since run()."""
        start = time.perf_counter()
        while True:
            try:
                if self.request("GET", "/health", timeout=10).status_code == 200:
                    return time.perf_counter() - self.t0
            except httpx.HTTPError:
                pass
            if time.perf_counter() - start > timeout:
                raise TimeoutError(f"{self.id} agent not answering after {timeout}s")
            time.sleep(0.25)

    def exec(self, cmd: str, timeout: int = 120, **kw) -> dict:
        r = self.request("POST", "/exec", json={"cmd": cmd, "timeout": timeout, **kw}, timeout=timeout + 30)
        r.raise_for_status()
        return r.json()

    def sh(self, cmd: str, timeout: int = 120) -> str:
        r = self.exec(cmd, timeout)
        return (r["stdout"] + r["stderr"]).strip()

    def stream(self, cmd: str):
        """Yield output lines from POST /exec/stream (server-sent events)."""
        headers = {"X-aws-proxy-auth": self.token(8080), "X-aws-proxy-port": "8080"}
        with self.http.stream("POST", f"https://{self.endpoint}/exec/stream", json={"cmd": cmd},
                              headers=headers, timeout=None) as r:
            event = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    if event == "exit":
                        yield ("exit", int(line[6:]))
                        return
                    yield ("line", json.loads(line[6:]))

    def put_file(self, path: str, data: bytes) -> dict:
        return self.request("PUT", f"/files?path={path}", content=data).json()

    def get_file(self, path: str) -> bytes:
        r = self.request("GET", f"/files?path={path}")
        r.raise_for_status()
        return r.content

    def agent_state(self) -> dict:
        return self.request("GET", "/state").json()

    def events(self) -> list:
        return self.request("GET", "/events").json()["events"]

    def terminate(self) -> None:
        try:
            mvm.terminate_microvm(microvmIdentifier=self.id)
        except Exception as e:
            print(f"terminate {self.id}: {err(e)}")


def our_microvms(include_terminated: bool = False) -> list[dict]:
    """MicroVMs started from this repo's images (MicroVMs have no tags, so filter by image ARN)."""
    out, kw = [], {}
    while True:
        r = mvm.list_microvms(**kw)
        for m in r.get("items", []):
            if f":microvm-image:{PREFIX}-" in (m.get("imageArn") or "") and (include_terminated or m.get("state") != "TERMINATED"):
                out.append(m)
        if not r.get("nextToken"):
            return out
        kw["nextToken"] = r["nextToken"]


def runtime_logs(image_short: str, vm_id: str, since_ms: int | None = None) -> list[str]:
    """Agent stdout for one MicroVM from CloudWatch (needs an execution role with logs permissions)."""
    group = f"/aws/lambda-microvms/{image_name(image_short)}"
    try:
        streams = logs.describe_log_streams(logGroupName=group, orderBy="LastEventTime", descending=True, limit=50)
    except logs.exceptions.ResourceNotFoundException:
        return [f"(no log group {group})"]
    lines = []
    for st in streams["logStreams"]:
        if vm_id in st["logStreamName"]:
            kw = {"logGroupName": group, "logStreamName": st["logStreamName"], "startFromHead": True}
            if since_ms:
                kw["startTime"] = since_ms
            lines += [e["message"].rstrip() for e in logs.get_log_events(**kw)["events"]]
    return lines or [f"(no stream containing {vm_id} in {group}; streams: {[s['logStreamName'] for s in streams['logStreams'][:5]]})"]
