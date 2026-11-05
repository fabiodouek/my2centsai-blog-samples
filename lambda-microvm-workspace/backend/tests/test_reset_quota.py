"""The proxy policy's factory reset (GET/POST /api/policy/reset) and the MicroVM memory quota read from Service
Quotas (app/quota.py), against fakes."""

import json

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.policy import ConfigDoc
from app.quota import MemoryQuota
from tests.test_policy import FIXTURE, Env, hdr

NEW = {"name": "httpbin-test", "env": "TEST_KEY", "hosts": ["httpbin.org"], "headers": ["X-Test-Key"],
       "value": "made-up-value-123"}


@pytest.fixture
def env():
    e = Env()
    e.s3._set("iron-proxy.default.yaml", FIXTURE.encode())
    return e


def dirty(env):
    """An edited policy: a host, a custom credential, demo revoked, read-only with a session policy."""
    assert env.c.post("/api/policy/allowlist", json={"host": "example.com"}, headers=hdr("admin")).status_code == 201
    assert env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin")).status_code == 201
    assert env.c.post("/api/credentials/demo/revoke", headers=hdr("admin")).status_code == 200
    r = env.c.put("/api/policy/aws/scope", json={"role": "read-only", "session_policy": True,
                                                 "actions": ["s3:ListAllMyBuckets", "ssm:*"]}, headers=hdr("admin"))
    assert r.status_code == 200, r.text


def test_plan_lists_exactly_what_changes(env):
    dirty(env)
    assert env.c.get("/api/policy/reset", headers=hdr("viewer")).status_code == 403
    plan = env.c.get("/api/policy/reset", headers=hdr("admin")).json()
    assert plan["allowlist"]["remove"] == ["example.com"] and plan["allowlist"]["add"] == []
    assert plan["credentials"]["reactivate"] == ["demo"]
    custom = plan["credentials"]["remove"]
    assert [c["name"] for c in custom] == ["httpbin-test"] and custom[0]["env"] == "TEST_KEY"
    assert custom[0]["hosts"] == ["httpbin.org"] and custom[0]["secret_name"] == "my2cents-ip/user/httpbin-test"
    assert plan["aws"]["from"] == {"role": "read-only", "session_policy": True, "actions": ["s3:ListAllMyBuckets", "ssm:*"]}
    assert plan["aws"]["to"] == {"role": "view-only", "session_policy": False, "actions": []} and plan["aws"]["changes"]
    assert plan["next_config_version"] == plan["config_version"] + 1 and plan["changes"] == 4
    assert "next check" in plan["note"]


def test_reset_restores_the_defaults_and_keeps_custom_secrets(env):
    dirty(env)
    env.sidecar_applied()
    before = ConfigDoc(env.s3.text("iron-proxy.yaml")).version
    assert env.c.post("/api/policy/reset", json={"confirm": "nope"}, headers=hdr("admin")).status_code == 422
    assert env.c.post("/api/policy/reset", json={"confirm": "reset"}, headers=hdr("viewer")).status_code == 403
    r = env.c.post("/api/policy/reset", json={"confirm": "reset"}, headers=hdr("admin"))
    assert r.status_code == 200, r.text
    out = r.json()
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    default = ConfigDoc(FIXTURE)
    assert doc.version == before + 1 == out["apply"]["config_version"]
    assert [h for h in doc.hosts() if not h.endswith("amazonaws.com")] == [h for h in default.hosts() if not h.endswith("amazonaws.com")]
    assert "example.com" not in doc.hosts()
    # the template's secrets only: the custom one is out of the config...
    assert [e["secret_id"] for e in doc.secret_entries()] == [e["secret_id"] for e in default.secret_entries()]
    # ...and out of the index, but its secret stays in Secrets Manager
    assert env.s3.json("credentials.json")["credentials"] == {}
    assert "my2cents-ip/user/httpbin-test" in env.sm.secrets and not env.sm.secrets["my2cents-ip/user/httpbin-test"]["deleted"]
    # demo active again: no deny statement
    assert env.sm.secrets["my2cents-ip/demo"]["policy"] is None
    # the default AWS scope with the default role's lists
    scope = env.s3.json("aws-scope.json")
    assert (scope["role"], scope["session_policy"], scope["actions"], scope["statements"]) == ("view-only", False, [], [])
    assert scope["version"] == out["apply"]["scope_version"]
    assert "secretsmanager" not in doc.allowed_services() and len(doc.allowed_services()) > 3
    # one change entry; pending until the sidecar applies it
    ch = env.changes.recent()[0]
    assert ch["kind"] == "policy.reset" and ch["detail"]["credentials_removed"] == ["httpbin-test"]
    assert ch["detail"]["reactivated"] == ["demo"] and "Reset the proxy policy" in ch["summary"]
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["pending"] is True and st["target"]["config_version"] == doc.version
    env.sidecar_applied()
    assert env.c.get("/api/policy/status", headers=hdr("viewer")).json()["applied"] is True
    # the credentials page lists the built-ins only, all active
    creds = env.c.get("/api/policy/credentials", headers=hdr("viewer")).json()
    assert [c["name"] for c in creds] == ["anthropic", "github", "demo"]
    # nothing left to reset but the version bump
    plan = env.c.get("/api/policy/reset", headers=hdr("admin")).json()
    assert plan["changes"] == 0


def test_a_reset_credential_can_be_added_again_with_its_name(env):
    """The dialog says so: after a reset, adding the credential again under the same name takes back its secret (kept
    by the reset) with the new value, instead of refusing the name."""
    dirty(env)
    assert env.c.post("/api/policy/reset", json={"confirm": "reset"}, headers=hdr("admin")).status_code == 200
    n_secrets = len(env.sm.secrets)
    r = env.c.post("/api/policy/credentials", json={**NEW, "value": "made-up-value-456"}, headers=hdr("admin"))
    assert r.status_code == 201, r.text
    sec = env.sm.secrets["my2cents-ip/user/httpbin-test"]
    assert sec["value"] == "made-up-value-456" and len(env.sm.secrets) == n_secrets       # the same secret, not a new one
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    assert sec["arn"] in [e["secret_id"] for e in doc.secret_entries()]
    assert "httpbin-test" in env.s3.json("credentials.json")["credentials"]
    assert [c["name"] for c in env.c.get("/api/policy/credentials", headers=hdr("viewer")).json()][-1] == "httpbin-test"


def test_a_secret_that_isnt_ours_still_blocks_the_name(env):
    env.sm.secrets["my2cents-ip/user/httpbin-test"] = {"arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:x",
                                                       "value": "someone else's", "deleted": False, "policy": None, "tags": []}
    r = env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin"))
    assert r.status_code == 409 and "isn't this app's httpbin-test credential" in r.json()["detail"]
    assert env.sm.secrets["my2cents-ip/user/httpbin-test"]["value"] == "someone else's"


def test_reset_without_a_template_touches_nothing(env):
    del env.s3.objects["iron-proxy.default.yaml"]
    env.c.post("/api/credentials/demo/revoke", headers=hdr("admin"))
    r = env.c.post("/api/policy/reset", json={"confirm": "reset"}, headers=hdr("admin"))
    assert r.status_code == 422 and "iron-proxy.default.yaml" in r.json()["detail"]
    assert env.sm.secrets["my2cents-ip/demo"]["policy"] is not None        # still revoked


# ---------------------------------------------------------------- the MicroVM memory quota

class FakeQuotas:
    def __init__(self, value=8.0, unit="Gigabytes", fail=False):
        self.value, self.unit, self.fail, self.calls = value, unit, fail, 0

    def get_service_quota(self, ServiceCode, QuotaCode):
        self.calls += 1
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetServiceQuota")
        assert (ServiceCode, QuotaCode) == ("lambda", "L-CD1C0CC4")
        return {"Quota": {"QuotaName": "Max allocated ARM_64 MicroVM memory", "Value": self.value, "Unit": self.unit,
                          "Adjustable": True}}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_quota_from_service_quotas_cached_an_hour():
    clock, fq = Clock(), FakeQuotas(16)
    q = MemoryQuota(Settings(), client=fq, clock=clock)
    gb, src = q.get()
    assert gb == 16 and src["kind"] == "service_quotas" and src["adjustable"] is True and src["quota_code"] == "L-CD1C0CC4"
    clock.t += 3500
    assert q.gb() == 16 and fq.calls == 1
    clock.t += 200
    fq.value = 24
    assert q.gb() == 24 and fq.calls == 2
    assert MemoryQuota(Settings(), client=FakeQuotas(8192, "Megabytes")).gb() == 8


def test_quota_falls_back_to_quota_gb():
    clock, fq = Clock(), FakeQuotas(fail=True)
    q = MemoryQuota(Settings(quota_gb=8), client=fq, clock=clock)
    gb, src = q.get()
    assert gb == 8 and src["kind"] == "fallback" and "AccessDenied" in src["error"]
    clock.t += 301
    fq.fail, fq.value = False, 12
    assert q.gb() == 12                     # a failure is retried after five minutes


def quota_env(value):
    from tests.test_policy import settings
    img = "arn:aws:lambda:us-east-1:111122223333:microvm-image:my2cents-ws-sandbox"
    e = Env(settings(image_arn=img, image_arns={"small": img + "-1g", "medium": img, "large": img + "-4g",
                                                 "xlarge": img + "-8g"}))
    from app.events import Hub
    from app.main import create_app
    from fastapi.testclient import TestClient
    from tests.test_policy import RoleVerifier
    from app.store import MemoryStore
    e.c = TestClient(create_app(e.s, microvms=e.mv, store=MemoryStore(), verifier=RoleVerifier(), hub=Hub(None),
                                credentials=e.creds, policy=e.policy, quota=MemoryQuota(e.s, client=FakeQuotas(value))))
    return e


def test_api_uses_the_real_quota():
    e = quota_env(16)
    q = e.c.get("/api/quota", headers=hdr("viewer")).json()
    assert q["quota_gb"] == 16 and q["free_gb"] == 16 and q["quota_source"]["kind"] == "service_quotas"
    cfg = e.c.get("/api/config", headers=hdr("viewer")).json()
    xl = next(s for s in cfg["sizes"] if s["id"] == "xlarge")
    assert cfg["quota_gb"] == 16 and xl["note"] is None and xl["available"] is True     # 8 GB isn't the whole quota
    small = quota_env(4).c.get("/api/config", headers=hdr("viewer")).json()
    sizes = {s["id"]: s for s in small["sizes"]}
    assert sizes["large"]["available"] is True and sizes["large"]["note"].startswith("Needs all 4 GB")
    # bigger than the quota as we read it: still offered (AWS decides), with a note
    assert sizes["xlarge"]["available"] is True and sizes["xlarge"]["unavailable_reason"] is None
    assert sizes["xlarge"]["note"].startswith("Bigger than this account's 4 GB MicroVM memory quota as we read it")
