import json
import time

from botocore.exceptions import ClientError

from app.audit import SecretNames, parse_line
from app.config import Settings
from app.credentials import REVOKE_SID, Credentials, log_group_name
from app.events import log_group_arn
from app.pricing import price_for, session_cost, with_costs

ARNS = {"anthropic": "arn:aws:secretsmanager:us-east-1:111122223333:secret:my2cents-ip/anthropic-d02acc-SkzC7E",
        "github": "arn:aws:secretsmanager:us-east-1:111122223333:secret:my2cents-ip/github-0708ef-jtnLXc",
        "demo": "arn:aws:secretsmanager:us-east-1:111122223333:secret:my2cents-ip/demo-6ea58d-rkmjKD"}


def proxy_line(host="httpbin.org", secret=ARNS["demo"], action="allow", status=200, rejected=None):
    rec = {"time": "2026-11-03T18:00:00.123456Z", "level": "INFO", "msg": "request",
           "audit": {"host": host, "method": "GET", "path": "/anything", "remote_addr": "10.50.10.10:1234",
                     "sni": host, "mode": "mitm", "action": action, "status_code": status, "duration_ms": 12}}
    if rejected:
        rec["rejected_by"] = rejected
    rec["request_transforms"] = [{"name": "allowlist", "action": "continue"},
                                 {"name": "secrets", "action": "continue",
                                  **({"annotations": {"swapped": [{"secret": secret, "locations": ["header:Authorization"]}]}} if secret else {})}]
    return json.dumps(rec)


def test_proxy_event():
    names = SecretNames(ARNS)
    ev = parse_line(proxy_line(), names, 1)
    assert ev == {"type": "proxy", "ts": "2026-11-03T18:00:00.123Z", "action": "allow", "status": 200, "method": "GET",
                  "host": "httpbin.org", "path": "/anything", "mode": "mitm", "duration_ms": 12, "rejected_by": None,
                  "secrets": ["demo"], "aws": None}
    denied = parse_line(proxy_line(host="evil.example", secret=None, action="reject", status=403, rejected="allowlist"), names)
    assert denied["action"] == "reject" and denied["rejected_by"] == "allowlist" and denied["secrets"] == []
    # a secret referenced by name rather than full ARN still maps
    assert parse_line(proxy_line(secret="my2cents-ip/github-0708ef"), names)["secrets"] == ["github"]
    # explicit-mode CONNECT reject (real line shape from iron-proxy): host:443 -> host
    connect = json.dumps({"time": "2026-11-03T19:31:43.803400922Z", "level": "WARN", "msg": "request",
                          "audit": {"host": "example.com:443", "method": "CONNECT", "path": "", "remote_addr": "10.50.10.10:41943",
                                    "sni": "example.com", "mode": "mitm", "action": "reject", "status_code": 403, "duration_ms": 0.13},
                          "rejected_by": "allowlist", "request_transforms": [{"name": "allowlist", "action": "reject"}]})
    ev = parse_line(connect, names)
    assert (ev["host"], ev["method"], ev["action"], ev["rejected_by"], ev["ts"]) == (
        "example.com", "CONNECT", "reject", "allowlist", "2026-11-03T19:31:43.803Z")
    # non-request lines and garbage are dropped
    assert parse_line(json.dumps({"time": "x", "msg": "starting"}), names) is None
    assert parse_line("not json", names) is None


def test_dns_event():
    line = json.dumps({"version": "1.100000", "account_id": "111122223333", "region": "us-east-1", "vpc_id": "vpc-1",
                       "query_timestamp": "2026-11-03T18:00:01Z", "query_name": "api.anthropic.com.", "query_type": "A",
                       "query_class": "IN", "rcode": "NOERROR", "answers": [], "srcaddr": "10.50.20.5", "srcport": "5353"})
    ev = parse_line(line, SecretNames(ARNS))
    assert ev == {"type": "dns", "ts": "2026-11-03T18:00:01.000Z", "query": "api.anthropic.com.", "qtype": "A",
                  "rcode": "NOERROR", "srcaddr": "10.50.20.5"}


def test_log_group_refs():
    assert log_group_name("arn:aws:logs:us-east-1:1:log-group:/my2cents/iron-proxy:*") == "/my2cents/iron-proxy"
    assert log_group_name("/my2cents/iron-proxy") == "/my2cents/iron-proxy"
    assert log_group_arn("/a/b", "us-east-1", "123") == "arn:aws:logs:us-east-1:123:log-group:/a/b"
    assert log_group_arn("arn:aws:logs:us-east-1:123:log-group:/a/b:*", "us-east-1", "123") == "arn:aws:logs:us-east-1:123:log-group:/a/b"


class FakeLogs:
    def __init__(self, lines):
        self.lines, self.calls = lines, []

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        now = int(time.time() * 1000)
        return {"events": [{"message": m, "timestamp": now - i * 1000, "eventId": str(i)} for i, m in enumerate(self.lines)]}


class FakeSM:
    def __init__(self):
        self.policies = {}
        self.calls = []

    def get_resource_policy(self, SecretId):
        return {"ResourcePolicy": self.policies.get(SecretId)} if SecretId in self.policies else {}

    def put_resource_policy(self, SecretId, ResourcePolicy, BlockPublicPolicy):
        self.calls.append(("put", SecretId))
        self.policies[SecretId] = ResourcePolicy

    def delete_resource_policy(self, SecretId):
        self.calls.append(("delete", SecretId))
        self.policies.pop(SecretId, None)

    def get_secret_value(self, **kw):  # must never be called
        raise AssertionError("secret values must never be read")


class FakeECS:
    """iron-proxy's ECS service: update_service starts a deployment; describe_services shows its rollout."""

    def __init__(self):
        from datetime import datetime, timezone
        self.now = lambda: datetime.now(timezone.utc)
        self.deployments = [{"id": "ecs-svc/old", "status": "PRIMARY", "rolloutState": "COMPLETED", "runningCount": 1,
                             "desiredCount": 1, "createdAt": self.now()}]
        self.updates = []
        self.fail = None

    def update_service(self, cluster, service, forceNewDeployment):
        if self.fail:
            raise ClientError({"Error": {"Code": self.fail, "Message": "no"}}, "UpdateService")
        self.updates.append((cluster, service, forceNewDeployment))
        for d in self.deployments:
            d["status"] = "ACTIVE"
        new = {"id": f"ecs-svc/{len(self.updates)}", "status": "PRIMARY", "rolloutState": "IN_PROGRESS",
               "runningCount": 0, "desiredCount": 1, "createdAt": self.now()}
        self.deployments.insert(0, new)
        return {"service": {"deployments": [dict(d) for d in self.deployments]}}

    def complete(self):
        p = self.deployments[0]
        p.update(rolloutState="COMPLETED", runningCount=1)
        self.deployments = [p]

    def describe_services(self, cluster, services):
        return {"services": [{"deployments": [dict(d) for d in self.deployments]}]}


def creds(lines):
    s = Settings(secret_arns=ARNS, proxy_log_group="/my2cents/iron-proxy", task_role_arn="arn:aws:iam::111122223333:role/my2cents-ip-task",
                 placeholders={"anthropic": "sk-ant-api03-proxy-placeholder-0000", "github": "ghp_x", "demo": "demo-placeholder-token"})
    return Credentials(s, logs_client=FakeLogs(lines), sm_client=FakeSM(), ecs_client=FakeECS())


def test_usage_summary():
    c = creds([proxy_line(), proxy_line(), proxy_line(host="api.anthropic.com", secret=ARNS["anthropic"])])
    out = {x["name"]: x for x in c.summary()}
    assert out["demo"]["uses_15m"] == 2 and out["demo"]["hosts"] == {"httpbin.org": 2}
    assert out["anthropic"]["hosts"] == {"api.anthropic.com": 1}
    assert out["github"]["uses_15m"] == 0 and out["github"]["last_used"] is None
    assert out["demo"]["last_used"].endswith("Z") and out["demo"]["revoked"] is False
    assert out["anthropic"]["placeholder_prefix"] == "sk-ant-api03-p…"
    assert c.logs.calls[0]["filterPattern"] == '"swapped"'
    c.summary()
    assert len(c.logs.calls) == 1  # cached for 10 s


def test_revoke_restore_policy():
    c = creds([])
    arn = ARNS["github"]
    # an unrelated existing statement must survive revoke/restore
    other = {"Sid": "Other", "Effect": "Deny", "Principal": {"AWS": "arn:aws:iam::111122223333:role/x"},
             "Action": "secretsmanager:DeleteSecret", "Resource": "*"}
    c.sm.policies[arn] = json.dumps({"Version": "2012-10-17", "Statement": [other]})
    c.revoke("github")
    pol = json.loads(c.sm.policies[arn])
    deny = [s for s in pol["Statement"] if s["Sid"] == REVOKE_SID][0]
    assert deny == {"Sid": REVOKE_SID, "Effect": "Deny", "Principal": {"AWS": "arn:aws:iam::111122223333:role/my2cents-ip-task"},
                    "Action": "secretsmanager:GetSecretValue", "Resource": "*"}
    assert c.is_revoked(arn)
    c.revoke("github")  # idempotent
    assert len([s for s in json.loads(c.sm.policies[arn])["Statement"] if s["Sid"] == REVOKE_SID]) == 1
    c.restore("github")
    assert json.loads(c.sm.policies[arn])["Statement"] == [other] and not c.is_revoked(arn)
    # no other statements: restore deletes the policy
    c.revoke("demo")
    c.restore("demo")
    assert ARNS["demo"] not in c.sm.policies and ("delete", ARNS["demo"]) in c.sm.calls


def test_pricing():
    assert price_for("claude-opus-5-5")[0] == "claude-opus-5-5"
    assert price_for("claude-opus-5")[0] == "claude-opus-5"
    assert price_for("claude-haiku-4-5-20251001")[0] == "claude-haiku-4-5"
    assert price_for("us.anthropic.claude-sonnet-5-5")[0] == "claude-sonnet-5-5"
    assert price_for("claude-opus-5-5[1m]")[0] == "claude-opus-5-5"
    assert price_for("<synthetic>") is None and price_for(None) is None
    # 1M input at $4 + 1M output at $20 + 1M cache read at $0.20 + 1M cache write at $5 (1.25x)
    s = {"model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 1_000_000,
         "cache_read_tokens": 1_000_000, "cache_creation_tokens": 1_000_000}
    assert session_cost(s) == 29.2
    out = with_costs({"sessions": [s, {"model": "mystery-model", "input_tokens": 5}], "totals": {"input_tokens": 1_000_005}})
    assert out["sessions"][0]["est_cost_usd"] == 29.2 and out["sessions"][1]["est_cost_usd"] is None
    assert out["totals"]["est_cost_usd"] == 29.2 and out["totals"]["input_tokens"] == 1_000_005
    assert out["unpriced_models"] == ["mystery-model"]


def test_pricing_text_is_user_facing():
    from app.pricing import public_pricing
    p = public_pricing()
    assert "skill" not in json.dumps(p).lower()
    assert p["note"] == "Estimated from Anthropic list prices as of 2026-11-03"
    out = with_costs({"sessions": []})
    assert out["pricing"] == {"source": "Anthropic", "as_of": "2026-11-03", "note": p["note"]}


def by_name(c):
    return {x["name"]: x for x in c.summary()}


def test_revoke_is_pending_until_the_proxy_restart_completes():
    c = creds([])
    r = c.revoke("demo")
    assert (r["state"], r["revoked"], r["effective_after_s"]) == ("revoking", "pending", 180)
    assert c.ecs.updates == [("my2cents-ip", "iron-proxy", True)]
    d = by_name(c)["demo"]
    assert (d["status"], d["revoked"]) == ("revoking", "pending") and "restarting" in d["status_detail"]
    assert by_name(c)["github"]["status"] == "active"
    c.ecs.complete()
    c._service = None
    d = by_name(c)["demo"]
    assert (d["status"], d["revoked"]) == ("revoked", True)


def test_revoke_rolls_back_if_the_restart_fails():
    c = creds([])
    c.ecs.fail = "AccessDeniedException"
    try:
        c.revoke("demo")
        raise AssertionError("expected a failure")
    except ClientError:
        pass
    assert ARNS["demo"] not in c.sm.policies              # deny policy taken back off
    assert by_name(c)["demo"]["status"] == "active"


def test_restore_is_pending_until_a_swap_shows_up():
    c = creds([])
    c.revoke("demo")
    c.ecs.complete()
    c._service = None
    assert by_name(c)["demo"]["status"] == "revoked"
    r = c.restore("demo")
    assert (r["state"], r["revoked"]) == ("restoring", False)
    assert len(c.ecs.updates) == 1                        # restore doesn't restart the proxy
    d = by_name(c)["demo"]
    assert (d["status"], d["revoked"]) == ("restoring", False)
    # a swap of the demo secret appears in the audit log after the restore
    c._ops["demo"]["since"] -= 1
    c.logs.lines.append(proxy_line())
    c.invalidate()
    assert by_name(c)["demo"]["status"] == "active"


def test_restore_gives_up_waiting_after_90_s():
    c = creds([])
    c.restore("demo")
    c._ops["demo"]["since"] -= 91
    d = by_name(c)["demo"]
    assert d["status"] == "active" and "no swap seen" in d["status_detail"]
