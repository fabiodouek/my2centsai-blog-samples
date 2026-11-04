"""The proxy control plane (app/policy.py, aws_catalog.py and their routes), against fake S3, Secrets Manager
and change log. Nothing here reaches AWS."""

import hashlib
import io
import json
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app import aws_catalog as cat
from app.audit import parse_line
from app.auth import AuthError, User
from app.config import Settings
from app.credentials import REVOKE_SID, Credentials
from app.events import Hub
from app.main import create_app, payload_bytes
from app.policy import ConfigDoc, Invalid, MemoryChangeLog, ProxyPolicy, normalize_host, now_iso
from app.store import MemoryStore
from tests.catalog_fixture import ROLE_DEFS, make_catalog
from tests.fakes import FakeMicroVMs

FIXTURE = (Path(__file__).parent / "data" / "iron-proxy.yaml").read_text()
ACCT = "111122223333"
ARNS = {n: f"arn:aws:secretsmanager:us-east-1:{ACCT}:secret:my2cents-ip/{n}-TEST-AbCdEf" for n in ("anthropic", "github", "demo")}
PH = {"anthropic": "sk-ant-placeholder", "github": "ghp_placeholder", "demo": "demo-placeholder-token"}
BASELINE = {"Sid": "ProxySecrets", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
            "Resource": [*ARNS.values(), f"arn:aws:secretsmanager:us-east-1:{ACCT}:secret:my2cents-ip/user/*"]}
TASK_ROLE = f"arn:aws:iam::{ACCT}:role/my2cents-ip-task"
ROLE_ARNS = [f"arn:aws:iam::{ACCT}:role/my2cents-ip-{r}" for r in ("builder", "data-reader", "read-only", "view-only")]
SECRET = "real-value-SUPERSECRET-123"
ORIGIN = {"X-Origin-Verify": "s3cret"}


def err(code, op, msg=""):
    return ClientError({"Error": {"Code": code, "Message": msg}}, op)


class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str, dict]] = {}
        self.puts: list[str] = []
        self.race: int = 0   # change the object behind the writer's back this many times

    def _set(self, key, body: bytes, md=None):
        self.objects[key] = (body, '"' + hashlib.md5(body).hexdigest() + '"', md or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise err("NoSuchKey", "GetObject")
        b, e, md = self.objects[Key]
        return {"Body": io.BytesIO(b), "ETag": e, "Metadata": md}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise err("404", "HeadObject")
        _, e, md = self.objects[Key]
        return {"ETag": e, "Metadata": md}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None, Metadata=None):
        if self.race and Key in self.objects:
            self.race -= 1
            b, _, md = self.objects[Key]
            self._set(Key, b + b"\n", md)   # someone else wrote meanwhile
        if IfMatch and (Key not in self.objects or self.objects[Key][1] != IfMatch):
            raise err("PreconditionFailed", "PutObject")
        if IfNoneMatch == "*" and Key in self.objects:
            raise err("PreconditionFailed", "PutObject")
        self._set(Key, Body, Metadata)
        self.puts.append(Key)
        return {"ETag": self.objects[Key][1]}

    def text(self, key) -> str:
        return self.objects[key][0].decode()

    def json(self, key) -> dict:
        return json.loads(self.objects[key][0])


class FakeSM:
    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.calls: list[tuple] = []
        for n, arn in ARNS.items():
            self.secrets[f"my2cents-ip/{n}"] = {"arn": arn, "value": f"real-{n}", "deleted": False, "policy": None}

    def _find(self, sid):
        for name, d in self.secrets.items():
            if sid in (name, d["arn"]):
                return d
        raise err("ResourceNotFoundException", "Describe")

    def create_secret(self, Name, SecretString, Description=None, Tags=None):
        self.calls.append(("create", Name))
        if Name in self.secrets:
            if self.secrets[Name]["deleted"]:
                raise err("InvalidRequestException", "CreateSecret", "You can't create this secret because a secret "
                          "with this name is already scheduled for deletion.")
            raise err("ResourceExistsException", "CreateSecret")
        arn = f"arn:aws:secretsmanager:us-east-1:{ACCT}:secret:{Name}-Xy12Zw"
        self.secrets[Name] = {"arn": arn, "value": SecretString, "deleted": False, "policy": None}
        return {"ARN": arn, "Name": Name}

    def put_secret_value(self, SecretId, SecretString):
        self.calls.append(("put_value", SecretId))
        d = self._find(SecretId)
        d["value"] = SecretString
        return {"ARN": d["arn"]}

    def delete_secret(self, SecretId, RecoveryWindowInDays=None, ForceDeleteWithoutRecovery=None):
        self.calls.append(("delete", SecretId, RecoveryWindowInDays, ForceDeleteWithoutRecovery))
        d = self._find(SecretId)
        if ForceDeleteWithoutRecovery:
            self.secrets = {k: v for k, v in self.secrets.items() if v is not d}
        else:
            d["deleted"] = True

    def restore_secret(self, SecretId):
        self.calls.append(("restore", SecretId))
        self._find(SecretId)["deleted"] = False

    def describe_secret(self, SecretId):
        d = self._find(SecretId)
        return {"ARN": d["arn"], **({"DeletedDate": "x"} if d["deleted"] else {})}

    def get_resource_policy(self, SecretId):
        p = self._find(SecretId)["policy"]
        return {"ResourcePolicy": p} if p else {}

    def put_resource_policy(self, SecretId, ResourcePolicy, BlockPublicPolicy=True):
        self._find(SecretId)["policy"] = ResourcePolicy

    def delete_resource_policy(self, SecretId):
        self._find(SecretId)["policy"] = None


class FakeLogs:
    def filter_log_events(self, **kw):
        return {"events": []}


class FakeECS:
    def __init__(self):
        self.calls = []

    def update_service(self, **kw):
        self.calls.append(kw)
        raise AssertionError("the control plane must reload, not restart")


class RoleVerifier:
    """'admin' -> in the admins group, 'viewer' -> signed in, no group."""

    async def verify(self, token):
        if token == "admin":
            return User(sub="sub-admin", email="admin@example.com", groups=["admins"])
        if token == "viewer":
            return User(sub="sub-viewer", email="viewer@example.com", groups=[])
        raise AuthError("bad token")


def hdr(who):
    return {**ORIGIN, "Authorization": f"Bearer {who}"}


def settings(**kw) -> Settings:
    base = dict(origin_secret="s3cret", user_pool_id="p", client_id="c", table_name="t", image_arn="arn:img",
                connector_arn="arn:conn", nlb_ips=["10.50.10.10"], placeholders=PH, secret_arns=ARNS,
                task_role_arn=TASK_ROLE, control_bucket="ctl", sandbox_roles=ROLE_DEFS, scope_baseline=BASELINE)
    base.update(kw)
    return Settings(**base)


class Env:
    def __init__(self, s: Settings | None = None, payload_size=None):
        self.s = s or settings()
        self.s3, self.sm, self.changes, self.ecs = FakeS3(), FakeSM(), MemoryChangeLog(), FakeECS()
        self.s3._set("iron-proxy.yaml", FIXTURE.encode(), {"version": "1", "updated-by": "terraform"})
        self.s3._set("aws-scope.json", json.dumps({"version": 1, "updated_by": "terraform", "actions": [
            "ecs:DescribeClusters", "ecs:ListClusters", "ecs:ListServices", "s3:ListAllMyBuckets", "sts:GetCallerIdentity"],
            "statements": []}).encode())
        self.policy = ProxyPolicy(self.s, s3=self.s3, sm=self.sm, changes=self.changes, catalog=make_catalog(BASELINE),
                                  payload_size=payload_size or (lambda extra: payload_bytes(self.s, extra)))
        self.creds = Credentials(self.s, logs_client=FakeLogs(), sm_client=self.sm, ecs_client=self.ecs,
                                 catalog=self.policy.credential_catalog, policy=self.policy)
        self.mv = FakeMicroVMs()
        self.app = create_app(self.s, microvms=self.mv, store=MemoryStore(), verifier=RoleVerifier(), hub=Hub(None),
                              credentials=self.creds, policy=self.policy)
        self.c = TestClient(self.app)

    def sidecar_applied(self, at: float | None = None):
        """What part 2's sidecar writes after a successful reload of the current versions."""
        cfg = ConfigDoc(self.s3.text("iron-proxy.yaml")).version
        scope = self.s3.json("aws-scope.json")["version"]
        t = now_iso(at)
        self.s3._set("status.json", json.dumps({
            "updated_at": now_iso(), "task": "task123",
            "config": {"synced_version": cfg, "applied_version": cfg, "applied_at": t},
            "scope": {"synced_version": scope, "applied_version": scope, "applied_at": t,
                      "session_name": f"ws-scope-v{scope}", "source_identity": "admin@example.com",
                      "expiration": "2026-11-03T23:40:00Z", "session_token_utilization": 31,
                      "session_token_size": 1290, "packed_policy_size": 9, "error": None},
            "reload": {"at": t, "ok": True, "http_status": 200, "error": None}}).encode())
        self.policy._status_cache = None


@pytest.fixture
def env():
    return Env()


# ---------------------------------------------------------------- hosts

@pytest.mark.parametrize("raw,want", [("example.com", "example.com"), ("Example.COM.", "example.com"),
                                       ("*.example.com", "*.example.com"), (" api.openai.com ", "api.openai.com"),
                                       ("xn--bcher-kva.example", "xn--bcher-kva.example")])
def test_hosts_accepted(raw, want):
    assert normalize_host(raw) == want


@pytest.mark.parametrize("raw", ["", "1.2.3.4", "::1", "[::1]", "*", "*.com", "*.co.uk", "*.github.io", "foo*.com",
                                 "a.*.example.com", "https://example.com", "example.com:443", "example.com/path",
                                 "localhost", "-bad.example.com", "sts.amazonaws.com", "*.s3.amazonaws.com",
                                 "secretsmanager.us-east-1.amazonaws.com", "user@example.com", "a_b.example.com"])
def test_hosts_refused(raw):
    with pytest.raises(Invalid):
        normalize_host(raw)


# ---------------------------------------------------------------- config document

def test_config_round_trip_keeps_everything():
    doc = ConfigDoc(FIXTURE)
    assert doc.version == 1
    out = doc.dump(2, "2026-11-03T22:00:00.000Z", "a@b.c")
    assert out.splitlines()[0] == "# my2cents control plane: config v2, 2026-11-03T22:00:00.000Z, a@b.c"
    assert out.splitlines()[1:] == FIXTURE.splitlines()[1:]   # comments, quoting, order: byte for byte


def test_config_edits_touch_only_their_lines():
    doc = ConfigDoc(FIXTURE)
    doc.add_host("example.com")
    lines = doc.dump(2, "t", "a").splitlines()[1:]
    old = FIXTURE.splitlines()[1:]
    assert len(lines) == len(old) + 1
    i = lines.index('        - "example.com"')
    # custom hosts go before the AWS endpoints
    assert lines[i - 1] == '        - "httpbin.org"' and lines[i + 1] == '        - "sts.amazonaws.com"'
    assert lines[:i] + lines[i + 1:] == old


# ---------------------------------------------------------------- allowlist API

def test_allowlist_add_remove_and_versions(env):
    r = env.c.get("/api/policy/allowlist", headers=hdr("viewer"))
    assert r.status_code == 200
    hosts = {h["host"]: h for h in r.json()["hosts"]}
    assert hosts["httpbin.org"]["managed"] is None and hosts["sts.amazonaws.com"]["managed"] == "aws"
    assert hosts["sts.amazonaws.com"]["service"] == "sts"

    r = env.c.post("/api/policy/allowlist", json={"host": "Example.com"}, headers=hdr("admin"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["host"] == "example.com" and body["version"] == 2 and body["apply"]["config_version"] == 2
    assert body["change"]["kind"] == "allowlist.add" and body["change"]["actor"] == "admin@example.com"
    assert env.s3.text("iron-proxy.yaml").startswith("# my2cents control plane: config v2, ")
    assert env.s3.objects["iron-proxy.yaml"][2]["version"] == "2"
    assert '"example.com"' in env.s3.text("iron-proxy.yaml")

    assert env.c.post("/api/policy/allowlist", json={"host": "example.com"}, headers=hdr("admin")).status_code == 409
    r = env.c.post("/api/policy/allowlist", json={"host": "10.0.0.1"}, headers=hdr("admin"))
    assert r.status_code == 422 and "IP" in r.json()["detail"]
    assert env.c.delete("/api/policy/allowlist/sts.amazonaws.com", headers=hdr("admin")).status_code == 409
    assert env.c.delete("/api/policy/allowlist/nope.example", headers=hdr("admin")).status_code == 404
    r = env.c.delete("/api/policy/allowlist/example.com", headers=hdr("admin"))
    assert r.status_code == 200 and r.json()["version"] == 3
    assert '"example.com"' not in env.s3.text("iron-proxy.yaml")
    kinds = [c["kind"] for c in env.c.get("/api/policy/changes", headers=hdr("viewer")).json()]
    assert kinds == ["allowlist.remove", "allowlist.add"]


def test_viewers_read_but_cannot_write(env):
    assert env.c.get("/api/policy", headers=hdr("viewer")).json()["editable"] is False
    assert env.c.get("/api/policy", headers=hdr("admin")).json()["editable"] is True
    assert env.c.get("/api/me", headers=hdr("admin")).json()["is_admin"] is True
    for method, path, body in [("post", "/api/policy/allowlist", {"host": "example.com"}),
                               ("delete", "/api/policy/allowlist/httpbin.org", None),
                               ("post", "/api/policy/credentials", {"name": "x1", "env": "X1", "hosts": ["a.example"], "value": "v"}),
                               ("post", "/api/policy/credentials/demo/rotate", {"value": "v"}),
                               ("delete", "/api/policy/credentials/demo", None),
                               ("put", "/api/policy/aws/scope", {"actions": []}),
                               ("post", "/api/credentials/demo/revoke", None),
                               ("post", "/api/credentials/demo/restore", None)]:
        kw = {"json": body} if body is not None else {}
        r = getattr(env.c, method)(path, headers=hdr("viewer"), **kw)
        assert r.status_code == 403 and "admins only" in r.json()["detail"], (method, path, r.status_code)
    assert env.s3.puts == [] and env.sm.calls == []


def test_concurrent_edit_retries_once_then_409(env):
    env.s3.race = 1
    assert env.c.post("/api/policy/allowlist", json={"host": "a.example.com"}, headers=hdr("admin")).status_code == 201
    env.s3.race = 2
    r = env.c.post("/api/policy/allowlist", json={"host": "b.example.com"}, headers=hdr("admin"))
    assert r.status_code == 409 and "changed meanwhile" in r.json()["detail"]


def test_control_plane_off():
    e = Env(settings(control_bucket=None))
    assert e.c.get("/api/policy", headers=hdr("admin")).json()["enabled"] is False
    for path in ("/api/policy/allowlist", "/api/policy/credentials", "/api/policy/aws/catalog", "/api/policy/changes"):
        r = e.c.get(path, headers=hdr("admin"))
        assert r.status_code == 503 and "control_plane=true" in r.json()["detail"]


# ---------------------------------------------------------------- credentials

NEW = {"name": "httpbin-test", "env": "TEST_KEY", "hosts": ["httpbin.org"], "headers": ["X-Test-Key"], "value": SECRET}


def test_create_credential(env):
    r = env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin"))
    assert r.status_code == 201, r.text
    assert SECRET not in r.text
    out = r.json()
    cred = out["credential"]
    assert cred["kind"] == "custom" and cred["env"] == "TEST_KEY" and cred["hosts"] == ["httpbin.org"]
    assert cred["headers"] == ["X-Test-Key"] and cred["on_allowlist"] is True
    assert cred["placeholder"].startswith("m2c-ph-httpbin-test-") and len(cred["placeholder"]) == len("m2c-ph-httpbin-test-") + 16
    assert out["apply"]["config_version"] == 2 and out["warnings"] == []
    # the value is in Secrets Manager and nowhere else
    assert env.sm.secrets["my2cents-ip/user/httpbin-test"]["value"] == SECRET
    for key, (body, _, _) in env.s3.objects.items():
        assert SECRET.encode() not in body, key
    assert SECRET not in json.dumps(env.changes.items)
    # iron-proxy config: a replace-mode aws_sm secret with the short failure TTL
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    entry = [e for e in doc.secret_entries() if "user/httpbin-test" in e["secret_id"]][0]
    assert entry == {"secret_id": env.sm.secrets["my2cents-ip/user/httpbin-test"]["arn"], "placeholder": cred["placeholder"],
                     "headers": ["X-Test-Key"], "query": False, "body": False, "hosts": ["httpbin.org"]}
    assert 'failure_ttl: "5s"' in env.s3.text("iron-proxy.yaml")
    assert env.s3.json("credentials.json")["credentials"]["httpbin-test"]["env"] == "TEST_KEY"
    # listed after the built-ins, with a status
    names = [(c["name"], c["kind"]) for c in env.c.get("/api/policy/credentials", headers=hdr("viewer")).json()]
    assert names == [("anthropic", "builtin"), ("github", "builtin"), ("demo", "builtin"), ("httpbin-test", "custom")]


def test_new_sandboxes_get_the_placeholder(env):
    ph = env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin")).json()["credential"]["placeholder"]
    r = env.c.post("/api/sandboxes", json={"name": "cp-1"}, headers=hdr("viewer"))
    assert r.status_code == 201, r.text
    payload = json.loads(env.mv.calls[0][3])
    assert payload["env"]["TEST_KEY"] == ph and payload["env"]["SANDBOX_NAME"] == "cp-1"
    assert SECRET not in env.mv.calls[0][3]


def test_feed_names_custom_credentials(env):
    env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin"))
    arn = env.sm.secrets["my2cents-ip/user/httpbin-test"]["arn"]
    line = json.dumps({"time": "2026-11-03T18:00:00Z", "audit": {"host": "httpbin.org", "method": "GET", "path": "/headers",
                                                                 "action": "allow", "status_code": 200},
                       "request_transforms": [{"name": "secrets", "annotations": {"swapped": [
                           {"secret": arn, "locations": ["header:X-Test-Key"]}]}}]})
    env.creds.names._at = -1e9
    assert parse_line(line, env.creds.names)["secrets"] == ["httpbin-test"]


@pytest.mark.parametrize("patch,status,needle", [
    ({"name": "Bad Name"}, 422, "name"), ({"env": "PATH"}, 422, "reserved"), ({"env": "AWS_X"}, 422, "reserved"),
    ({"env": "lower"}, 422, "env"), ({"hosts": ["1.2.3.4"]}, 422, "IP"), ({"hosts": ["*"]}, 422, "bare"),
    ({"headers": ["Host"]}, 422, "Host"), ({"headers": ["bad header"]}, 422, "header name"),
    ({"name": "demo"}, 409, "already exists"), ({"env": "DEMO_TOKEN"}, 422, "reserved"),
    ({"value": "two\nlines"}, 422, "line breaks"), ({"value": ""}, 422, "value"), ({"hosts": []}, 422, "hosts"),
])
def test_create_credential_validation_never_echoes_the_value(env, patch, status, needle):
    body = {**NEW, **patch}
    r = env.c.post("/api/policy/credentials", json=body, headers=hdr("admin"))
    assert r.status_code == status, r.text
    assert needle in r.text
    assert SECRET not in r.text and "two\nlines" not in r.text and "input" not in r.text
    assert not any(c[0] == "create" for c in env.sm.calls)


def test_warns_when_a_host_is_not_allowlisted(env):
    r = env.c.post("/api/policy/credentials", json={**NEW, "hosts": ["api.example.org"]}, headers=hdr("admin"))
    assert r.status_code == 201
    assert r.json()["credential"]["on_allowlist"] is False
    assert [w for w in r.json()["warnings"] if "api.example.org isn't on the allowlist" in w]


def test_env_var_names_are_unique(env):
    assert env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin")).status_code == 201
    r = env.c.post("/api/policy/credentials", json={**NEW, "name": "other"}, headers=hdr("admin"))
    assert r.status_code == 409 and "already used" in r.json()["detail"]
    r = env.c.post("/api/policy/credentials", json={**NEW, "env": "OTHER_KEY"}, headers=hdr("admin"))
    assert r.status_code == 409 and "already exists" in r.json()["detail"]


def test_payload_limit_blocks_a_credential():
    e = Env(payload_size=lambda extra: 5000)
    r = e.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin"))
    assert r.status_code == 409 and "4,096" in r.json()["detail"]
    assert e.sm.calls == []


def test_failed_config_write_removes_the_new_secret(env):
    env.s3.race = 2
    r = env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin"))
    assert r.status_code == 409
    assert "my2cents-ip/user/httpbin-test" not in env.sm.secrets
    assert ("delete", env.sm.calls[-1][1], None, True) == env.sm.calls[-1]


def test_rotate_and_delete(env):
    env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin"))
    v = env.policy.overview(None, True)["config_version"]
    r = env.c.post("/api/policy/credentials/httpbin-test/rotate", json={"value": "rotated-SUPERSECRET-2"}, headers=hdr("admin"))
    assert r.status_code == 200 and "rotated-SUPERSECRET-2" not in r.text
    assert r.json()["effective_within_s"] == 30
    assert env.sm.secrets["my2cents-ip/user/httpbin-test"]["value"] == "rotated-SUPERSECRET-2"
    assert env.policy.overview(None, True)["config_version"] == v       # no reload needed
    assert env.c.post("/api/policy/credentials/demo/rotate", json={"value": "x"}, headers=hdr("admin")).status_code == 409
    assert env.c.post("/api/policy/credentials/nope/rotate", json={"value": "x"}, headers=hdr("admin")).status_code == 404

    r = env.c.delete("/api/policy/credentials/httpbin-test", headers=hdr("admin"))
    assert r.status_code == 200 and r.json()["recovery_window_days"] == 7
    assert "user/httpbin-test" not in env.s3.text("iron-proxy.yaml")
    assert env.s3.json("credentials.json")["credentials"] == {}
    assert env.sm.secrets["my2cents-ip/user/httpbin-test"]["deleted"] is True
    assert env.c.delete("/api/policy/credentials/demo", headers=hdr("admin")).status_code == 409
    # the same name again inside the recovery window: restored and overwritten
    r = env.c.post("/api/policy/credentials", json={**NEW, "value": "third-value"}, headers=hdr("admin"))
    assert r.status_code == 201, r.text
    d = env.sm.secrets["my2cents-ip/user/httpbin-test"]
    assert d["deleted"] is False and d["value"] == "third-value"
    kinds = [c["kind"] for c in env.changes.recent()]
    assert kinds == ["credential.create", "credential.delete", "credential.rotate", "credential.create"]


# ---------------------------------------------------------------- revoke / restore by reload

def test_revoke_and_restore_by_reload(env):
    r = env.c.post("/api/credentials/demo/revoke", headers=hdr("admin"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["method"] == "reload" and out["apply"]["config_version"] == 2 and out["effective_after_s"] == 10
    assert env.ecs.calls == []
    pol = json.loads(env.sm.secrets["my2cents-ip/demo"]["policy"])
    deny = [st for st in pol["Statement"] if st["Sid"] == REVOKE_SID][0]
    assert deny["Principal"]["AWS"][0] == TASK_ROLE and sorted(deny["Principal"]["AWS"][1:]) == ROLE_ARNS
    assert deny["Effect"] == "Deny"
    env.creds.invalidate()
    demo = [c for c in env.c.get("/api/credentials", headers=hdr("viewer")).json() if c["name"] == "demo"][0]
    assert demo["status"] == "revoking"
    env.sidecar_applied()
    env.creds.invalidate()
    demo = [c for c in env.c.get("/api/credentials", headers=hdr("viewer")).json() if c["name"] == "demo"][0]
    assert demo["status"] == "revoked" and demo["revoked"] is True

    r = env.c.post("/api/credentials/demo/restore", headers=hdr("admin"))
    assert r.status_code == 200 and r.json()["method"] == "reload" and r.json()["apply"]["config_version"] == 3
    assert env.sm.secrets["my2cents-ip/demo"]["policy"] is None
    assert [c["kind"] for c in env.changes.recent()] == ["credential.restore", "credential.revoke"]


def test_revoke_custom_credential(env):
    env.c.post("/api/policy/credentials", json=NEW, headers=hdr("admin"))
    r = env.c.post("/api/credentials/httpbin-test/revoke", headers=hdr("admin"))
    assert r.status_code == 200, r.text
    assert env.sm.secrets["my2cents-ip/user/httpbin-test"]["policy"]
    assert env.c.post("/api/credentials/nope/revoke", headers=hdr("admin")).status_code == 404


# ---------------------------------------------------------------- AWS scope

def test_catalog_routes_are_cached_an_hour_and_gzipped(env):
    r = env.c.get("/api/policy/aws/catalog", headers={**hdr("viewer"), "Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.headers["content-encoding"] == "gzip"
    assert int(r.headers["x-catalog-age"]) >= 0 and r.headers["x-catalog-fetched-at"].endswith("Z")
    body = r.json()
    assert body["source"]["services"] == 24 and body["role"] == "view-only" and body["ceiling"]["role"] == "view-only"
    assert not any("secretsmanager" == s["id"] and s["allowed"] for s in body["services"])
    r = env.c.get("/api/policy/aws/services/dynamodb?role=read-only", headers=hdr("viewer"))
    assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=3600"
    assert any(a["action"] == "dynamodb:ListTables" and a["allowed"] for a in r.json()["actions"])
    assert env.c.get("/api/policy/aws/services/nope", headers=hdr("viewer")).status_code == 404
    r = env.c.get("/api/policy/aws/search?q=GetObject&level=Read&role=read-only", headers=hdr("viewer"))
    assert r.status_code == 200 and r.json()["actions"][0]["action"] == "s3:GetObject"
    assert env.c.get("/api/policy/aws/search?q=x&level=Admin", headers=hdr("viewer")).status_code == 422
    # the scope itself is never cached
    assert "max-age" not in env.c.get("/api/policy/aws/scope", headers=hdr("viewer")).headers.get("cache-control", "")


def test_catalog_unavailable_is_503():
    from app.aws_catalog import Catalog
    from app.aws_reference import TTLCache

    def down():
        raise OSError("no route to host")
    e = Env()
    e.policy.aws_catalog = Catalog(TTLCache("ref", down, background=False), TTLCache("roles", down, background=False),
                                   ROLE_DEFS, {}, "us-east-1", BASELINE)
    r = e.c.get("/api/policy/aws/catalog", headers=hdr("viewer"))
    assert r.status_code == 503 and "Service Authorization Reference" in r.json()["detail"]


def test_preview_matches_what_the_sidecar_sends(env):
    r = env.c.post("/api/policy/aws/preview", json={"role": "read-only", "actions": ["s3:ListAllMyBuckets", "sts:GetCallerIdentity",
                                                                "s3:ListAllMyBuckets"]}, headers=hdr("viewer"))
    assert r.status_code == 200
    p = r.json()
    assert p["actions"] == ["s3:ListAllMyBuckets", "sts:GetCallerIdentity"] and p["services"] == ["s3", "sts"]
    assert p["allowed_services"] == ["s3", "sts"]
    assert p["policy"]["Statement"][0] == BASELINE
    # the sidecar's jq: {Version, Statement: [$base] + .statements}, compact
    assert p["policy_json"] == json.dumps(p["policy"], separators=(",", ":")) and p["chars"] == len(p["policy_json"])
    assert p["fits"] and p["limit"] == 2048
    r = env.c.post("/api/policy/aws/preview", json={"role": "read-only", "actions": ["secretsmanager:GetSecretValue"]}, headers=hdr("viewer"))
    assert r.status_code == 422 and "never enabled here" in r.json()["detail"]
    r = env.c.post("/api/policy/aws/preview", json={"role": "read-only", "actions": ["s3:PutObject"]}, headers=hdr("viewer"))
    assert r.status_code == 422 and "outside the read-only role" in r.json()["detail"]
    r = env.c.post("/api/policy/aws/preview", json={"role": "root", "actions": []}, headers=hdr("viewer"))
    assert r.status_code == 422 and "role must be one of" in r.json()["detail"]


def test_apply_scope_keeps_proxy_in_step(env):
    r = env.c.put("/api/policy/aws/scope", json={"role": "read-only", "actions": ["sts:GetCallerIdentity", "s3:ListAllMyBuckets"]},
                  headers=hdr("admin"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["scope_version"] == 2 and out["config_version"] == 2 and not out["unchanged"]
    scope = env.s3.json("aws-scope.json")
    assert scope["statements"] == [{"Sid": "Scope", "Effect": "Allow", "Action": ["s3:ListAllMyBuckets", "sts:GetCallerIdentity"],
                                    "Resource": "*"}]
    assert scope["updated_by"] == "admin@example.com" and scope["role"] == "read-only" and scope["session_policy"] is True
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    assert doc.allowed_services() == ["s3", "sts"]
    aws_hosts = [h for h in doc.hosts() if h.endswith("amazonaws.com")]
    assert set(aws_hosts) == {"*.sts.us-east-1.amazonaws.com", "sts.amazonaws.com", "*.s3.amazonaws.com",
                              "*.s3-control.us-east-1.amazonaws.com", "*.s3.us-east-1.amazonaws.com"}
    assert "ecs.us-east-1.amazonaws.com" not in doc.hosts() and "httpbin.org" in doc.hosts()
    assert 'allowed_services: ["s3", "sts"]' in env.s3.text("iron-proxy.yaml")
    # the same actions again: nothing written
    n = len(env.s3.puts)
    again = env.c.put("/api/policy/aws/scope", json={"role": "read-only", "actions": ["s3:ListAllMyBuckets", "sts:GetCallerIdentity"]},
                      headers=hdr("admin")).json()
    assert again["unchanged"] is True and len(env.s3.puts) == n
    got = env.c.get("/api/policy/aws/scope", headers=hdr("viewer")).json()
    assert got["version"] == 2 and got["services"] == ["s3", "sts"] and got["role"] == "read-only" and got["session_policy"] is True


def test_empty_scope_allows_no_service(env):
    r = env.c.put("/api/policy/aws/scope", json={"role": "read-only", "actions": []}, headers=hdr("admin"))
    assert r.status_code == 200
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    # iron-proxy 0.55.0 reads an empty list as "every service"
    assert doc.allowed_services() == ["none"]
    assert not [h for h in doc.hosts() if h.endswith("amazonaws.com")]
    assert env.s3.json("aws-scope.json")["statements"] == []


def test_scope_over_the_sts_limit(env, monkeypatch):
    monkeypatch.setattr(cat, "POLICY_CHARS", 300)
    r = env.c.put("/api/policy/aws/scope", json={"role": "read-only", "actions": ["dynamodb:Describe*", "s3:Get*", "iam:List*"]}, headers=hdr("admin"))
    assert r.status_code == 422 and "STS allows" in r.json()["detail"]


# ---------------------------------------------------------------- status

def test_status_tracks_the_sidecar(env):
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["applied"] is False and "hasn't reported" in st["error"]
    env.sidecar_applied()
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["applied"] is True and st["scope"]["session_name"] == "ws-scope-v1"
    assert (st["scope"]["session_token_utilization"], st["scope"]["session_token_size"],
            st["scope"]["packed_policy_size"]) == (31, 1290, 9)
    t0 = time.time()
    env.c.post("/api/policy/allowlist", json={"host": "example.com"}, headers=hdr("admin"))
    env.policy._status_cache = None
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["applied"] is False and st["pending"] is True and st["target"]["config_version"] == 2
    env.sidecar_applied(at=t0 + 2.0)
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["applied"] is True and st["config"]["version"] == 2 and 1.5 < st["latency_s"] < 2.5
    assert st["sidecar"]["stale"] is False


def test_status_reports_a_rejected_config(env):
    env.sidecar_applied()
    env.c.post("/api/policy/allowlist", json={"host": "example.com"}, headers=hdr("admin"))
    s = env.s3.json("status.json")
    s["config"].update(synced_version=2, rejected_version=2)
    s["reload"].update(ok=False, http_status=422, error="parsing config: yaml: bad")
    env.s3._set("status.json", json.dumps(s).encode())
    env.policy._status_cache = None
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["applied"] is False and st["pending"] is False and "rejected config v2" in st["error"]


def test_status_goes_stale(env):
    env.sidecar_applied()
    s = env.s3.json("status.json")
    s["updated_at"] = now_iso(time.time() - 300)
    env.s3._set("status.json", json.dumps(s).encode())
    env.policy._status_cache = None
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["sidecar"]["stale"] is True and "90 s" in st["error"]


# ---------------------------------------------------------------- roles, the session-policy switch, the guard

def test_no_session_policy_writes_explicit_lists_without_secrets_manager(env):
    r = env.c.put("/api/policy/aws/scope", json={"role": "builder", "session_policy": False,
                                                 "actions": ["s3:ListAllMyBuckets"]}, headers=hdr("admin"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["preview"]["policy"] is None and out["preview"]["session_policy"] is False
    scope = env.s3.json("aws-scope.json")
    assert (scope["role"], scope["session_policy"], scope["statements"]) == ("builder", False, [])
    assert scope["actions"] == ["s3:ListAllMyBuckets"]          # kept for switching the policy back on
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    services = doc.allowed_services()
    assert len(services) > 10 and "secretsmanager" not in services and "none" not in services
    aws_hosts = [h for h in doc.hosts() if h.endswith("amazonaws.com")]
    assert aws_hosts and not [h for h in aws_hosts if "secretsmanager" in h]
    assert "*.amazonaws.com" not in doc.hosts()
    assert env.c.get("/api/policy/aws/scope", headers=hdr("viewer")).json()["role"] == "builder"
    change = env.changes.recent()[0]
    assert change["kind"] == "aws.scope" and "builder, no session policy" in change["summary"]


def test_switching_role_or_policy_is_a_change(env):
    body = {"role": "read-only", "session_policy": True, "actions": ["s3:ListAllMyBuckets"]}
    assert env.c.put("/api/policy/aws/scope", json=body, headers=hdr("admin")).json()["unchanged"] is False
    assert env.c.put("/api/policy/aws/scope", json=body, headers=hdr("admin")).json()["unchanged"] is True
    assert env.c.put("/api/policy/aws/scope", json={**body, "role": "builder"}, headers=hdr("admin")).json()["unchanged"] is False
    assert env.c.put("/api/policy/aws/scope", json={**body, "role": "builder", "session_policy": False},
                     headers=hdr("admin")).json()["unchanged"] is False
    # an action the new role doesn't allow is refused only when the policy is on
    r = env.c.put("/api/policy/aws/scope", json={"role": "data-reader", "session_policy": True,
                                                 "actions": ["s3:ListAllMyBuckets"]}, headers=hdr("admin"))
    assert r.status_code == 422 and "outside the data-reader role" in r.json()["detail"]


def test_config_writer_refuses_secrets_manager_whatever_it_is_given(env):
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    doc.set_allowed_services(["s3", "secretsmanager"])
    doc.set_aws_hosts(["*.s3.amazonaws.com", "*.secretsmanager.us-east-1.amazonaws.com", "*.us-east-1.amazonaws.com"])
    assert doc.allowed_services() == ["s3"]
    assert [h for h in doc.hosts() if h.endswith("amazonaws.com")] == ["*.s3.amazonaws.com"]
    doc.set_allowed_services(["secretsmanager"])
    assert doc.allowed_services() == ["none"]


def test_older_scope_without_role_reads_as_default_role(env):
    st = env.c.get("/api/policy/aws/scope", headers=hdr("viewer")).json()
    assert st["role"] == "view-only"            # the fixture's scope predates roles; it had no statements
    assert st["session_policy"] is False


def test_catalog_lists_roles_and_follows_role(env):
    body = env.c.get("/api/policy/aws/catalog?role=data-reader", headers=hdr("viewer")).json()
    assert body["role"] == "data-reader" and {r["id"] for r in body["roles"]} == set(ROLE_DEFS)
    assert next(r for r in body["roles"] if r["id"] == "view-only")["default"] is True
    s3 = env.c.get("/api/policy/aws/services/s3?role=data-reader", headers=hdr("viewer")).json()
    get = next(a for a in s3["actions"] if a["action"] == "s3:GetObject")
    assert get["allowed"] and get["limited"]
    assert env.c.get("/api/policy/aws/catalog?role=root", headers=hdr("viewer")).status_code == 422


def test_part2s_seeded_scope_gets_its_lists_once(env):
    env.s3._set("aws-scope.json", json.dumps({"version": 1, "updated_by": "terraform", "role": "view-only",
                                              "session_policy": False, "actions": [], "statements": [], "seed": True}).encode())
    assert env.policy.seed_scope() is True
    doc = ConfigDoc(env.s3.text("iron-proxy.yaml"))
    assert len(doc.allowed_services()) > 10 and "secretsmanager" not in doc.allowed_services()
    sc = env.s3.json("aws-scope.json")
    assert "seed" not in sc and sc["version"] == 2 and sc["updated_by"] == "workspace (seed)"
    assert env.policy.seed_scope() is False


def test_status_reports_a_config_the_sidecar_refused(env):
    env.sidecar_applied()
    env.c.post("/api/policy/allowlist", json={"host": "example.com"}, headers=hdr("admin"))
    s = env.s3.json("status.json")
    s["config"].update(synced_version=2, rejected_version=2, refused_reason="allowlists a Secrets Manager host")
    env.s3._set("status.json", json.dumps(s).encode())
    env.policy._status_cache = None
    st = env.c.get("/api/policy/status", headers=hdr("viewer")).json()
    assert st["applied"] is False and "sidecar refused config v2" in st["error"]
