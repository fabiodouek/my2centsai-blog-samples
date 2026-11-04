"""Test doubles: JWT signing keys/JWKS, a fake MicroVM control plane, fake WebSockets for the bridge."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import AuthError, User
from app.terminal import UpstreamClosed

REGION = "us-east-1"
POOL = "us-east-1_TESTPOOL"
CLIENT = "testclient123"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"


def _b64(n: int) -> str:
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class KeyPair:
    def __init__(self, kid: str):
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self) -> dict:
        n = self.key.public_key().public_numbers()
        return {"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256", "n": _b64(n.n), "e": _b64(n.e)}

    def token(self, **overrides) -> str:
        now = int(time.time())
        claims = {"sub": "user-a", "email": "a@example.com", "iss": ISSUER, "aud": CLIENT, "token_use": "id",
                  "iat": now, "exp": now + 3600, "auth_time": now}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})


class FakeVerifier:
    """token 'tok-<sub>' -> User(sub)."""

    async def verify(self, token):
        if not token or not str(token).startswith("tok-"):
            raise AuthError("bad token")
        sub = token[4:]
        return User(sub=sub, email=f"{sub}@example.com")


class FakeMicroVMs:
    def __init__(self):
        self.vms: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.n = 0
        self.quota_full = False

    def run(self, *, idle_minutes, max_duration_s, payload, client_token=None):
        from app.microvm import QuotaExceeded
        self.calls.append(("run", idle_minutes, max_duration_s, payload))
        if self.quota_full:
            raise QuotaExceeded("ServiceQuotaExceededException")
        self.n += 1
        vm_id = f"microvm-{self.n:04d}"
        d = {"microvmId": vm_id, "state": "PENDING", "endpoint": f"{vm_id}.example", "imageVersion": "1.0",
             "startedAt": datetime.now(timezone.utc), "maximumDurationInSeconds": max_duration_s,
             "idlePolicy": {"autoResumeEnabled": True, "maxIdleDurationSeconds": idle_minutes * 60,
                            "suspendedDurationSeconds": max_duration_s}}
        self.vms[vm_id] = d
        return dict(d)

    def get(self, vm_id, max_age=2.0):
        d = self.vms.get(vm_id)
        return dict(d) if d else {"microvmId": vm_id, "state": "TERMINATED"}

    def _set(self, vm_id, state):
        from app.microvm import NotFound
        self.calls.append((state, vm_id))
        if vm_id not in self.vms:
            raise NotFound(vm_id)
        self.vms[vm_id]["state"] = state

    def suspend(self, vm_id):
        self._set(vm_id, "SUSPENDED")

    def resume(self, vm_id):
        self._set(vm_id, "RUNNING")

    def terminate(self, vm_id):
        self._set(vm_id, "TERMINATING")

    def list_cached(self, max_age=10.0, **kw):
        return self.list_all(**kw)

    def list_all(self, **kw):
        return [{"microvmId": k, "state": v["state"]} for k, v in self.vms.items()]

    def endpoint_token(self, vm_id):
        return "endpoint-token"

    def shell_token(self, vm_id):
        return "shell-token"


# ---------------- terminal fakes ----------------

class FakeBrowser:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.out: list[tuple[str, object]] = []
        self.closed: tuple[int, str] | None = None
        self.changed = asyncio.Event()

    def type(self, data):  # what the user does
        self.inbox.put_nowait(data)

    def leave(self):
        self.inbox.put_nowait(None)

    async def recv(self):
        return await self.inbox.get()

    async def send_text(self, data):
        self.out.append(("text", data))
        self.changed.set()

    async def send_bytes(self, data):
        self.out.append(("bytes", data))
        self.changed.set()

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        self.changed.set()

    def statuses(self) -> list[dict]:
        return [json.loads(d) for k, d in self.out if k == "text" and json.loads(d).get("type") == "status"]

    def states(self) -> list[str]:
        return [s["state"] for s in self.statuses()]

    def output(self) -> bytes:
        return b"".join(d for k, d in self.out if k == "bytes")

    async def wait_for(self, pred, timeout=2.0):
        end = time.monotonic() + timeout
        while not pred():
            self.changed.clear()
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"timed out; statuses={self.states()} closed={self.closed}")
            try:
                await asyncio.wait_for(self.changed.wait(), min(left, 0.01))
            except asyncio.TimeoutError:
                pass


class FakeUpstream:
    def __init__(self, init: bool = True):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False
        if init:
            self.inbox.put_nowait(json.dumps({"type": "session_init", "session_id": "sess-1"}))

    def emit(self, data):
        self.inbox.put_nowait(data)

    def drop(self, code=1006, reason=""):
        self.inbox.put_nowait(("CLOSE", code, reason))

    async def recv(self):
        m = await self.inbox.get()
        if isinstance(m, tuple):
            self.closed = True
            raise UpstreamClosed(m[1], m[2])
        return m

    async def send(self, data):
        if self.closed:
            raise UpstreamClosed(1006, "")
        self.sent.append(data)

    async def close(self):
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(("CLOSE", 1000, "client closed"))


class FakeControl:
    def __init__(self, state="RUNNING"):
        self.st = state
        self.resumed = 0
        self.tokens = 0

    async def state(self, fresh=False):
        await asyncio.sleep(0)
        return self.st

    async def resume(self):
        self.resumed += 1
        self.st = "RUNNING"

    async def shell_token(self):
        self.tokens += 1
        return f"shell-{self.tokens}"


class Connector:
    """Hands out prepared upstreams in order; an Exception in the list is raised instead."""

    def __init__(self, *ups):
        self.ups = list(ups)
        self.tokens: list[str] = []

    async def __call__(self, token):
        self.tokens.append(token)
        if not self.ups:
            raise OSError("no more upstreams")
        u = self.ups.pop(0)
        if isinstance(u, Exception):
            raise u
        return u
