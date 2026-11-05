"""Cognito ID token verification (RS256 against the user pool JWKS), plus the DEV_AUTH fake user."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

log = logging.getLogger("auth")

DEV_USER = {"sub": "dev-user-0000", "email": "dev@localhost", "groups": ["admins"]}


class AuthError(Exception):
    pass


@dataclass
class User:
    sub: str
    email: str | None = None
    # Cognito groups from the ID token's cognito:groups claim; "admins" may change the proxy policy
    groups: list[str] = field(default_factory=list)

    def in_group(self, group: str) -> bool:
        return group in self.groups

    @property
    def actor(self) -> str:
        """Who did it, for the change log and the scoped session's SourceIdentity."""
        return self.email or self.sub

    def public(self, admin_group: str = "admins") -> dict:
        return {"sub": self.sub, "email": self.email, "groups": list(self.groups), "is_admin": self.in_group(admin_group)}


JwksFetcher = Callable[[], Awaitable[dict]]


def http_jwks_fetcher(url: str) -> JwksFetcher:
    async def fetch() -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()
    return fetch


class CognitoVerifier:
    """Verifies Cognito ID tokens: RS256 only, key from the pool JWKS (refetched on an unknown kid at most
    every 60 s), iss = the pool, aud = the app client, token_use = id, exp/iat present and valid."""

    def __init__(self, issuer: str, client_id: str, fetch_jwks: JwksFetcher, leeway: int = 30):
        self.issuer, self.client_id, self.fetch_jwks, self.leeway = issuer, client_id, fetch_jwks, leeway
        self._keys: dict[str, object] = {}
        self._fetched_at = 0.0
        self._forced_at = -1e9
        self._lock = asyncio.Lock()

    async def _refresh(self, force: bool) -> None:
        async with self._lock:
            if not force and self._keys:
                return
            if force:
                # An unknown kid may mean the pool rotated keys; refetch, but at most once a minute so
                # garbage tokens can't turn into a stream of JWKS requests.
                if time.monotonic() - self._forced_at < 60:
                    return
                self._forced_at = time.monotonic()
            jwks = await self.fetch_jwks()
            keys = {}
            for k in jwks.get("keys", []):
                if k.get("kty") == "RSA" and k.get("kid") and k.get("use", "sig") == "sig":
                    keys[k["kid"]] = RSAAlgorithm.from_jwk(json.dumps(k))
            self._keys, self._fetched_at = keys, time.monotonic()

    async def _key(self, kid: str):
        await self._refresh(force=False)
        if kid not in self._keys:
            await self._refresh(force=True)
        if kid not in self._keys:
            raise AuthError("unknown signing key")
        return self._keys[kid]

    async def verify(self, token: str) -> User:
        if not token or not isinstance(token, str):
            raise AuthError("missing token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        if header.get("alg") != "RS256":
            raise AuthError("unexpected algorithm")
        key = await self._key(header.get("kid") or "")
        try:
            claims = jwt.decode(token, key=key, algorithms=["RS256"], audience=self.client_id, issuer=self.issuer,
                                leeway=self.leeway, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {type(e).__name__}") from e
        if claims.get("token_use") != "id":
            raise AuthError("not an ID token")
        groups = claims.get("cognito:groups") or []
        if not isinstance(groups, list):
            groups = []
        return User(sub=claims["sub"], email=claims.get("email"), groups=[str(g) for g in groups])


class DevVerifier:
    """DEV_AUTH=1: every caller is the same fake user. Config refuses this when ORIGIN_SECRET is set."""

    async def verify(self, token: str | None) -> User:
        return User(**DEV_USER)


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None
