"""The admin Config view (GET /api/policy/config): the desired state in the control bucket against what part 2's
sidecar reports in status.json, the in-sync badge, the next-poll estimate, and server-side redaction."""

import json
import time

import pytest

from app.policy import now_iso
from app.redact import MARK, redact_json, redact_yaml
from tests.test_policy import FIXTURE, Env, hdr


@pytest.fixture
def env():
    return Env()


def status(env, **over):
    """Edit status.json the way the sidecar would, then drop the backend's cache."""
    s = env.s3.json("status.json")
    for k, v in over.items():
        if isinstance(v, dict):
            s.setdefault(k, {}).update(v)
        else:
            s[k] = v
    env.s3._set("status.json", json.dumps(s).encode())
    env.policy._status_cache = None


def view(env, who="admin"):
    return env.c.get("/api/policy/config", headers=hdr(who))


def test_admins_only(env):
    env.sidecar_applied()
    assert view(env, "viewer").status_code == 403
    r = view(env)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"


def test_desired_matches_the_bucket_and_in_sync(env):
    env.sidecar_applied()
    status(env, config={"etag": env.s3.objects["iron-proxy.yaml"][1]}, poll_s=30)
    v = view(env).json()
    cfg = v["desired"]["config"]
    assert cfg["version"] == 1 and cfg["etag"] == env.s3.objects["iron-proxy.yaml"][1]
    assert cfg["key"] == "iron-proxy.yaml" and cfg["version_id"] == "ver1" and cfg["size"] == len(FIXTURE.encode())
    assert cfg["last_modified"].endswith("Z") and cfg["updated_by"] == "terraform"
    assert v["desired"]["scope"]["version"] == 1 and v["desired"]["scope"]["last_modified"].endswith("Z")
    assert v["applied"]["config"]["version"] == 1 and v["applied"]["scope"]["version"] == 1
    assert v["applied"]["reload"]["ok"] is True and v["applied"]["sidecar"]["poll_s"] == 30
    assert v["sync"]["state"] == "in_sync" and v["sync"]["behind"] == [] and v["sync"]["etag_match"] is True
    assert v["content"]["config"] == FIXTURE and json.loads(v["content"]["scope"])["version"] == 1
    assert v["redactions"] == {"config": [], "scope": []}


def test_a_change_is_pending_until_the_next_poll(env):
    env.sidecar_applied()
    status(env, updated_at=now_iso(time.time() - 10), next_poll_at=now_iso(time.time() + 20), poll_s=30)
    env.c.post("/api/policy/allowlist", json={"host": "example.com"}, headers=hdr("admin"))
    v = view(env).json()
    assert v["desired"]["config"]["version"] == 2 and v["applied"]["config"]["version"] == 1
    assert v["sync"]["state"] == "pending" and v["sync"]["behind"] == ["config"]
    assert "next check (every 30 s)" in v["sync"]["detail"]
    assert 18 <= v["applied"]["sidecar"]["next_poll_in_s"] <= 20
    # the status endpoint says the same
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["pending"] is True and st["sync"]["state"] == "pending" and st["sidecar"]["poll_s"] == 30
    env.sidecar_applied()
    assert view(env).json()["sync"]["state"] == "in_sync"


def test_next_poll_without_a_reported_time(env):
    env.sidecar_applied()
    t = time.time()
    st = env.policy._evaluate({"config_version": 1, "scope_version": 1, "requested_at": None},
                              {**env.s3.json("status.json"), "updated_at": now_iso(t - 12), "poll_s": 30}, t)
    assert 17.5 <= st["sidecar"]["next_poll_in_s"] <= 18.5
    # a round due 3 s ago that hasn't reported counts as running now; 40 s overdue moves on by an interval
    st = env.policy._evaluate({"config_version": 1, "scope_version": 1, "requested_at": None},
                              {**env.s3.json("status.json"), "updated_at": now_iso(t - 33), "poll_s": 30}, t)
    assert st["sidecar"]["next_poll_in_s"] == 0
    st = env.policy._evaluate({"config_version": 1, "scope_version": 1, "requested_at": None},
                              {**env.s3.json("status.json"), "updated_at": now_iso(t - 70), "poll_s": 30}, t)
    assert 19.5 <= st["sidecar"]["next_poll_in_s"] <= 20.5


def test_refused_and_rejected(env):
    env.sidecar_applied()
    env.c.post("/api/policy/allowlist", json={"host": "example.com"}, headers=hdr("admin"))
    status(env, config={"rejected_version": 2, "refused_reason": "the allowlist entry *.amazonaws.com matches ..."})
    v = view(env).json()
    assert v["sync"]["state"] == "refused" and "refused config v2" in v["sync"]["detail"]
    assert v["applied"]["config"]["refused_reason"].startswith("the allowlist entry")
    assert "keeps the last config" in v["sync"]["detail"]
    status(env, config={"refused_reason": None, "synced_version": 2},
           reload={"ok": False, "http_status": 422, "error": "yaml: bad"})
    v = view(env).json()
    assert v["sync"]["state"] == "refused" and "iron-proxy rejected config v2" in v["sync"]["detail"]


def test_stale_without_a_heartbeat_for_90_s(env):
    env.sidecar_applied()
    status(env, updated_at=now_iso(time.time() - 95))
    v = view(env).json()
    assert v["sync"]["state"] == "stale" and v["applied"]["sidecar"]["stale"] is True
    assert v["applied"]["sidecar"]["heartbeat_age_s"] >= 95 and "90 s" in v["sync"]["detail"]
    status(env, updated_at=now_iso(time.time() - 60))
    assert view(env).json()["sync"]["state"] == "in_sync"


def test_no_status_yet_is_stale(env):
    v = view(env).json()
    assert v["sync"]["state"] == "stale" and v["applied"]["status_last_modified"] is None
    assert "hasn't reported" in v["sync"]["detail"]


def test_scope_change_is_pending_on_the_scope(env):
    env.sidecar_applied()
    r = env.c.put("/api/policy/aws/scope", json={"role": "read-only", "session_policy": False, "actions": []},
                  headers=hdr("admin"))
    assert r.status_code == 200, r.text
    v = view(env).json()
    assert v["sync"]["state"] == "pending" and v["sync"]["behind"] == ["config", "scope"]
    assert v["desired"]["scope"]["role"] == "read-only" and v["desired"]["scope"]["version"] == 2
    assert "AWS scope v2" in v["sync"]["detail"]


def test_secret_material_is_redacted_before_it_leaves(env):
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    hand = FIXTURE + (
        "\n# hand-edited, by mistake\n"
        "extra:\n"
        "  token: \"tok-123456789\"\n"
        "  password: hunter2hunter2\n"
        "  empty_value: \"\"\n"
        "  key_block: |\n"
        "    " + pem.replace("\n", "\n    ") + "\n"
        "  value: |\n"
        "    line one\n"
        "    line two\n"
        "  note: sk-ant-api03-" + "Z" * 40 + "\n"
        "  gh: ghp_" + "a" * 36 + "\n"
        "  aws: AKIAABCDEFGHIJKLMNOP\n"
        "after: kept\n")
    env.s3._set("iron-proxy.yaml", hand.encode(), {"version": "1"})
    env.sidecar_applied()
    v = view(env).json()
    text = v["content"]["config"]
    for leaked in ("tok-123456789", "hunter2", "MIIEvQ", "line one", "Z" * 40, "a" * 36, "AKIAABCDEFGHIJKLMNOP"):
        assert leaked not in text
    assert text.count(MARK) >= 7 and "after: kept" in text
    # what the file legitimately holds stays: placeholders, ARNs, the token's env var name, the CA key's path
    for kept in ('proxy_value: "sk-ant-api03-proxy-placeholder', 'proxy_value: "ghp_proxyplaceholder',
                 'api_key_env: "IRON_MANAGEMENT_API_KEY"', 'ca_key: "/etc/iron-proxy/ca.key"', "secret_id:"):
        assert kept in text
    whats = [r["what"] for r in v["redactions"]["config"]]
    assert "a value under token" in whats and "a value under password" in whats and "a PEM private key" in whats
    assert "an Anthropic API key" in whats and "a GitHub token" in whats and "an AWS access key ID" in whats
    assert all(r["line"] > len(FIXTURE.splitlines()) for r in v["redactions"]["config"])
    assert len(text.splitlines()) == len(hand.splitlines())


def test_the_seeded_config_has_nothing_to_redact():
    text, found = redact_yaml(FIXTURE, ["sk-ant-placeholder"])
    assert found == [] and text == FIXTURE


def test_scope_json_redaction():
    text, found = redact_json({"version": 3, "statements": [{"Action": "s3:ListAllMyBuckets"}],
                               "nested": {"secret_access_key": "abc", "token": None},
                               "note": "ASIAABCDEFGHIJKLMNOP"})
    d = json.loads(text)
    assert d["nested"]["secret_access_key"] == MARK and d["nested"]["token"] is None and d["note"] == MARK
    assert d["statements"][0]["Action"] == "s3:ListAllMyBuckets" and len(found) == 2


def test_control_plane_off_is_503():
    from app.config import Settings
    from tests.test_policy import settings
    e = Env(settings(control_bucket=None, sandbox_roles={}, scope_baseline={}))
    assert isinstance(e.s, Settings)
    assert view(e).status_code == 503
