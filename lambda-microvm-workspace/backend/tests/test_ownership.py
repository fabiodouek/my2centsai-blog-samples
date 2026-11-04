"""Every sandbox route is scoped to the caller's sub: another user's sandbox is 404, never touched."""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.events import Hub
from app.main import create_app
from app.config import Settings
from app.store import MemoryStore
from tests.fakes import FakeMicroVMs, FakeVerifier

ORIGIN = {"X-Origin-Verify": "s3cret"}


def hdr(sub):
    return {**ORIGIN, "Authorization": f"Bearer tok-{sub}"}


@pytest.fixture
def env():
    s = Settings(origin_secret="s3cret", user_pool_id="p", client_id="c", table_name="t", image_arn="arn:img",
                 connector_arn="arn:conn", nlb_ips=["10.50.10.10", "10.50.11.10"],
                 placeholders={"anthropic": "sk-ant-placeholder", "github": "ghp_placeholder", "demo": "demo-ph"},
                 secret_arns={"anthropic": "arn:aws:secretsmanager:us-east-1:1:secret:a-AbCdEf"})
    mv, store = FakeMicroVMs(), MemoryStore()
    app = create_app(s, microvms=mv, store=store, verifier=FakeVerifier(), hub=Hub(None))
    return TestClient(app), mv, store


def test_create_and_list_are_per_user(env):
    c, mv, store = env
    r = c.post("/api/sandboxes", json={"name": "alpha", "idle_minutes": 20, "max_hours": 2}, headers=hdr("alice"))
    assert r.status_code == 201, r.text
    a = r.json()
    assert set(a) >= {"id", "name", "state", "created_at", "started_at", "max_duration_s", "idle_minutes", "image_version"}
    assert (a["name"], a["idle_minutes"], a["max_duration_s"], a["state"]) == ("alpha", 20, 7200, "PENDING")
    run = mv.calls[0]
    assert run[1:3] == (20, 7200)
    c.post("/api/sandboxes", json={"name": "bravo"}, headers=hdr("bob"))
    assert [x["name"] for x in c.get("/api/sandboxes", headers=hdr("alice")).json()] == ["alpha"]
    assert [x["name"] for x in c.get("/api/sandboxes", headers=hdr("bob")).json()] == ["bravo"]
    assert store.get("alice", a["id"])["sub"] == "alice"


def test_other_users_sandbox_is_not_found(env):
    c, mv, store = env
    a = c.post("/api/sandboxes", json={"name": "alpha"}, headers=hdr("alice")).json()
    mv.vms[a["id"]]["state"] = "RUNNING"
    vid = a["id"]
    for method, path in [("post", f"/api/sandboxes/{vid}/suspend"), ("post", f"/api/sandboxes/{vid}/resume"),
                         ("delete", f"/api/sandboxes/{vid}"), ("get", f"/api/sandboxes/{vid}/vitals"),
                         ("get", f"/api/sandboxes/{vid}/vitals?live=1"), ("get", f"/api/sandboxes/{vid}/claude-usage")]:
        r = getattr(c, method)(path, headers=hdr("mallory"))
        assert r.status_code == 404, (method, path, r.status_code)
    # nothing was done to alice's MicroVM
    assert [x for x in mv.calls if x[0] != "run"] == []
    assert mv.vms[vid]["state"] == "RUNNING"
    # the owner can
    assert c.post(f"/api/sandboxes/{vid}/suspend", headers=hdr("alice")).json()["state"] == "SUSPENDED"
    mv.vms[vid]["state"] = "RUNNING"
    mv.suspend = lambda i: mv.calls.append(("noop", i))   # control plane still says RUNNING right after the call
    assert c.post(f"/api/sandboxes/{vid}/suspend", headers=hdr("alice")).json()["state"] == "SUSPENDING"
    mv.vms[vid]["state"] = "SUSPENDED"
    assert c.post(f"/api/sandboxes/{vid}/resume", headers=hdr("alice")).json()["state"] == "RUNNING"
    v = c.get(f"/api/sandboxes/{vid}/vitals", headers=hdr("alice")).json()
    assert v["idle_policy"] == {"auto_resume": True, "max_idle_s": 900, "suspended_duration_s": 28800}
    assert v["stats"] is None and v["remaining_s"] > 28000
    assert c.delete(f"/api/sandboxes/{vid}", headers=hdr("alice")).json()["state"] == "TERMINATING"
    assert c.get("/api/sandboxes", headers=hdr("alice")).json()[0]["state"] == "TERMINATING"


def test_terminal_ws_checks_ownership(env):
    c, mv, store = env
    a = c.post("/api/sandboxes", json={"name": "alpha"}, headers=hdr("alice")).json()
    with c.websocket_connect(f"/ws/terminal/{a['id']}", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "tok-mallory"}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4404
    assert mv.calls == [mv.calls[0]]   # no shell token minted, nothing called


def test_quota_409_and_validation(env):
    c, mv, store = env
    mv.quota_full = True
    r = c.post("/api/sandboxes", json={"name": "x"}, headers=hdr("alice"))
    assert r.status_code == 409 and "quota" in r.json()["detail"].lower()
    mv.quota_full = False
    assert c.post("/api/sandboxes", json={"name": ""}, headers=hdr("alice")).status_code == 422
    assert c.post("/api/sandboxes", json={"name": "x", "max_hours": 9}, headers=hdr("alice")).status_code == 422
    assert c.post("/api/sandboxes", json={"name": "x", "idle_minutes": 0}, headers=hdr("alice")).status_code == 422
    assert c.post("/api/sandboxes", json={"name": "x", "idle_minutes": 120, "max_hours": 1},
                  headers=hdr("alice")).status_code == 422


def test_terminated_kept_for_an_hour_then_dropped(env):
    c, mv, store = env
    a = c.post("/api/sandboxes", json={"name": "alpha"}, headers=hdr("alice")).json()
    mv.vms[a["id"]]["state"] = "TERMINATED"
    lst = c.get("/api/sandboxes", headers=hdr("alice")).json()
    assert lst[0]["state"] == "TERMINATED"
    assert store.get("alice", a["id"])["terminated_at"]
    store.update("alice", a["id"], terminated_at="2020-01-01T00:00:00.000Z")
    assert c.get("/api/sandboxes", headers=hdr("alice")).json() == []
    assert store.get("alice", a["id"]) is None


def test_quota_endpoint(env):
    c, mv, store = env
    c.post("/api/sandboxes", json={"name": "a"}, headers=hdr("alice"))
    c.post("/api/sandboxes", json={"name": "b"}, headers=hdr("bob"))
    q = c.get("/api/quota", headers=hdr("alice")).json()
    assert (q["used_gb"], q["quota_gb"], q["user_gb"], q["scope"]) == (4, 8, 2, "account")


def test_config_and_me(env):
    c, _, _ = env
    assert c.get("/api/me", headers=hdr("alice")).json() == {"sub": "alice", "email": "alice@example.com",
                                                                 "groups": [], "is_admin": False}
    cfg = c.get("/api/config", headers=hdr("alice")).json()
    assert cfg["proxy_mode"] == "transparent" and cfg["quota_gb"] == 8 and cfg["max_duration_s"] == 28800
    assert "claude-opus-5-5" in cfg["pricing"]["models"]


def test_create_passes_sandbox_name_to_the_vm(env):
    c, mv, store = env
    c.post("/api/sandboxes", json={"name": "my box #1"}, headers=hdr("alice"))
    payload = json.loads(mv.calls[-1][3])
    assert payload["env"]["SANDBOX_NAME"] == "my box -1"


def test_actions_push_lifecycle_and_return_settled_state(env):
    c, mv, store = env
    a = c.post("/api/sandboxes", json={"name": "alpha"}, headers=hdr("alice")).json()
    mv.vms[a["id"]]["state"] = "RUNNING"
    with c.websocket_connect("/ws/events", headers=ORIGIN) as ws:
        ws.send_json({"type": "auth", "token": "tok-alice"})
        assert ws.receive_json()["type"] == "status"
        snap = ws.receive_json()
        assert (snap["type"], snap["state"], snap["initial"]) == ("lifecycle", "RUNNING", True)
        r = c.post(f"/api/sandboxes/{a['id']}/suspend", headers=hdr("alice")).json()
        assert r["state"] == "SUSPENDED"
        ev = ws.receive_json()
        assert (ev["type"], ev["prev"], ev["state"], ev["initial"]) == ("lifecycle", "RUNNING", "SUSPENDED", False)
        assert c.post(f"/api/sandboxes/{a['id']}/resume", headers=hdr("alice")).json()["state"] == "RUNNING"
        ev = ws.receive_json()
        assert (ev["prev"], ev["state"]) == ("SUSPENDED", "RUNNING")


def test_quota_counts_own_terminating_and_ignores_stale_list(env):
    c, mv, store = env
    a = c.post("/api/sandboxes", json={"name": "a"}, headers=hdr("alice")).json()
    c.post("/api/sandboxes", json={"name": "b"}, headers=hdr("bob"))
    mv.vms[a["id"]]["state"] = "RUNNING"
    assert c.delete(f"/api/sandboxes/{a['id']}", headers=hdr("alice")).json()["state"] == "TERMINATING"
    q = c.get("/api/quota", headers=hdr("alice")).json()
    # alice's terminating sandbox is still hers, not "other users"
    assert (q["used_gb"], q["user_gb"], q["other_microvms"]) == (4, 2, 1)
    # gone per get_microvm, but the cached account list still says RUNNING: not billed to anyone
    mv.vms[a["id"]]["state"] = "TERMINATED"
    mv.list_cached = lambda max_age=10.0, **kw: [{"microvmId": a["id"], "state": "RUNNING"},
                                                 *[i for i in mv.list_all() if i["microvmId"] != a["id"]]]
    q = c.get("/api/quota", headers=hdr("alice")).json()
    assert (q["used_gb"], q["user_gb"], q["other_microvms"]) == (2, 0, 1)
    item = c.get("/api/sandboxes", headers=hdr("alice")).json()[0]
    assert item["state"] == "TERMINATED" and item["terminated_at"]


def test_config_lists_sandbox_cidrs(env):
    c, _, _ = env
    assert c.get("/api/config", headers=hdr("alice")).json()["sandbox_cidrs"] == []
