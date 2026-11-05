"""Lambda MicroVM control plane (boto3 `lambda-microvms`), the run-hook payload, endpoint/shell tokens.

Control-plane calls (get/list/suspend/...) don't count as endpoint traffic, so they never keep a MicroVM
awake. Only the agent HTTP calls and the shell WebSocket do."""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import Settings

log = logging.getLogger("microvm")

# Nothing secret goes here: placeholders only. The SDKs need *some* AWS key to sign with; iron-proxy's
# aws_auth transform throws the signature away and re-signs.
MAX_PAYLOAD = 4096
LIVE_STATES = ("PENDING", "RUNNING", "SUSPENDING", "SUSPENDED")
GONE_STATES = ("TERMINATING", "TERMINATED")


class QuotaExceeded(Exception):
    """AWS refused with ServiceQuotaExceededException; str() is AWS's own message."""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def code(e: Exception) -> str | None:
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else None


def iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(v)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The image trusts part 2's CA in its system bundle and sets these too; they're repeated in the run hook payload so
# every process that reads /etc/sandbox.env gets them, whatever its environment.
CA_ENV = {"NODE_EXTRA_CA_CERTS": "/usr/local/share/ca-certificates/iron-proxy-demo.crt",
          "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
          "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
          "AWS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt"}
AWS_PLACEHOLDER_KEYS = {"AWS_ACCESS_KEY_ID": "AKIAPLACEHOLDER00000", "AWS_SECRET_ACCESS_KEY": "placeholder-secret-not-real"}
# The AWS CLI and SDKs send uploads (PutObject, UploadPart) as aws-chunked bodies with a trailing checksum by default;
# iron-proxy 0.55.0's aws_auth refuses chunked bodies (chunked_body_not_allowed) and S3 answers 400. With checksums only
# when an operation requires them, the body goes as one signed payload, which iron-proxy re-signs.
AWS_CLIENT_ENV = {"AWS_REQUEST_CHECKSUM_CALCULATION": "when_required"}
BUILTIN_ENV = {"anthropic": "ANTHROPIC_API_KEY", "github": "GH_TOKEN", "demo": "DEMO_TOKEN"}


def build_run_payload(placeholders: dict, region: str, extra_env: dict | None = None) -> dict:
    """The run-hook payload: {"env": {...}, "nameserver": null}. Transparent mode: no proxy settings at all.
    Every name the sandbox looks up resolves to iron-proxy's load balancer (DNS Firewall in part 2's sandbox VPC),
    so the env only carries placeholders, the AWS placeholder keys, the CA variables and labels."""
    env: dict[str, str] = {}
    for name, var in BUILTIN_ENV.items():
        if placeholders.get(name):
            env[var] = str(placeholders[name])
    env.update(AWS_PLACEHOLDER_KEYS)
    env.update(AWS_REGION=region, AWS_DEFAULT_REGION=region, AWS_EC2_METADATA_DISABLED="true")
    env.update(AWS_CLIENT_ENV)
    env.update(CA_ENV)
    env.update(extra_env or {})
    if env.get("SANDBOX_NAME") and "AWS_SDK_UA_APP_ID" not in env:
        # AWS CLIs and SDKs append "app/<id>" to their User-Agent, iron-proxy passes it through when it re-signs,
        # and CloudTrail records it: the only per-sandbox label AWS sees behind a shared proxy.
        env["AWS_SDK_UA_APP_ID"] = ua_app_id(env["SANDBOX_NAME"])
    return {"env": env, "nameserver": None}


def ua_app_id(name: str) -> str:
    """`ws-<sandbox name>` as an SDK user-agent app ID: at most 50 characters, letters, digits, `.`, `_`, `-`.
    A label for CloudTrail, not an identity: anything in the sandbox can change its own User-Agent."""
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return ("ws-" + (out or "sandbox"))[:50]


def env_safe_name(name: str, limit: int = 64) -> str:
    """The sandbox name as it goes into SANDBOX_NAME (/etc/sandbox.env, the tmux status bar): only
    characters that mean nothing to a shell or to tmux formats (no quotes, $, `, #, backslash)."""
    out = re.sub(r"[^A-Za-z0-9 ._:@+-]", "-", name).strip()
    return out[:limit] or "sandbox"


def encode_payload(payload: dict) -> str:
    s = json.dumps(payload, separators=(",", ":"))
    if len(s.encode()) > MAX_PAYLOAD:
        raise ValueError(f"run hook payload is {len(s.encode())} bytes (max {MAX_PAYLOAD})")
    return s


class NoEgressConnector(ValueError):
    pass


def egress_connectors(s: Settings) -> list[str]:
    """The sandbox's egress: part 2's connector, always. An empty (or missing) egressNetworkConnectors isn't
    "offline": Lambda attaches its managed INTERNET_EGRESS connector instead, so the sandbox would reach the internet
    directly, past iron-proxy, its allowlist and its audit log. Refuse rather than send one."""
    conn = (s.connector_arn or "").strip()
    if not conn:
        raise NoEgressConnector("CONNECTOR_ARN isn't set: refusing to run a MicroVM without part 2's egress connector "
                                "(Lambda would attach INTERNET_EGRESS)")
    if conn.endswith(":INTERNET_EGRESS"):
        raise NoEgressConnector("CONNECTOR_ARN is Lambda's managed INTERNET_EGRESS connector, not part 2's")
    return [conn]


def run_params(s: Settings, *, idle_minutes: int, max_duration_s: int, client_token: str, payload: str,
               image_arn: str | None = None, suspended_duration_s: int | None = None, auto_resume: bool = True) -> dict:
    """RunMicrovm's parameters. Egress is always part 2's connector (egress_connectors refuses anything else)."""
    return {
        "imageIdentifier": image_arn or s.image_arn,
        "ingressNetworkConnectors": [s.http_ingress, s.shell_ingress],
        "egressNetworkConnectors": egress_connectors(s),
        # Suspended time counts toward maximumDuration, so letting it stay suspended "until max" (the default) is the
        # same as never terminating early on suspension. 0 terminates it as soon as it suspends.
        "idlePolicy": {"autoResumeEnabled": bool(auto_resume), "maxIdleDurationSeconds": idle_minutes * 60,
                       "suspendedDurationSeconds": max_duration_s if suspended_duration_s is None else suspended_duration_s},
        "maximumDurationInSeconds": max_duration_s,
        "runHookPayload": payload,
        "clientToken": client_token,
    }


class MicroVMs:
    """Thin, thread-safe wrapper. Call from async code through asyncio.to_thread."""

    def __init__(self, s: Settings, client=None):
        self.s = s
        self.c = client or boto3.client("lambda-microvms", config=Config(
            region_name=s.region, retries={"max_attempts": 3, "mode": "standard"}, max_pool_connections=50))
        self._cache: dict[str, tuple[float, dict]] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lists: dict[str, tuple[float, list]] = {}
        self._images: dict[str, tuple[float, tuple[bool, str | None]]] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()

    # ---- state ----
    def get(self, vm_id: str, max_age: float = 2.0) -> dict:
        """get_microvm with a short cache; a vanished MicroVM reads as TERMINATED."""
        with self._lock:
            hit = self._cache.get(vm_id)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
        try:
            d = self.c.get_microvm(microvmIdentifier=vm_id)
            d.pop("ResponseMetadata", None)
        except ClientError as e:
            if code(e) != "ResourceNotFoundException":
                raise
            d = {"microvmId": vm_id, "state": "TERMINATED", "stateReason": "not found"}
        with self._lock:
            self._cache[vm_id] = (time.monotonic(), d)
        return d

    def image_ready(self, arn: str, max_age: float = 300.0) -> tuple[bool, str | None]:
        """(usable, why not) for a size's image: it must exist with an active version. Cached 5 minutes."""
        with self._lock:
            hit = self._images.get(arn)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
        try:
            d = self.c.get_microvm_image(imageIdentifier=arn)
            ok = bool(d.get("latestActiveImageVersion")) and "FAIL" not in str(d.get("state"))
            res = (ok, None if ok else f"its image is {str(d.get('state')).lower().replace('_', ' ')}")
        except ClientError as e:
            if code(e) not in ("ResourceNotFoundException", "ValidationException"):
                log.warning("get_microvm_image: %s", e)
                return True, None   # don't hide a size on a transient error; RunMicrovm has the last word
            res = (False, "its image isn't built yet (scripts/build_sandbox_image.py)")
        with self._lock:
            self._images[arn] = (time.monotonic(), res)
        return res

    def forget(self, vm_id: str) -> None:
        with self._lock:
            self._cache.pop(vm_id, None)

    def list_cached(self, max_age: float = 10.0, **kw) -> list[dict]:
        """ListMicrovms is throttled at a low rate account-wide (other tools share it); the quota bar
        polls, so serve it from a 10 s cache."""
        key = json.dumps(kw, sort_keys=True)
        with self._lock:
            hit = self._lists.get(key)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
        items = self.list_all(**kw)
        with self._lock:
            self._lists[key] = (time.monotonic(), items)
        return items

    def list_all(self, **kw) -> list[dict]:
        items = []
        while True:
            r = self.c.list_microvms(**kw)
            items += r.get("items", [])
            if not r.get("nextToken"):
                return items
            kw["nextToken"] = r["nextToken"]

    # ---- lifecycle ----
    def run(self, *, idle_minutes: int, max_duration_s: int, payload: str, client_token: str | None = None,
            image_arn: str | None = None, suspended_duration_s: int | None = None, auto_resume: bool = True) -> dict:
        params = run_params(self.s, idle_minutes=idle_minutes, max_duration_s=max_duration_s,
                            client_token=client_token or str(uuid.uuid4()), payload=payload, image_arn=image_arn,
                            suspended_duration_s=suspended_duration_s, auto_resume=auto_resume)
        # RunMicrovm is rate limited (~1/s): one at a time per process, back off on throttling.
        with self._run_lock:
            for attempt in range(6):
                try:
                    r = self.c.run_microvm(**params)
                    r.pop("ResponseMetadata", None)
                    return r
                except ClientError as e:
                    c = code(e)
                    if c == "ServiceQuotaExceededException":
                        raise QuotaExceeded(e.response.get("Error", {}).get("Message") or str(e)) from e
                    if c != "ThrottlingException" or attempt == 5:
                        raise
                    time.sleep(min(8, 0.5 * 2 ** attempt) + random.random() * 0.3)
        raise RuntimeError("unreachable")

    def _act(self, fn, vm_id: str) -> None:
        try:
            fn(microvmIdentifier=vm_id)
        except ClientError as e:
            c = code(e)
            if c == "ResourceNotFoundException":
                raise NotFound(vm_id) from e
            if c in ("ConflictException", "ValidationException"):
                raise Conflict(e.response.get("Error", {}).get("Message") or c) from e
            if c == "ServiceQuotaExceededException":
                raise QuotaExceeded(e.response.get("Error", {}).get("Message") or str(e)) from e
            raise
        finally:
            self.forget(vm_id)

    def suspend(self, vm_id: str) -> None:
        self._act(self.c.suspend_microvm, vm_id)

    def resume(self, vm_id: str) -> None:
        self._act(self.c.resume_microvm, vm_id)

    def terminate(self, vm_id: str) -> None:
        self._act(self.c.terminate_microvm, vm_id)
        with self._lock:
            self._tokens.pop(vm_id, None)

    def wait(self, vm_id: str, *want: str, timeout: float = 30, poll: float = 0.25) -> str:
        end = time.monotonic() + timeout
        while True:
            st = self.get(vm_id, max_age=0)["state"]
            if st in want or st == "TERMINATED" or time.monotonic() > end:
                return st
            time.sleep(poll)

    # ---- endpoint auth ----
    def endpoint_token(self, vm_id: str) -> str:
        """Token for port 8080 only (the hooks on 9001 must never be reachable). Cached ~55 minutes."""
        with self._lock:
            t = self._tokens.get(vm_id)
            if t and t[1] > time.time() + 300:
                return t[0]
        r = self.c.create_microvm_auth_token(microvmIdentifier=vm_id, expirationInMinutes=60, allowedPorts=[{"port": 8080}])
        tok = r["authToken"]["X-aws-proxy-auth"]
        with self._lock:
            self._tokens[vm_id] = (tok, time.time() + 3600)
        return tok

    def shell_token(self, vm_id: str) -> str:
        r = self.c.create_microvm_shell_auth_token(microvmIdentifier=vm_id, expirationInMinutes=5)
        return r["authToken"]["X-aws-proxy-auth"]
