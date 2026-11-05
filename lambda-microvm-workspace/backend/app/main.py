"""FastAPI app: REST for sandboxes/credentials/quota, /ws/terminal/{id} (shell bridge), /ws/events (proxy,
DNS and lifecycle feed). Run with `uvicorn app.main:create_app --factory`."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import anyio
import httpx
import websockets
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.websockets import WebSocketDisconnect, WebSocketState

from . import pricing
from . import sandbox_env
from . import sizes as sz
from .quota import MemoryQuota
from .auth import AuthError, CognitoVerifier, DevVerifier, User, bearer, http_jwks_fetcher
from .config import Settings, load
from .credentials import Credentials
from .events import Hub, LogTail, offer
from .microvm import (GONE_STATES, MAX_PAYLOAD, Conflict, MicroVMs, NoEgressConnector, NotFound, QuotaExceeded,
                      build_run_payload, egress_connectors, encode_payload, env_safe_name, iso, now_iso)
from .policy import PolicyError, ProxyPolicy
from .store import DynamoStore, MemoryStore
from .terminal import TerminalBridge, UpstreamClosed

log = logging.getLogger("api")

TERMINATED_KEEP_S = 3600
AUTH_TIMEOUT_S = 5.0
LIFECYCLE_POLL_S = 5.0
HEARTBEAT_S = 25.0
FEED_SCOPE = ("iron-proxy sees every request from the NLB's address, so the feed covers all sandboxes on "
              "this proxy and can't be tied to one sandbox.")

# What runs in the terminal's tmux pane (session "main"), read through the agent's /exec so the sandbox image
# doesn't change. Line 1: tmux's pane_current_command. Line 2: the command line of the pane's foreground process.
# Prints nothing when no terminal has started tmux yet.
PANE_PROBE = r"""tmux display-message -p -t main '#{pane_current_command}' 2>/dev/null || exit 0
fg=$(ps -o tpgid= -p "$(tmux display-message -p -t main '#{pane_pid}')" | tr -d ' ')
[ -n "$fg" ] && [ "$fg" != -1 ] && tr '\0' ' ' < "/proc/$fg/cmdline"
exit 0"""
SHELLS = {"bash", "sh", "dash", "zsh", "fish"}


# ---------------------------------------------------------------- helpers

def pane_info(out: str) -> dict | None:
    """PANE_PROBE output -> {"command", "claude_running", "at_shell"}, or None without a tmux session.
    Claude Code shows up as `claude` (it sets its process title); as a fallback, `node .../claude`."""
    lines = out.splitlines()
    if not lines or not lines[0].strip():
        return None
    command = lines[0].strip().lstrip("-")
    argv = (lines[1] if len(lines) > 1 else "").split()
    base = [os.path.basename(a) for a in argv[:2]]
    claude = (command == "claude" or base[:1] == ["claude"]
              or (base[:1] in (["node"], ["nodejs"]) and len(argv) > 1 and (base[1] == "claude" or "claude-code" in argv[1])))
    return {"command": command, "claude_running": claude, "at_shell": command in SHELLS and not claude}


class OriginCheck:
    """Requests without `X-Origin-Verify: <ORIGIN_SECRET>` get 403 (HTTP) or a refused handshake (WebSocket),
    except /healthz for the ALB health check. Defence in depth behind the ALB rule that does the same."""

    def __init__(self, app, secret: str | None):
        self.app, self.secret = app, (secret.encode() if secret else None)

    async def __call__(self, scope, receive, send):
        if self.secret and scope["type"] in ("http", "websocket") and scope.get("path") != "/healthz":
            got = next((v for k, v in scope.get("headers", []) if k == b"x-origin-verify"), b"")
            if not hmac.compare_digest(got, self.secret):
                if scope["type"] == "http":
                    return await JSONResponse({"detail": "forbidden"}, status_code=403)(scope, receive, send)
                await receive()  # websocket.connect
                return await send({"type": "websocket.close", "code": 1008, "reason": "forbidden"})
        return await self.app(scope, receive, send)


class Activity:
    """Which sandboxes have a terminal attached in this process, and when one last saw use. The UI uses it
    for the idle countdown (idle only counts endpoint traffic, which an attached terminal produces)."""

    def __init__(self):
        self.attached: dict[str, int] = {}
        self.last: dict[str, float] = {}

    def attach(self, vm_id: str) -> None:
        self.attached[vm_id] = self.attached.get(vm_id, 0) + 1
        self.touch(vm_id)

    def detach(self, vm_id: str) -> None:
        self.attached[vm_id] = max(0, self.attached.get(vm_id, 0) - 1)
        self.touch(vm_id)

    def touch(self, vm_id: str) -> None:
        self.last[vm_id] = time.time()

    def info(self, vm_id: str) -> dict:
        t = self.last.get(vm_id)
        return {"attached": self.attached.get(vm_id, 0) > 0,
                "last_activity_at": iso(datetime.fromtimestamp(t, tz=timezone.utc)) if t else None}


class Agent:
    """HTTP to the in-VM agent on port 8080 (token minted for 8080 only). Each call is endpoint traffic and
    keeps the MicroVM awake (or wakes it), so callers only use it while a terminal is attached."""

    def __init__(self, mv: MicroVMs):
        self.mv = mv
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10))

    async def get(self, vm_id: str, endpoint: str, path: str) -> dict:
        return await self._call("GET", vm_id, endpoint, path)

    async def post(self, vm_id: str, endpoint: str, path: str, body: dict) -> dict:
        return await self._call("POST", vm_id, endpoint, path, body)

    async def _call(self, method: str, vm_id: str, endpoint: str, path: str, body: dict | None = None) -> dict:
        token = await asyncio.to_thread(self.mv.endpoint_token, vm_id)
        try:
            r = await self.http.request(method, f"https://{endpoint}{path}", headers={"X-aws-proxy-auth": token},
                                        json=body)
        except httpx.TimeoutException:
            raise HTTPException(504, f"sandbox agent did not answer {path} in time")
        except httpx.HTTPError as e:
            raise HTTPException(502, f"sandbox agent unreachable: {type(e).__name__}")
        if r.status_code == 404:
            raise HTTPException(502, f"sandbox agent has no {path} (older image?)")
        if r.status_code != 200:
            raise HTTPException(502, f"sandbox agent returned {r.status_code} for {path}")
        try:
            return r.json()
        except ValueError:
            raise HTTPException(502, f"sandbox agent returned non-JSON for {path}")

    async def aclose(self) -> None:
        await self.http.aclose()


class FastAPIBrowser:
    def __init__(self, ws: WebSocket):
        self.ws = ws

    async def recv(self):
        try:
            m = await self.ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if m["type"] == "websocket.disconnect":
            return None
        if m.get("bytes") is not None:
            return m["bytes"]
        return m.get("text")

    async def send_text(self, data: str) -> None:
        await self.ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.ws.application_state != WebSocketState.DISCONNECTED:
            await self.ws.close(code, reason)


class WsUpstream:
    def __init__(self, ws):
        self.ws = ws
        self._lock = asyncio.Lock()

    @staticmethod
    def _closed(e: websockets.ConnectionClosed) -> UpstreamClosed:
        rc = e.rcvd
        return UpstreamClosed(rc.code if rc else 1006, rc.reason if rc else "")

    async def recv(self):
        try:
            return await self.ws.recv()
        except websockets.ConnectionClosed as e:
            raise self._closed(e)

    async def send(self, data) -> None:
        async with self._lock:
            try:
                await self.ws.send(data)
            except websockets.ConnectionClosed as e:
                raise self._closed(e)

    async def close(self) -> None:
        await self.ws.close()


def shell_connector(endpoint: str, keepalive: float):
    async def connect(token: str) -> WsUpstream:
        ws = await websockets.connect(f"wss://{endpoint}/shell", additional_headers={"X-aws-proxy-auth": token},
                                      open_timeout=20, max_size=None, ping_interval=keepalive or None,
                                      ping_timeout=20 if keepalive else None, close_timeout=3)
        return WsUpstream(ws)
    return connect


class VmControl:
    def __init__(self, mv: MicroVMs, vm_id: str):
        self.mv, self.vm_id = mv, vm_id

    async def state(self, fresh: bool = False) -> str:
        return (await asyncio.to_thread(self.mv.get, self.vm_id, 0 if fresh else 2.0))["state"]

    async def resume(self) -> None:
        await asyncio.to_thread(self.mv.resume, self.vm_id)

    async def shell_token(self) -> str:
        return await asyncio.to_thread(self.mv.shell_token, self.vm_id)


# ---------------------------------------------------------------- request models

class CreateSandbox(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # small | medium | large | xlarge (app/sizes.py): one image per size
    size: str = Field(default=sz.DEFAULT_SIZE, max_length=16)
    # Idle policy: suspend after this many idle minutes; terminate after `suspended_minutes` suspended (None: only
    # when the lifetime ends; 0: as soon as it suspends); auto-resume on endpoint traffic (terminal, agent).
    # Measured: Lambda applies suspendedDurationSeconds to idle suspends only; after SuspendMicrovm (the Suspend
    # button) a sandbox stays suspended until its lifetime ends.
    idle_minutes: int = Field(default=15, ge=1, le=480)
    suspended_minutes: int | None = Field(default=None, ge=0, le=480)
    auto_resume: bool = True
    # Lifetime in minutes (1-480); max_hours is the older field, still accepted
    max_minutes: int | None = Field(default=None, ge=1, le=480)
    max_hours: float | None = Field(default=None, gt=0, le=8)
    # Extra variables for /etc/sandbox.env (app/sandbox_env.py): not for secrets
    env: dict[str, str] = Field(default_factory=dict, max_length=sandbox_env.MAX_VARS)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or not v.isprintable():
            raise ValueError("name must be printable text")
        return v


class HostBody(BaseModel):
    host: str = Field(max_length=300)


class NewCredential(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    name: str = Field(max_length=64)
    env: str = Field(max_length=80)
    hosts: list[str] = Field(min_length=1, max_length=10)
    headers: list[str] = Field(default_factory=list, max_length=10)
    query: bool = False
    body: bool = False
    value: SecretStr


class RotateBody(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    value: SecretStr


class ResetBody(BaseModel):
    confirm: str = Field(max_length=32)


class ScopeBody(BaseModel):
    role: str | None = Field(default=None, max_length=32)
    session_policy: bool = True
    actions: list[str] = Field(default_factory=list, max_length=200)


def payload_bytes(s: Settings, extra: dict) -> int:
    """Size of a new sandbox's run hook payload with these extra env vars, for the longest sandbox name."""
    p = build_run_payload(s.placeholders, s.region, {**extra, "SANDBOX_NAME": "x" * 64})
    return len(json.dumps(p, separators=(",", ":")).encode())


# ---------------------------------------------------------------- app

def create_app(settings: Settings | None = None, *, microvms: MicroVMs | None = None, store=None, verifier=None,
               credentials: Credentials | None = None, hub: Hub | None = None, agent: Agent | None = None,
               shell_connect=None, policy: ProxyPolicy | None = None, quota: MemoryQuota | None = None) -> FastAPI:
    s = settings or load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mv = microvms or MicroVMs(s)
    db = store or (DynamoStore(s.table_name, s.region) if s.table_name else MemoryStore())
    if verifier is None:
        verifier = DevVerifier() if s.dev_auth else CognitoVerifier(s.issuer, s.client_id, http_jwks_fetcher(s.jwks_url))
    policy = policy or ProxyPolicy(s, payload_size=lambda extra: payload_bytes(s, extra))
    creds = credentials or Credentials(s, catalog=policy.credential_catalog, policy=policy)
    hub = hub or Hub(lambda emit: LogTail(s, emit, names=creds.names))
    agent = agent or Agent(mv)
    mem_quota = quota or MemoryQuota(s)
    activity = Activity()
    bridges: dict[str, set[TerminalBridge]] = {}
    connect_for = shell_connect or (lambda endpoint: shell_connector(endpoint, s.terminal_keepalive_s))

    if s.dev_auth:
        log.warning("DEV_AUTH=1: every caller is the fake user %s; never use this in a deployment", "dev-user-0000")
    if not s.table_name:
        log.warning("TABLE_NAME unset: in-memory ownership store (lost on restart)")

    @asynccontextmanager
    async def lifespan(app):
        # Read the MicroVM memory quota once now (cached for an hour), so the first page doesn't wait on it
        threading.Thread(target=mem_quota.get, name="quota-warm", daemon=True).start()
        if policy.aws_catalog is not None:
            # Fetch the Service Authorization Reference and ReadOnlyAccess now, so the first visitor doesn't wait
            def warm():
                for attempt in range(10):   # the catalog fetch can fail at start (network, throttling): retry
                    try:
                        policy.aws_catalog.snapshot()
                        log.info("AWS catalog ready: %s", policy.aws_catalog.snapshot()["counts"])
                        # Part 2 seeds the scope with the default role but can't compute the matching lists
                        policy.seed_scope()
                        return
                    except Exception as e:
                        log.warning("AWS catalog warm-up (attempt %d): %s", attempt + 1, e)
                        time.sleep(30)
            threading.Thread(target=warm, name="catalog-warm", daemon=True).start()
        yield
        hub.shutdown()
        await agent.aclose()

    app = FastAPI(title="My2cents.ai Lambda MicroVM Playground API", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs" if s.dev_auth else None, redoc_url=None, openapi_url="/api/openapi.json" if s.dev_auth else None)
    app.state.settings, app.state.microvms, app.state.store, app.state.hub = s, mv, db, hub
    app.state.activity, app.state.agent, app.state.credentials = activity, agent, creds
    app.state.policy = policy
    if s.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["*"], allow_headers=["*"])
    # The catalog and search responses are tens of KB of JSON; CloudFront doesn't compress /api (CachingDisabled)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(OriginCheck, secret=s.origin_secret)

    # Validation errors never echo what was sent (a credential's value could be in it)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errs = [{"loc": list(e.get("loc") or []), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse({"detail": errs}, status_code=422)

    @app.exception_handler(PolicyError)
    async def policy_error(request: Request, exc: PolicyError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    # ---- auth ----
    async def current_user(request: Request) -> User:
        token = bearer(request.headers.get("authorization"))
        if s.dev_auth:
            return await verifier.verify(token)
        if not token:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        try:
            return await verifier.verify(token)
        except AuthError as e:
            raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"})

    async def ws_auth(ws: WebSocket) -> User | None:
        """First client message must be {"type":"auth","token":...} within 5 s, else close 4401."""
        try:
            raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("type") != "auth":
                raise AuthError("first message must be auth")
            return await verifier.verify(msg.get("token"))
        except WebSocketDisconnect:
            return None
        except (asyncio.TimeoutError, AuthError, ValueError, KeyError, RuntimeError) as e:
            reason = "auth timeout" if isinstance(e, asyncio.TimeoutError) else "unauthorized"
            try:
                await ws.close(4401, reason)
            except RuntimeError:
                pass
            return None

    async def admin_user(user: User = Depends(current_user)) -> User:
        """Writes to the proxy policy (global: every sandbox on the proxy) need the admins group."""
        if not user.in_group(s.admin_group):
            raise HTTPException(403, f"admins only: changing the proxy policy needs the {s.admin_group} group")
        return user

    async def owned(user: User, vm_id: str) -> dict:
        item = await asyncio.to_thread(db.get, user.sub, vm_id)
        if not item:
            raise HTTPException(404, "sandbox not found")
        return item

    # ---- views ----
    def size_of(item: dict, d: dict) -> str:
        """The sandbox's size: stored at create; for an older record, from the image it runs; else medium."""
        size = item.get("size")
        if size in sz.SIZES:
            return size
        return sz.size_of_arn(s.size_images, d.get("imageArn")) or sz.DEFAULT_SIZE

    def egress_of(d: dict) -> str | None:
        conns = d.get("egressNetworkConnectors")
        if conns is None:
            return None
        if s.connector_arn and s.connector_arn in conns:
            return "proxy"
        return "internet" if any(str(c).endswith(":INTERNET_EGRESS") for c in conns) else ("none" if not conns else "other")

    def end_reason(d: dict, expires) -> str | None:
        """Lambda reports "MicroVM exceeded maximum lifetime." for a sandbox its idle policy terminated after
        suspendedDurationSeconds too; say which it was when it ended well before its lifetime."""
        reason = d.get("stateReason")
        ended = d.get("terminatedAt")
        if (reason and "maximum lifetime" in reason and isinstance(ended, datetime) and expires is not None
                and ended < expires - timedelta(seconds=30)):
            susp = (d.get("idlePolicy") or {}).get("suspendedDurationSeconds")
            return ("Terminated by its idle policy after " + (f"{susp // 60} min" if susp and susp >= 60 else f"{susp or 0} s")
                    + " suspended")
        return reason

    def view(item: dict, d: dict) -> dict:
        started = d.get("startedAt")
        max_s = d.get("maximumDurationInSeconds") or item.get("max_duration_s")
        expires = (started + timedelta(seconds=max_s)) if isinstance(started, datetime) and max_s else None
        idle = d.get("idlePolicy") or {}
        size = size_of(item, d)
        info = sz.describe(size, mem_quota.peek())
        auto_resume = idle.get("autoResumeEnabled")
        susp = idle.get("suspendedDurationSeconds")
        return {
            "id": item["id"], "name": item.get("name"), "state": d.get("state"),
            "created_at": item.get("created_at"), "started_at": iso(started) or item.get("started_at"),
            "max_duration_s": max_s,
            "idle_minutes": (idle.get("maxIdleDurationSeconds") // 60) if idle.get("maxIdleDurationSeconds") else item.get("idle_minutes"),
            "image_version": d.get("imageVersion") or item.get("image_version"),
            "expires_at": iso(expires), "state_reason": end_reason(d, expires),
            "terminated_at": item.get("terminated_at") if d.get("state") in GONE_STATES else None,
            "size": size, "size_label": info["label"], "memory_gb": info["memory_gb"], "vcpu": info["vcpu"],
            "usd_per_hour": info["usd_per_hour"],
            "egress": egress_of(d) or item.get("egress") or "proxy",
            "auto_resume": auto_resume if isinstance(auto_resume, bool) else item.get("auto_resume", True),
            "suspended_duration_s": susp if isinstance(susp, int) else item.get("suspended_duration_s", max_s),
            "env_names": item.get("env_names") or [],
            **activity.info(item["id"]),
        }

    async def states(ids: list[str], max_age: float = 2.0) -> dict[str, dict]:
        res = await asyncio.gather(*(asyncio.to_thread(mv.get, i, max_age) for i in ids), return_exceptions=True)
        out = {}
        for i, r in zip(ids, res):
            if isinstance(r, Exception):
                log.warning("get_microvm %s: %s", i, r)
                r = {"microvmId": i, "state": "UNKNOWN"}
            out[i] = r
        return out

    async def user_sandboxes(user: User) -> list[tuple[dict, dict]]:
        items = await asyncio.to_thread(db.list, user.sub)
        st = await states([i["id"] for i in items])
        out, now = [], time.time()
        for item in items:
            d = st[item["id"]]
            if d.get("state") == "TERMINATED":
                t = item.get("terminated_at")
                if not t:
                    await asyncio.to_thread(db.update, user.sub, item["id"], terminated_at=now_iso())
                elif now - datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() > TERMINATED_KEEP_S:
                    await asyncio.to_thread(db.delete, user.sub, item["id"])
                    continue
            out.append((item, d))
        return out

    # ---- routes ----
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/me")
    async def me(user: User = Depends(current_user)):
        return user.public(s.admin_group)

    async def custom_env() -> dict:
        """Custom credentials' placeholders, each in the env var chosen for it (control plane only)."""
        try:
            return await asyncio.to_thread(policy.custom_env)
        except Exception as e:
            log.warning("custom credentials: %s", e)
            return {}

    async def size_options(quota_gb: float) -> list[dict]:
        images = s.size_images
        out = []
        for size in sz.SIZES:
            d = sz.describe(size, quota_gb)
            arn = images.get(size)
            # Only a missing or unbuilt image makes a size unavailable. The quota is an estimate: the size's note
            # and the form say when it may not fit, and AWS decides (create always calls RunMicrovm).
            if not arn:
                d.update(available=False, unavailable_reason="no image configured for this size")
            else:
                ok, why = await asyncio.to_thread(mv.image_ready, arn)
                d.update(available=ok, unavailable_reason=why)
            d["default"] = size == sz.DEFAULT_SIZE
            out.append(d)
        return out

    @app.get("/api/config")
    async def config(user: User = Depends(current_user)):
        custom = await custom_env()
        reserved = sandbox_env.reserved_names(s.placeholders, s.region, custom)
        max_min = s.max_duration_s // 60
        quota_gb, quota_source = await asyncio.to_thread(mem_quota.get)
        return {"proxy_mode": s.proxy_mode, "quota_gb": quota_gb, "quota_source": quota_source,
                "max_duration_s": s.max_duration_s,
                "vm_memory_gb": s.vm_memory_gb, "default_idle_minutes": 15, "pricing": pricing.public_pricing(),
                "proxy_allowlist": s.proxy_allowlist, "sandbox_cidrs": s.sandbox_cidrs, "feed_scope_note": FEED_SCOPE,
                "secrets": sorted(s.secret_arns), "dev_auth": s.dev_auth, "control_plane": s.control_plane,
                "sizes": await size_options(quota_gb), "default_size": sz.DEFAULT_SIZE, "burst_factor": sz.BURST,
                "lifetime": {"min_minutes": 1, "max_minutes": max_min, "default_minutes": max_min},
                "idle": {"min_minutes": 1, "max_minutes": max_min - 1, "default_minutes": 15},
                "suspended": {"min_minutes": 0, "max_minutes": max_min, "default": "lifetime"},
                "env": {"name_pattern": sandbox_env.NAME_RE.pattern, "max_vars": sandbox_env.MAX_VARS,
                        "value_max": sandbox_env.VALUE_MAX, "forbidden_chars": "".join(sorted(sandbox_env.FORBIDDEN_CHARS)),
                        "reserved": sorted(reserved), "reserved_prefixes": list(sandbox_env.RESERVED_PREFIXES)},
                # Bytes the payload already takes with the longest name; each variable adds len(name)+len(value)+6
                "run_payload": {"limit": MAX_PAYLOAD, "used_bytes": payload_bytes(s, custom)}}

    @app.get("/api/sandboxes")
    async def list_sandboxes(user: User = Depends(current_user)):
        return [view(item, d) for item, d in await user_sandboxes(user)]

    @app.post("/api/sandboxes", status_code=201)
    async def create_sandbox(body: CreateSandbox, user: User = Depends(current_user)):
        try:
            egress_connectors(s)      # never run without part 2's connector: Lambda would attach INTERNET_EGRESS
        except NoEgressConnector as e:
            raise HTTPException(503, str(e))
        if body.size not in sz.SIZES:
            raise HTTPException(422, f"size must be one of {', '.join(sz.SIZES)}")
        image = s.size_images.get(body.size)
        label = sz.SIZES[body.size]["label"]
        if not image:
            raise HTTPException(409, f"{label} isn't available: no image configured for it")
        ok, why = await asyncio.to_thread(mv.image_ready, image)
        if not ok:
            raise HTTPException(409, f"{label} isn't available: {why}")
        if body.max_minutes is not None:
            max_s = body.max_minutes * 60
        elif body.max_hours is not None:
            max_s = int(body.max_hours * 3600)
        else:
            max_s = s.max_duration_s
        max_s = min(max_s, s.max_duration_s)
        if body.idle_minutes * 60 >= max_s:
            raise HTTPException(422, "the idle time before suspending must be shorter than the lifetime")
        suspended_s = None if body.suspended_minutes is None else body.suspended_minutes * 60
        if suspended_s is not None and suspended_s > max_s:
            raise HTTPException(422, "the time suspended before terminating can't be longer than the lifetime")
        custom = await custom_env()
        try:
            extra = sandbox_env.check(body.env, sandbox_env.reserved_names(s.placeholders, s.region, custom))
        except sandbox_env.EnvError as e:
            raise HTTPException(422, f"environment variables: {e}")
        # Transparent: no proxy settings. Part 2's sandbox VPC answers every name with iron-proxy's load balancer.
        run_env = build_run_payload(s.placeholders, s.region, {**custom, **extra, "SANDBOX_NAME": env_safe_name(body.name)})
        try:
            payload = encode_payload(run_env)
        except ValueError:
            n = len(json.dumps(run_env, separators=(",", ":")).encode())
            mine = sum(sandbox_env.entry_bytes(k, v) for k, v in extra.items())
            raise HTTPException(409, f"run hook payload would be {n:,} bytes (limit {MAX_PAYLOAD:,}); your variables "
                                     f"take {mine:,}" + ("" if not custom else ", custom credentials' placeholders take "
                                     f"{sum(sandbox_env.entry_bytes(k, v) for k, v in custom.items()):,}") +
                                     ". Remove some variables.")
        try:
            r = await asyncio.to_thread(mv.run, idle_minutes=body.idle_minutes, max_duration_s=max_s, payload=payload,
                                        client_token=str(uuid.uuid4()), image_arn=image,
                                        suspended_duration_s=suspended_s, auto_resume=body.auto_resume)
        except QuotaExceeded as e:
            # No pre-check: AWS is the judge. Say what it said, the size and what we estimate now.
            raise HTTPException(409, f"AWS refused {label} ({sz.gb(body.size):g} GB) with ServiceQuotaExceededException: "
                                     f"“{str(e).rstrip('.')}”. {await quota_estimate_text(user)} Pick a smaller size or terminate a sandbox.")
        item = {"sub": user.sub, "id": r["microvmId"], "name": body.name, "created_at": now_iso(),
                "endpoint": r.get("endpoint"), "image_version": r.get("imageVersion"), "idle_minutes": body.idle_minutes,
                "max_duration_s": max_s, "started_at": iso(r.get("startedAt")), "email": user.email,
                "size": body.size, "memory_mib": sz.SIZES[body.size]["memory_mib"], "egress": "proxy",
                "auto_resume": body.auto_resume, "suspended_duration_s": max_s if suspended_s is None else suspended_s,
                # Stored as the form warns: values are visible in the sandbox and kept with its record
                "env": extra, "env_names": list(extra)}
        await asyncio.to_thread(db.put, item)
        hub.poke(user.sub)
        log.info("run %s for %s (%s, %s)", r["microvmId"], user.sub, body.name, body.size)
        return view(item, r)

    # The control plane can still report the old state right after the call; report the transition.
    expected = {"suspend": ({"RUNNING"}, "SUSPENDING"), "resume": ({"SUSPENDED", "SUSPENDING"}, "PENDING"),
                "terminate": ({"PENDING", "RUNNING", "SUSPENDING", "SUSPENDED"}, "TERMINATING")}

    # Where each action ends up, and how long the POST waits for it (suspend ~0.5 s, resume ~1 s measured).
    settled = {"suspend": ({"SUSPENDED"}, 3.0), "resume": ({"RUNNING"}, 3.0), "terminate": ({"TERMINATED"}, 0.0)}
    background: set[asyncio.Task] = set()

    def wake_bridges(vm_id: str) -> None:
        for b in list(bridges.get(vm_id, ())):
            b.wake()

    async def settle(user: User, item: dict, verb: str, budget: float, every: float) -> dict:
        """Poll get_microvm (control plane: doesn't wake the VM) until the action's end state, pushing each
        change to /ws/events and waking attached terminals right away."""
        vm_id, (done, _) = item["id"], settled[verb]
        end = time.monotonic() + budget
        while True:
            d = await asyncio.to_thread(mv.get, vm_id, 0)
            if hub.observe(user.sub, vm_id, item.get("name"), d.get("state")):
                wake_bridges(vm_id)
            if d.get("state") in done or d.get("state") == "TERMINATED" or time.monotonic() >= end:
                return d
            await asyncio.sleep(every)

    async def act(user: User, vm_id: str, fn, verb: str) -> dict:
        item = await owned(user, vm_id)
        try:
            await asyncio.to_thread(fn, vm_id)
        except NotFound:
            raise HTTPException(404, "sandbox no longer exists")
        except Conflict as e:
            raise HTTPException(409, f"can't {verb} now: {e}")
        except QuotaExceeded as e:
            raise HTTPException(409, f"AWS refused to {verb} it with ServiceQuotaExceededException: “{str(e).rstrip('.')}”. "
                                     f"{await quota_estimate_text(user)} Terminate a sandbox, then try again.")
        wake_bridges(vm_id)
        done, budget = settled[verb]
        d = dict(await settle(user, item, verb, budget, 0.25))
        if d.get("state") not in done and d.get("state") != "TERMINATED":
            t = asyncio.create_task(settle(user, item, verb, 30.0, 0.5))
            background.add(t)
            t.add_done_callback(background.discard)
        hub.poke(user.sub)
        stale, nxt = expected[verb]
        if d.get("state") in stale:
            d["state"] = nxt
        return view(item, d)

    @app.post("/api/sandboxes/{vm_id}/suspend")
    async def suspend(vm_id: str, user: User = Depends(current_user)):
        return await act(user, vm_id, mv.suspend, "suspend")

    @app.post("/api/sandboxes/{vm_id}/resume")
    async def resume(vm_id: str, user: User = Depends(current_user)):
        return await act(user, vm_id, mv.resume, "resume")

    @app.delete("/api/sandboxes/{vm_id}")
    async def terminate(vm_id: str, user: User = Depends(current_user)):
        out = await act(user, vm_id, mv.terminate, "terminate")
        await asyncio.to_thread(db.update, user.sub, vm_id, terminated_at=now_iso())
        return out

    @app.get("/api/sandboxes/{vm_id}/vitals")
    async def vitals(vm_id: str, live: int = Query(0, ge=0, le=1), user: User = Depends(current_user)):
        item = await owned(user, vm_id)
        d = await asyncio.to_thread(mv.get, vm_id, 1.0)
        v = view(item, d)
        idle = d.get("idlePolicy") or {}
        now = datetime.now(timezone.utc)
        exp = d.get("startedAt") + timedelta(seconds=v["max_duration_s"]) if isinstance(d.get("startedAt"), datetime) and v["max_duration_s"] else None
        out = {**v, "sampled_at": now_iso(),
               "remaining_s": max(0, int((exp - now).total_seconds())) if exp else None,
               "uptime_s": int((now - d["startedAt"]).total_seconds()) if isinstance(d.get("startedAt"), datetime) else None,
               "idle_policy": {"auto_resume": idle.get("autoResumeEnabled"), "max_idle_s": idle.get("maxIdleDurationSeconds"),
                               "suspended_duration_s": idle.get("suspendedDurationSeconds")},
               "stats": None, "stats_error": None, "pane": None}
        if live:
            if d.get("state") != "RUNNING":
                out["stats_error"] = f"sandbox is {d.get('state')}; live stats only while RUNNING (they would wake it)"
            else:
                endpoint = item.get("endpoint") or d["endpoint"]
                stats, pane = await asyncio.gather(agent.get(vm_id, endpoint, "/stats"),
                                                   agent.post(vm_id, endpoint, "/exec", {"cmd": PANE_PROBE, "timeout": 5}),
                                                   return_exceptions=True)
                if isinstance(stats, HTTPException):
                    out["stats_error"] = stats.detail
                elif isinstance(stats, BaseException):
                    raise stats
                else:
                    out["stats"] = stats
                    activity.touch(vm_id)
                # The pane is a hint for the UI (Start Claude Code); a failed probe just leaves it null.
                if isinstance(pane, dict) and pane.get("rc") == 0:
                    out["pane"] = pane_info(pane.get("stdout") or "")
                elif isinstance(pane, BaseException):
                    log.info("pane probe %s: %s", vm_id, getattr(pane, "detail", pane))
        return out

    @app.get("/api/sandboxes/{vm_id}/claude-usage")
    async def claude_usage(vm_id: str, user: User = Depends(current_user)):
        item = await owned(user, vm_id)
        d = await asyncio.to_thread(mv.get, vm_id, 1.0)
        if d.get("state") != "RUNNING":
            raise HTTPException(409, f"sandbox is {d.get('state')}; usage is read from the running sandbox")
        usage = await agent.get(vm_id, item.get("endpoint") or d["endpoint"], "/claude-usage")
        activity.touch(vm_id)
        return pricing.with_costs(usage)

    @app.get("/api/quota")
    async def quota(user: User = Depends(current_user)):
        return await quota_view(user)

    async def quota_estimate_text(user: User) -> str:
        """Our current estimate, for a ServiceQuotaExceededException message. Never raises."""
        try:
            q = await quota_view(user)
        except Exception as e:      # the message must still go out
            log.info("quota estimate: %s", e)
            return f"This account's MicroVM memory quota is {mem_quota.peek():g} GB as we read it."
        where = "in the account" if q.get("scope") == "account" else "in your sandboxes (others' aren't readable)"
        return (f"Our estimate: {q['used_gb']:g} of {q['quota_gb']:g} GB of MicroVM memory in use {where}, "
                f"{q['free_gb']:g} GB free; suspended sandboxes count.")

    async def quota_view(user: User) -> dict:
        pairs = await user_sandboxes(user)
        own = {item["id"] for item, _ in pairs}
        # The user's own sandboxes are counted from get_microvm (at most 2 s old), never from the cached account
        # list, which can still show one they just terminated as RUNNING and would bill it to "other users".
        # TERMINATING still counts as theirs until it's gone.
        mine = [(item, d) for item, d in pairs if d.get("state") not in ("TERMINATED", "UNKNOWN")]
        segments = [{"id": item["id"], "name": item.get("name"), "state": d.get("state"), "mine": True,
                     "gb": sz.gb(size_of(item, d))} for item, d in mine]
        user_gb = sum(x["gb"] for x in segments)
        quota_gb, quota_source = await asyncio.to_thread(mem_quota.get)
        out = {"quota_gb": quota_gb, "quota_source": quota_source, "vm_memory_gb": s.vm_memory_gb,
               "user_microvms": len(mine), "user_gb": user_gb}
        live, scope = None, "user"
        for kw, sc in (({}, "account"), ({"imageIdentifier": s.image_arn}, "image")):
            if sc == "image" and not s.image_arn:
                continue
            try:
                items = await asyncio.to_thread(mv.list_cached, 10.0, **kw)
                live, scope = [i for i in items if i.get("state") not in GONE_STATES], sc
                break
            except Exception as e:
                log.info("list_microvms (%s): %s", sc, e)
        if live is not None:
            others = [i for i in live if i["microvmId"] not in own]
            unknown = 0
            for i in others:
                size = sz.size_of_arn(s.size_images, i.get("imageArn"))
                unknown += size is None
                # Someone else's image: its size isn't readable here (GetMicrovmImage doesn't return it); count 2 GB
                segments.append({"id": None, "name": None, "state": i.get("state"), "mine": False,
                                 "gb": sz.gb(size) if size else float(s.vm_memory_gb)})
            used = sum(x["gb"] for x in segments)
            out.update(account_microvms=len(others) + len(mine), other_microvms=len(others), used_gb=used,
                       other_gb=used - user_gb, other_unknown_size=unknown, scope=scope)
        else:
            out.update(account_microvms=None, used_gb=user_gb, other_gb=0, other_unknown_size=0, scope="user")
        out["free_gb"] = max(0.0, quota_gb - out["used_gb"])
        out["segments"] = segments
        return out

    @app.get("/api/credentials")
    async def credentials(user: User = Depends(current_user)):
        try:
            return await asyncio.to_thread(creds.summary)
        except Exception as e:
            log.warning("credentials summary: %s", e)
            raise HTTPException(502, "could not read the proxy audit log")

    async def cred_action(name: str, fn, verb: str, user: User) -> dict:
        if not await asyncio.to_thread(creds.known, name):
            raise HTTPException(404, "unknown credential")
        try:
            return await asyncio.to_thread(fn, name, user)
        except PolicyError:
            raise
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            op = e.operation_name
            log.warning("credential %s %s: %s %s", name, verb, op, code)
            if op == "UpdateService":
                raise HTTPException(502, f"could not restart iron-proxy ({code}); the deny policy was taken back off, "
                                         "nothing changed")
            if op in ("PutObject", "GetObject", "HeadObject"):
                raise HTTPException(502, f"could not write the proxy config ({code}); nothing changed")
            raise HTTPException(502, f"could not update the secret's resource policy ({code})")
        except Exception as e:
            log.warning("credential %s %s: %s", name, verb, e)
            raise HTTPException(502, f"could not {verb} {name}: {type(e).__name__}")

    @app.post("/api/credentials/{name}/revoke")
    async def revoke(name: str, user: User = Depends(admin_user)):
        return await cred_action(name, creds.revoke, "revoke", user)

    @app.post("/api/credentials/{name}/restore")
    async def restore(name: str, user: User = Depends(admin_user)):
        return await cred_action(name, creds.restore, "restore", user)

    # ---- proxy policy (control plane) ----
    def is_admin(user: User) -> bool:
        return user.in_group(s.admin_group)

    @app.get("/api/policy")
    async def policy_overview(user: User = Depends(current_user)):
        return await asyncio.to_thread(policy.overview, user, is_admin(user))

    @app.get("/api/policy/allowlist")
    async def policy_allowlist(user: User = Depends(current_user)):
        return await asyncio.to_thread(policy.allowlist)

    @app.post("/api/policy/allowlist", status_code=201)
    async def policy_allow(body: HostBody, user: User = Depends(admin_user)):
        return await asyncio.to_thread(policy.add_host, user, body.host)

    @app.delete("/api/policy/allowlist/{host}")
    async def policy_disallow(host: str, user: User = Depends(admin_user)):
        return await asyncio.to_thread(policy.remove_host, user, host)

    @app.get("/api/policy/credentials")
    async def policy_credentials(user: User = Depends(current_user)):
        items = await asyncio.to_thread(policy.list_credentials)
        try:
            states = {c["name"]: c for c in await asyncio.to_thread(creds.summary)}
        except Exception as e:
            log.warning("credential status: %s", e)
            states = {}
        for item in items:
            st = states.get(item["name"]) or {}
            item.update(status=st.get("status", "unknown"), revoked=st.get("revoked"),
                        status_detail=st.get("status_detail"), status_since=st.get("status_since"))
        return items

    @app.post("/api/policy/credentials", status_code=201)
    async def policy_add_credential(body: NewCredential, user: User = Depends(admin_user)):
        data = body.model_dump(exclude={"value"})
        data["value"] = body.value.get_secret_value()
        out = await asyncio.to_thread(policy.create_credential, user, data)
        data.pop("value", None)
        creds.invalidate()
        return out

    @app.post("/api/policy/credentials/{name}/rotate")
    async def policy_rotate(name: str, body: RotateBody, user: User = Depends(admin_user)):
        return await asyncio.to_thread(policy.rotate, user, name, body.value.get_secret_value())

    @app.delete("/api/policy/credentials/{name}")
    async def policy_delete_credential(name: str, user: User = Depends(admin_user)):
        out = await asyncio.to_thread(policy.delete_credential, user, name)
        creds.invalidate()
        return out

    def cached(body) -> JSONResponse:
        """The catalog changes at most hourly: let the browser keep it for that long, and say how old it is."""
        st = policy.catalog_state()
        headers = {"Cache-Control": f"private, max-age={int(s.catalog_ttl_s)}"}
        if st.get("fetched_at"):
            headers.update({"X-Catalog-Fetched-At": st["fetched_at"], "X-Catalog-Age": str(st.get("age_s") or 0)})
        return JSONResponse(body, headers=headers)

    @app.get("/api/policy/aws/catalog")
    async def policy_catalog(role: str | None = Query(None, max_length=32), user: User = Depends(current_user)):
        return cached(await asyncio.to_thread(policy.catalog, role))

    @app.get("/api/policy/aws/services/{sid}")
    async def policy_aws_service(sid: str, role: str | None = Query(None, max_length=32),
                                 user: User = Depends(current_user)):
        return cached(await asyncio.to_thread(policy.aws_service, sid, role))

    @app.get("/api/policy/aws/search")
    async def policy_aws_search(q: str = Query("", max_length=128), level: str | None = Query(None, max_length=16),
                                limit: int = Query(100, ge=1, le=500), role: str | None = Query(None, max_length=32),
                                user: User = Depends(current_user)):
        return cached(await asyncio.to_thread(policy.aws_search, q, level or None, limit, role))

    @app.get("/api/policy/aws/scope")
    async def policy_scope(user: User = Depends(current_user)):
        return await asyncio.to_thread(policy.scope)

    @app.post("/api/policy/aws/preview")
    async def policy_preview(body: ScopeBody, user: User = Depends(current_user)):
        return await asyncio.to_thread(policy.preview, body.actions, body.role, body.session_policy)

    @app.put("/api/policy/aws/scope")
    async def policy_apply_scope(body: ScopeBody, user: User = Depends(admin_user)):
        return await asyncio.to_thread(policy.apply_scope, user, body.actions, body.role, body.session_policy)

    @app.get("/api/policy/status")
    async def policy_status(user: User = Depends(current_user)):
        return await asyncio.to_thread(policy.status)

    @app.get("/api/policy/config")
    async def policy_config(user: User = Depends(admin_user)):
        """Admins only: iron-proxy.yaml and aws-scope.json as they are in the bucket, against what the sidecar
        applied. Never cached: it's what an admin checks when something looks out of step."""
        return JSONResponse(await asyncio.to_thread(policy.config_view), headers={"Cache-Control": "no-store"})

    @app.get("/api/policy/reset")
    async def policy_reset_plan(user: User = Depends(admin_user)):
        """Admins: what a factory reset would change."""
        revoked = await asyncio.to_thread(creds.revoked_names)
        return await asyncio.to_thread(policy.reset_plan, revoked)

    @app.post("/api/policy/reset")
    async def policy_reset(body: ResetBody, user: User = Depends(admin_user)):
        """Admins: back to part 2's deploy-time policy. The body must say {"confirm": "reset"}."""
        if body.confirm.strip().lower() != "reset":
            raise HTTPException(422, 'type "reset" to confirm')
        await asyncio.to_thread(policy._default_doc)          # fail before touching anything without a template
        revoked = await asyncio.to_thread(creds.clear_denies)
        out = await asyncio.to_thread(policy.reset, user, revoked)
        creds.invalidate()
        return out

    @app.get("/api/policy/changes")
    async def policy_changes(limit: int = Query(50, ge=1, le=200), user: User = Depends(current_user)):
        return await asyncio.to_thread(policy.recent_changes, limit)

    # ---- websockets ----
    @app.websocket("/ws/terminal/{vm_id}")
    async def ws_terminal(ws: WebSocket, vm_id: str):
        await ws.accept()
        user = await ws_auth(ws)
        if not user:
            return
        item = await asyncio.to_thread(db.get, user.sub, vm_id)
        if not item:
            return await ws.close(4404, "sandbox not found")
        d = await asyncio.to_thread(mv.get, vm_id, 2.0)
        endpoint = item.get("endpoint") or d.get("endpoint")
        if not endpoint:
            return await ws.close(4410, "sandbox terminated")
        auto = (d.get("idlePolicy") or {}).get("autoResumeEnabled")
        auto_resume = auto if isinstance(auto, bool) else item.get("auto_resume", True)
        bridge = TerminalBridge(FastAPIBrowser(ws), VmControl(mv, vm_id), connect_for(endpoint),
                                auto_resume=auto_resume,
                                on_attach=lambda: activity.attach(vm_id), on_detach=lambda: activity.detach(vm_id),
                                on_input=lambda: activity.touch(vm_id))
        log.info("terminal open %s", vm_id)
        bridges.setdefault(vm_id, set()).add(bridge)
        try:
            await bridge.run()
        finally:
            bridges.get(vm_id, set()).discard(bridge)
            log.info("terminal closed %s: %s", vm_id, bridge.statuses[-1]["state"] if bridge.statuses else "-")
            if ws.application_state != WebSocketState.DISCONNECTED:
                try:
                    await ws.close(1000)
                except Exception:  # the browser is already gone
                    pass

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        await ws.accept()
        user = await ws_auth(ws)
        if not user:
            return
        q = hub.subscribe(user.sub)
        poke = hub.poke_event(user.sub)
        try:
            await ws.send_text(json.dumps(hub.status()))
        except Exception:
            hub.unsubscribe(q)
            hub.drop_poke_event(user.sub, poke)
            return

        async def sender():
            while True:
                ev = await q.get()
                await ws.send_text(json.dumps(ev, default=str))

        async def reader():
            # {"type":"ping"} -> pong. {"type":"feeds","on":bool}: whether this page shows a feed panel (dock
            # open, Proxy or DNS tab, page visible); the log tail runs only while some connection says true.
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    return
                if m.get("text"):
                    try:
                        msg = json.loads(m["text"])
                        kind = msg.get("type")
                        if kind == "ping":
                            offer(q, {"type": "pong"})
                        elif kind == "feeds":
                            hub.want_feeds(q, msg.get("on") is True)
                    except (ValueError, AttributeError, asyncio.QueueFull):
                        pass

        async def heartbeat():
            # CloudFront closes WebSockets after 60 s of silence; a quiet feed would otherwise drop.
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                offer(q, {"type": "heartbeat"})

        async def lifecycle():
            # Snapshot for this connection, then changes. Changes go through hub.observe(), shared with the
            # post-action settle loop, so a suspend/resume shows up within ~0.5 s and only once.
            fast_until = 0.0
            first = True
            while True:
                try:
                    pairs = await user_sandboxes(user)
                except Exception as e:
                    log.warning("lifecycle poll: %s", e)
                    pairs = None
                if pairs is not None:
                    for item, d in pairs:
                        st = d.get("state")
                        if first and st != "UNKNOWN":
                            offer(q, hub.snapshot_event(user.sub, item["id"], item.get("name"), st))
                        else:
                            hub.observe(user.sub, item["id"], item.get("name"), st)
                    first = False
                wait = 1.0 if time.monotonic() < fast_until else LIFECYCLE_POLL_S
                try:
                    await asyncio.wait_for(poke.wait(), wait)
                    poke.clear()
                    fast_until = time.monotonic() + 10
                except asyncio.TimeoutError:
                    pass

        try:
            # anyio task group: the first of the three to finish (normally the reader, on disconnect) ends all.
            async with anyio.create_task_group() as tg:
                async def until_first(fn):
                    try:
                        await fn()
                    except (WebSocketDisconnect, RuntimeError, OSError):
                        pass
                    except Exception:
                        log.exception("events %s", fn.__name__)
                    tg.cancel_scope.cancel()
                for fn in (sender, reader, lifecycle, heartbeat):
                    tg.start_soon(until_first, fn)
        finally:
            hub.unsubscribe(q)
            hub.drop_poke_event(user.sub, poke)

    return app

