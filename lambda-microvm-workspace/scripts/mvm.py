"""Shared helpers for the workspace scripts: part 2's and this stack's Terraform outputs (read fresh), the run-hook
payload, run_microvm with quota/throttle retries, a small wrapper around the in-VM agent, the SHELL_INGRESS
WebSocket, and account-ID redaction."""

import asyncio
import codecs
import json
import os
import random
import subprocess
import time
from functools import cache
from pathlib import Path

import boto3
import httpx
import websockets
from botocore.config import Config

ROOT = Path(__file__).resolve().parent.parent
PART2_INFRA = Path(os.environ.get("PART2_INFRA", Path.home() / "Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra"))
INFRA = ROOT / "infra"
REGION = os.environ.get("AWS_REGION", "us-east-1")
IMAGE_NAME = "my2cents-ws-sandbox"
# One image per sandbox size (a MicroVM's memory is set by its image: resources[].minimumMemoryInMiB; vCPU is
# memory / 2 GB). Medium keeps the original image name; the others add a suffix. Same code in every image.
SIZES = {  # size -> (MiB, image name)
    "small": (1024, IMAGE_NAME + "-1g"),
    "medium": (2048, IMAGE_NAME),
    "large": (4096, IMAGE_NAME + "-4g"),
    "xlarge": (8192, IMAGE_NAME + "-8g"),
}
DEFAULT_SIZE = "medium"
BASE_IMAGE = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
MANAGED = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:"
HTTP_INGRESS, SHELL_INGRESS = MANAGED + "HTTP_INGRESS", MANAGED + "SHELL_INGRESS"

# Placeholder AWS keys: the SDK needs something to sign with; iron-proxy's aws_auth re-signs.
AWS_PLACEHOLDER = {"AWS_ACCESS_KEY_ID": "AKIAPLACEHOLDER00000", "AWS_SECRET_ACCESS_KEY": "placeholder-secret-not-real",
                   "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION, "AWS_EC2_METADATA_DISABLED": "true",
                   # uploads as one signed payload: iron-proxy refuses the default aws-chunked bodies
                   "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required"}
# The image trusts the CA in its system bundle; the payload repeats the variables (same as the backend)
CA_ENV = {"NODE_EXTRA_CA_CERTS": "/usr/local/share/ca-certificates/iron-proxy-demo.crt",
          "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt", "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
          "AWS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt"}

mvm = boto3.client("lambda-microvms", config=Config(region_name=REGION, retries={"max_attempts": 1, "mode": "standard"}))
s3 = boto3.client("s3", config=Config(region_name=REGION))


def _outputs(chdir: Path) -> dict:
    out = subprocess.run(["terraform", f"-chdir={chdir}", "output", "-json"], capture_output=True, text=True, check=True).stdout
    return {k: v["value"] for k, v in json.loads(out).items()}


def part2() -> dict:
    """Part 2's Terraform outputs (the two-VPC stack: proxy VPC, CA, built-in secrets, its own test connector)."""
    t = _outputs(PART2_INFRA)
    if "proxy_vpc_id" not in t:
        raise RuntimeError(f"{PART2_INFRA} isn't the two-VPC part 2 (no proxy_vpc_id output); deploy that first")
    return t


def workspace() -> dict:
    """This stack's Terraform outputs (the URL, the Cognito pool, the tables...)."""
    return _outputs(INFRA)


@cache
def account_id() -> str:
    return boto3.client("sts", config=Config(region_name=REGION)).get_caller_identity()["Account"]


def redact(text) -> str:
    s = text if isinstance(text, str) else json.dumps(text, indent=2, default=str)
    return s.replace(account_id(), "<acct>")


def image_arn(size: str = DEFAULT_SIZE) -> str:
    return f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{SIZES[size][1]}"


def settings(t: dict | None = None, extra_env: dict | None = None) -> dict:
    """The run-hook payload: placeholders, the AWS placeholder keys and the CA variables. Nothing secret, and no proxy
    settings: the connector's VPC (part 2's sandbox VPC) answers every name with iron-proxy's NLB, so the
    sandbox reaches iron-proxy without knowing it (transparent interception). Same shape as the backend's."""
    t = t or part2()
    p = t["placeholders"]
    env = dict(ANTHROPIC_API_KEY=p["anthropic"], GH_TOKEN=p["github"], DEMO_TOKEN=p["demo"], **AWS_PLACEHOLDER, **CA_ENV)
    env.update(extra_env or {})  # e.g. SANDBOX_NAME, shown in tmux's status line
    return {"env": env, "nameserver": None}


def run_microvm(*, idle_s: int = 900, suspended_s: int = 1800, max_duration_s: int = 3600, extra_env: dict | None = None,
                log=print) -> tuple["VM", dict]:
    """Run one MicroVM from the workspace image with HTTP + SHELL ingress, on part 2's sandbox connector.
    Retries ServiceQuotaExceededException (8 GB account quota) every 60 s up to 10 times, and throttling."""
    t = part2()
    if not t.get("connector_arn"):
        # An empty egressNetworkConnectors isn't offline: Lambda attaches INTERNET_EGRESS (direct internet, no proxy)
        raise RuntimeError("part 2 has no connector_arn output; refusing to run a MicroVM without it")
    payload = settings(t, extra_env)
    params = {
        "imageIdentifier": image_arn(),
        "ingressNetworkConnectors": [HTTP_INGRESS, SHELL_INGRESS],
        "egressNetworkConnectors": [t["connector_arn"]],
        "idlePolicy": {"autoResumeEnabled": True, "maxIdleDurationSeconds": idle_s, "suspendedDurationSeconds": suspended_s},
        "maximumDurationInSeconds": max_duration_s,
        "runHookPayload": json.dumps(payload),
    }
    quota_waits = throttles = 0
    while True:
        t0 = time.perf_counter()
        try:
            r = mvm.run_microvm(**params)
            return VM(r["microvmId"], r["endpoint"], t0), {"connector": "part 2",
                                                           "payload_bytes": len(params["runHookPayload"])}
        except mvm.exceptions.ServiceQuotaExceededException:
            quota_waits += 1
            if quota_waits > 10:
                raise
            log(f"  quota full (8 GB of MicroVM memory in use); waiting 60 s ({quota_waits}/10)")
            time.sleep(60)
        except mvm.exceptions.ThrottlingException:
            throttles += 1
            if throttles > 8:
                raise
            time.sleep(min(8, 0.5 * 2**throttles) + random.random() * 0.3)


class VM:
    def __init__(self, vm_id: str, endpoint: str, t0: float):
        self.id, self.endpoint, self.t0 = vm_id, endpoint, t0
        self._tok = None
        self.http = httpx.Client(timeout=httpx.Timeout(300, connect=15))

    def describe(self) -> dict:
        return mvm.get_microvm(microvmIdentifier=self.id)

    def state(self) -> str:
        return self.describe()["state"]

    def wait(self, *want: str, timeout: float = 300, poll: float = 0.1) -> float:
        start = time.perf_counter()
        while (s := self.state()) not in want:
            if s == "TERMINATED":
                raise RuntimeError(f"{self.id} TERMINATED: {self.describe().get('stateReason')}")
            if time.perf_counter() - start > timeout:
                raise TimeoutError(f"{self.id} still {s}")
            time.sleep(poll)
        return time.perf_counter() - start

    def token(self) -> str:
        # Only ever port 8080: the hooks on 9001 must not be reachable through the endpoint
        if not self._tok or self._tok[1] < time.time() + 60:
            r = mvm.create_microvm_auth_token(microvmIdentifier=self.id, expirationInMinutes=60, allowedPorts=[{"port": 8080}])
            self._tok = (r["authToken"]["X-aws-proxy-auth"], time.time() + 3600)
        return self._tok[0]

    def shell_token(self, minutes: int = 30) -> str:
        return mvm.create_microvm_shell_auth_token(microvmIdentifier=self.id, expirationInMinutes=minutes)["authToken"]["X-aws-proxy-auth"]

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, f"https://{self.endpoint}{path}", headers={"X-aws-proxy-auth": self.token()}, **kw)

    def get(self, path: str, **kw) -> dict:
        r = self.request("GET", path, **kw)
        r.raise_for_status()
        return r.json()

    def ready(self, timeout: float = 60) -> float:
        start = time.perf_counter()
        while time.perf_counter() - start < timeout:
            try:
                if self.request("GET", "/health", timeout=10).status_code == 200:
                    return time.perf_counter() - start
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"{self.id} agent not answering")

    def exec(self, cmd: str, timeout: int = 120) -> dict:
        r = self.request("POST", "/exec", json={"cmd": cmd, "timeout": timeout}, timeout=timeout + 30)
        r.raise_for_status()
        return r.json()

    def sh(self, cmd: str, timeout: int = 120) -> str:
        r = self.exec(cmd, timeout)
        return (r["stdout"] + r["stderr"]).rstrip()

    def screen(self, target: str = "main") -> str:
        """What tmux shows in the session's active pane, as plain text."""
        return self.sh(f"tmux capture-pane -p -t {target}")

    def terminate(self) -> None:
        mvm.terminate_microvm(microvmIdentifier=self.id)


class Shell:
    """The SHELL_INGRESS WebSocket: wss://<endpoint>/shell with header X-aws-proxy-auth. The server's first
    frame is text JSON session_init; resize goes up as text JSON, keystrokes as binary frames; output comes
    back as binary (sometimes text)."""

    def __init__(self, vm: VM, token: str | None = None):
        self.vm, self.token = vm, token
        self.ws, self.session_init, self.out = None, None, []
        # Output frames can split a multi-byte UTF-8 character; decode the stream, not each frame
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")

    async def connect(self, rows: int = 45, cols: int = 160, open_timeout: float = 30) -> float:
        t0 = time.perf_counter()
        token = self.token or self.vm.shell_token()
        self.ws = await websockets.connect(f"wss://{self.vm.endpoint}/shell", additional_headers={"X-aws-proxy-auth": token},
                                           open_timeout=open_timeout, max_size=None, ping_interval=None)
        first = await asyncio.wait_for(self.ws.recv(), 15)
        self.session_init = json.loads(first) if isinstance(first, str) else {"binary": first[:80]}
        await self.ws.send(json.dumps({"type": "resize", "rows": rows, "cols": cols}))
        self._reader = asyncio.create_task(self._read())
        return time.perf_counter() - t0

    async def _read(self):
        try:
            async for m in self.ws:
                self.out.append(self._utf8.decode(m) if isinstance(m, bytes) else m)
        except websockets.ConnectionClosed:
            pass

    async def ping(self, timeout: float = 5) -> float | None:
        """Round trip of a WebSocket ping, or None if no pong comes back (keepalive check)."""
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(await self.ws.ping(), timeout)
            return time.perf_counter() - t0
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            return None

    async def send(self, keys: str) -> None:
        await self.ws.send(keys.encode())

    def text(self) -> str:
        return "".join(self.out)

    async def wait_for(self, needle: str, timeout: float = 20) -> float:
        t0 = time.perf_counter()
        while needle not in self.text():
            if time.perf_counter() - t0 > timeout:
                raise TimeoutError(f"{needle!r} not seen on the shell after {timeout}s")
            await asyncio.sleep(0.1)
        return time.perf_counter() - t0

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            self._reader.cancel()
