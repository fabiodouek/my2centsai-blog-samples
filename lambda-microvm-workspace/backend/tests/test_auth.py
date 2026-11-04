import asyncio
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main as main_mod
from app.auth import AuthError, CognitoVerifier, bearer
from app.config import ConfigError, Settings, load
from app.main import create_app
from app.store import MemoryStore
from tests.fakes import CLIENT, ISSUER, FakeMicroVMs, KeyPair


@pytest.fixture(scope="module")
def keys():
    return KeyPair("kid-1"), KeyPair("kid-2"), KeyPair("kid-evil")


def verifier_for(*pairs, counter=None):
    jwks = {"keys": [p.jwk() for p in pairs]}

    async def fetch():
        if counter is not None:
            counter.append(1)
        return jwks
    return CognitoVerifier(ISSUER, CLIENT, fetch), jwks


async def test_valid_token(keys):
    v, _ = verifier_for(keys[0])
    u = await v.verify(keys[0].token())
    assert (u.sub, u.email) == ("user-a", "a@example.com")


@pytest.mark.parametrize("claims,msg", [
    ({"exp": int(time.time()) - 120}, "expired"),
    ({"aud": "someone-else"}, "invalid"),
    ({"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"}, "invalid"),
    ({"token_use": "access"}, "not an ID token"),
    ({"token_use": None}, "not an ID token"),
    ({"sub": None}, "invalid"),
    ({"exp": None}, "invalid"),
])
async def test_rejected_claims(keys, claims, msg):
    v, _ = verifier_for(keys[0])
    with pytest.raises(AuthError, match=msg):
        await v.verify(keys[0].token(**claims))


async def test_wrong_key_and_tampering(keys):
    v, _ = verifier_for(keys[0])
    # Signed by a key not in the JWKS but claiming a known kid
    forged = keys[2].token()
    forged = jwt.encode(jwt.decode(forged, options={"verify_signature": False}), keys[2].key, algorithm="RS256",
                        headers={"kid": "kid-1"})
    with pytest.raises(AuthError):
        await v.verify(forged)
    # Payload changed after signing
    h, p, s = keys[0].token().split(".")
    other = keys[0].token(sub="user-b").split(".")[1]
    with pytest.raises(AuthError):
        await v.verify(f"{h}.{other}.{s[:-4]}AAAA")
    with pytest.raises(AuthError):
        await v.verify("not-a-jwt")
    with pytest.raises(AuthError):
        await v.verify("")


async def test_alg_confusion_and_none(keys):
    v, jwks = verifier_for(keys[0])
    pub_pem = keys[0].jwk()["n"]  # any string as an HMAC secret
    hs = jwt.encode({"sub": "x", "iss": ISSUER, "aud": CLIENT, "token_use": "id", "exp": int(time.time()) + 60,
                     "iat": int(time.time())}, pub_pem, algorithm="HS256", headers={"kid": "kid-1"})
    with pytest.raises(AuthError, match="algorithm"):
        await v.verify(hs)
    none = jwt.encode({"sub": "x"}, None, algorithm="none")
    with pytest.raises(AuthError):
        await v.verify(none)


async def test_unknown_kid_refetches_once(keys):
    calls = []
    jwks = {"keys": [keys[0].jwk()]}

    async def fetch():
        calls.append(1)
        return jwks
    v = CognitoVerifier(ISSUER, CLIENT, fetch)
    assert (await v.verify(keys[0].token())).sub == "user-a"
    assert len(calls) == 1
    # Key rotation: kid-2 appears in the JWKS later; an unknown kid triggers one refetch
    jwks["keys"].append(keys[1].jwk())
    assert (await v.verify(keys[1].token(sub="user-b"))).sub == "user-b"
    assert len(calls) == 2
    # A kid that never appears: one more refetch is rate limited (60 s), so no fetch storm
    with pytest.raises(AuthError, match="unknown signing key"):
        await v.verify(keys[2].token())
    with pytest.raises(AuthError, match="unknown signing key"):
        await v.verify(keys[2].token())
    assert len(calls) == 2


def test_bearer_parsing():
    assert bearer("Bearer abc") == "abc"
    assert bearer("bearer  abc ") == "abc"
    assert bearer("Basic abc") is None
    assert bearer("Bearer") is None
    assert bearer(None) is None


# ---------------- config guard rails ----------------

def test_dev_auth_refused_with_origin_secret():
    with pytest.raises(ConfigError):
        load({"DEV_AUTH": "1", "ORIGIN_SECRET": "x"})


def test_production_requires_settings():
    with pytest.raises(ConfigError, match="ORIGIN_SECRET"):
        load({})
    s = load({"ORIGIN_SECRET": "x", "COGNITO_USER_POOL_ID": "p", "COGNITO_CLIENT_ID": "c", "TABLE_NAME": "t"})
    assert not s.dev_auth


def test_dev_mode_ok_without_production_settings():
    s = load({"DEV_AUTH": "1"})
    assert s.dev_auth and s.table_name is None


# ---------------- through the app ----------------

def prod_app(keys, **kw):
    v, _ = verifier_for(keys[0])
    s = Settings(origin_secret="origin-secret", user_pool_id="p", client_id=CLIENT, table_name="t",
                 nlb_ips=["10.0.0.1"], image_arn="img", connector_arn="conn", **kw)
    return create_app(s, microvms=FakeMicroVMs(), store=MemoryStore(), verifier=v, hub=main_mod.Hub(None))


def test_rest_auth_and_origin(keys):
    c = TestClient(prod_app(keys))
    origin = {"X-Origin-Verify": "origin-secret"}
    assert c.get("/healthz").status_code == 200                               # ALB health check, no header
    assert c.get("/api/me", headers={"Authorization": f"Bearer {keys[0].token()}"}).status_code == 403  # no origin
    assert c.get("/api/me", headers={**origin, "X-Origin-Verify": "wrong",
                                     "Authorization": f"Bearer {keys[0].token()}"}).status_code == 403
    assert c.get("/api/me", headers=origin).status_code == 401
    r = c.get("/api/me", headers={**origin, "Authorization": f"Bearer {keys[0].token()}"})
    assert r.status_code == 200 and r.json() == {"sub": "user-a", "email": "a@example.com", "groups": [],
                                                 "is_admin": False}
    r = c.get("/api/me", headers={**origin, "Authorization": f"Bearer {keys[0].token(token_use='access')}"})
    assert r.status_code == 401


def test_ws_origin_and_auth(keys, monkeypatch):
    monkeypatch.setattr(main_mod, "AUTH_TIMEOUT_S", 0.3)
    c = TestClient(prod_app(keys))
    origin = {"X-Origin-Verify": "origin-secret"}
    with pytest.raises(WebSocketDisconnect):  # handshake refused without the origin header
        with c.websocket_connect("/ws/events") as ws:
            ws.receive_text()
    # silence -> 4401 after the auth timeout
    with c.websocket_connect("/ws/events", headers=origin) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401
    # wrong first message -> 4401
    with c.websocket_connect("/ws/terminal/microvm-1", headers=origin) as ws:
        ws.send_text('{"type":"hello"}')
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401
    # bad token -> 4401
    with c.websocket_connect("/ws/terminal/microvm-1", headers=origin) as ws:
        ws.send_text('{"type":"auth","token":"garbage"}')
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4401
    # good token, but not the caller's sandbox -> 4404
    with c.websocket_connect("/ws/terminal/microvm-1", headers=origin) as ws:
        ws.send_json({"type": "auth", "token": keys[0].token()})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4404
    # events: good token -> status message first
    with c.websocket_connect("/ws/events", headers=origin) as ws:
        ws.send_json({"type": "auth", "token": keys[0].token()})
        assert ws.receive_json()["type"] == "status"
