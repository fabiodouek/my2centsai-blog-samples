"""Sandbox options on create: size (one image per size), the full idle policy, lifetime in minutes and extra
environment variables; the quota summed from real sizes; /api/config's options; auto-resume off in the terminal."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import sandbox_env
from app.config import Settings, load
from app.events import Hub
from app.main import create_app
from app.microvm import MAX_PAYLOAD
from app.sizes import size_of_arn
from app.store import MemoryStore
from tests.fakes import Connector, FakeBrowser, FakeControl, FakeMicroVMs, FakeUpstream, FakeVerifier
from tests.test_terminal import finish, make, start

ORIGIN = {"X-Origin-Verify": "s3cret"}
IMG = "arn:aws:lambda:us-east-1:111122223333:microvm-image:my2cents-ws-sandbox"
IMAGES = {"small": IMG + "-1g", "medium": IMG, "large": IMG + "-4g", "xlarge": IMG + "-8g"}
SECRET_LOOKING = "s3cr3t-VALUE-never-echoed"


def hdr(sub="alice"):
    return {**ORIGIN, "Authorization": f"Bearer tok-{sub}"}


class Env:
    def __init__(self, **kw):
        base = dict(origin_secret="s3cret", user_pool_id="p", client_id="c", table_name="t", image_arn=IMG,
                    image_arns=dict(IMAGES), connector_arn="arn:conn",
                    placeholders={"anthropic": "sk-ant-placeholder", "github": "ghp_placeholder", "demo": "demo-ph"})
        base.update(kw)
        self.s = Settings(**base)
        self.mv, self.store = FakeMicroVMs(), MemoryStore()
        self.c = TestClient(create_app(self.s, microvms=self.mv, store=self.store, verifier=FakeVerifier(), hub=Hub(None)))

    def create(self, **body):
        return self.c.post("/api/sandboxes", json={"name": "s1", **body}, headers=hdr())


@pytest.fixture
def env():
    return Env()


# ---------------------------------------------------------------- sizes

def test_default_is_medium_on_the_original_image(env):
    r = env.create()
    assert r.status_code == 201, r.text
    v = r.json()
    assert env.mv.runs[0]["image_arn"] == IMG
    assert (v["size"], v["size_label"], v["memory_gb"], v["vcpu"], v["usd_per_hour"]) == ("medium", "Medium", 2, 1, 0.1261)
    assert (v["egress"], v["auto_resume"], v["suspended_duration_s"], v["max_duration_s"]) == ("proxy", True, 28800, 28800)


@pytest.mark.parametrize("size,arn,gb,vcpu,usd", [("small", IMAGES["small"], 1, 0.5, 0.0631),
                                                  ("large", IMAGES["large"], 4, 2, 0.2522),
                                                  ("xlarge", IMAGES["xlarge"], 8, 4, 0.5044)])
def test_each_size_runs_its_own_image(env, size, arn, gb, vcpu, usd):
    v = env.create(size=size).json()
    assert env.mv.runs[0]["image_arn"] == arn
    assert (v["size"], v["memory_gb"], v["vcpu"], v["usd_per_hour"]) == (size, gb, vcpu, usd)
    assert env.store.get("alice", v["id"])["memory_mib"] == gb * 1024
    # the list and vitals say the same
    assert env.c.get("/api/sandboxes", headers=hdr()).json()[0]["size"] == size
    assert env.c.get(f"/api/sandboxes/{v['id']}/vitals", headers=hdr()).json()["memory_gb"] == gb


def test_unknown_or_unbuilt_size(env):
    assert env.create(size="huge").status_code == 422
    env.mv.missing_images.add(IMAGES["large"])
    r = env.create(size="large")
    assert r.status_code == 409 and "Large isn't available" in r.json()["detail"] and "isn't built" in r.json()["detail"]
    assert env.mv.runs == []
    e = Env(image_arns={})          # only IMAGE_ARN: medium works, the rest have no image
    assert e.create().status_code == 201
    assert e.create(size="small").status_code == 409


def test_quota_message_quotes_aws_names_the_size_and_gives_the_estimate(env):
    env.create(size="small")
    env.mv.quota_full = True
    r = env.create(size="xlarge")
    d = r.json()["detail"]
    assert r.status_code == 409 and len(env.mv.runs) == 2            # AWS was asked
    assert d.startswith("AWS refused X-Large (8 GB) with ServiceQuotaExceededException: "
                        "“Fake AWS: the MicroVM memory quota for this account is exhausted”.")
    assert "Our estimate: 1 of 8 GB of MicroVM memory in use in the account, 7 GB free" in d


def test_no_quota_pre_check_aws_decides(env):
    """Our estimate never blocks: with the quota estimated full, every built size is still offered and create still
    calls RunMicrovm (AWS's behaviour may change; here the fake accepts)."""
    env.create(size="xlarge")
    q = env.c.get("/api/quota", headers=hdr()).json()
    assert q["free_gb"] == 0
    sizes = {x["id"]: x for x in env.c.get("/api/config", headers=hdr()).json()["sizes"]}
    assert all(x["available"] for x in sizes.values())
    for size in ("small", "large", "xlarge"):
        r = env.create(size=size)
        assert r.status_code == 201, r.text
    assert [run["image_arn"] for run in env.mv.runs] == [IMAGES["xlarge"], IMAGES["small"], IMAGES["large"], IMAGES["xlarge"]]


def test_resume_refused_by_aws_says_so(env):
    v = env.create(size="large").json()
    env.c.post(f"/api/sandboxes/{v['id']}/suspend", headers=hdr())
    env.mv.quota_full = True
    r = env.c.post(f"/api/sandboxes/{v['id']}/resume", headers=hdr())
    d = r.json()["detail"]
    assert r.status_code == 409 and d.startswith("AWS refused to resume it with ServiceQuotaExceededException: “Fake AWS")
    assert "Our estimate: 4 of 8 GB" in d


def test_quota_sums_real_sizes(env):
    env.create(size="large")
    env.create(size="small")
    env.mv.vms["someone-else"] = {"microvmId": "someone-else", "state": "SUSPENDED", "imageArn": IMAGES["medium"] + ":2.0"}
    env.mv.vms["foreign"] = {"microvmId": "foreign", "state": "RUNNING", "imageArn": "arn:aws:lambda:us-east-1:1:microvm-image:other"}
    q = env.c.get("/api/quota", headers=hdr()).json()
    assert (q["user_gb"], q["used_gb"], q["other_gb"], q["free_gb"]) == (5, 9, 4, 0)
    assert q["other_unknown_size"] == 1          # the foreign image counts as 2 GB
    assert sorted(x["gb"] for x in q["segments"] if x["mine"]) == [1, 4]
    assert sorted(x["gb"] for x in q["segments"] if not x["mine"]) == [2, 2]


def test_size_of_arn_ignores_versions():
    assert size_of_arn(IMAGES, IMAGES["large"] + ":3.0") == "large"
    assert size_of_arn(IMAGES, IMG) == "medium" and size_of_arn(IMAGES, IMG + "-1g") == "small"
    assert size_of_arn(IMAGES, "arn:aws:lambda:us-east-1:1:microvm-image:other") is None and size_of_arn(IMAGES, None) is None


def test_image_arns_json_is_checked():
    s = load({"DEV_AUTH": "1", "IMAGE_ARN": IMG, "IMAGE_ARNS_JSON": json.dumps({"small": IMAGES["small"]})})
    assert s.size_images == {"small": IMAGES["small"], "medium": IMG}
    with pytest.raises(Exception, match="unknown sizes: huge"):
        load({"DEV_AUTH": "1", "IMAGE_ARNS_JSON": json.dumps({"huge": "x"})})


# ---------------------------------------------------------------- idle policy and lifetime

def test_lifetime_in_minutes(env):
    v = env.create(max_minutes=90, idle_minutes=10).json()
    assert v["max_duration_s"] == 5400 and env.mv.runs[0]["max_duration_s"] == 5400
    assert env.create(max_minutes=1).status_code == 422        # idle (15) isn't shorter than 1 minute
    assert env.create(max_minutes=2, idle_minutes=1).status_code == 201
    assert env.create(max_minutes=481).status_code == 422
    assert env.create(max_minutes=0).status_code == 422
    assert env.create(max_minutes=30, idle_minutes=30).json()["detail"].startswith("the idle time")
    assert env.create(max_hours=2).json()["max_duration_s"] == 7200     # the older field still works


def test_suspended_duration_and_auto_resume(env):
    v = env.create(max_minutes=60, suspended_minutes=0, auto_resume=False).json()
    run = env.mv.runs[-1]
    assert (run["suspended_duration_s"], run["auto_resume"]) == (0, False)
    assert (v["suspended_duration_s"], v["auto_resume"]) == (0, False)
    v = env.create(max_minutes=60, suspended_minutes=5).json()
    assert env.mv.runs[-1]["suspended_duration_s"] == 300 and v["suspended_duration_s"] == 300
    v = env.create(max_minutes=60).json()                       # default: until the lifetime ends
    assert env.mv.runs[-1]["suspended_duration_s"] is None and v["suspended_duration_s"] == 3600
    assert env.create(max_minutes=60, suspended_minutes=61).status_code == 422
    assert env.create(suspended_minutes=-1).status_code == 422


def test_idle_policy_termination_is_named(env):
    from datetime import timedelta
    v = env.create(max_minutes=60, idle_minutes=1, suspended_minutes=1).json()
    d = env.mv.vms[v["id"]]
    d.update(state="TERMINATED", stateReason="MicroVM exceeded maximum lifetime.",
             terminatedAt=d["startedAt"] + timedelta(minutes=3))
    got = env.c.get("/api/sandboxes", headers=hdr()).json()[0]
    assert got["state_reason"] == "Terminated by its idle policy after 1 min suspended"
    d["terminatedAt"] = d["startedAt"] + timedelta(minutes=60)      # the real lifetime end keeps Lambda's text
    assert env.c.get("/api/sandboxes", headers=hdr()).json()[0]["state_reason"] == "MicroVM exceeded maximum lifetime."


@pytest.mark.parametrize("conn", [None, "", "  ", "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"])
def test_never_runs_without_part2s_connector(conn):
    """An empty egressNetworkConnectors isn't offline: Lambda attaches INTERNET_EGRESS (direct internet, no proxy)."""
    from app.microvm import NoEgressConnector, run_params
    s = Settings(image_arn=IMG, connector_arn=conn)
    with pytest.raises(NoEgressConnector):
        run_params(s, idle_minutes=5, max_duration_s=600, client_token="t", payload="{}")
    e = Env(connector_arn=conn)
    r = e.create()
    assert r.status_code == 503 and "INTERNET_EGRESS" in r.json()["detail"] and e.mv.runs == []


def test_run_params_always_name_the_connector():
    from app.microvm import run_params
    p = run_params(Settings(image_arn=IMG, connector_arn="arn:conn"), idle_minutes=5, max_duration_s=600,
                   client_token="t", payload="{}")
    assert p["egressNetworkConnectors"] == ["arn:conn"]


def test_run_params_carry_the_idle_policy():
    from app.microvm import run_params
    s = Settings(image_arn=IMG, connector_arn="arn:conn")
    p = run_params(s, idle_minutes=5, max_duration_s=600, client_token="t", payload="{}", image_arn=IMAGES["small"],
                   suspended_duration_s=0, auto_resume=False)
    assert p["imageIdentifier"] == IMAGES["small"] and p["egressNetworkConnectors"] == ["arn:conn"]
    assert p["idlePolicy"] == {"autoResumeEnabled": False, "maxIdleDurationSeconds": 300, "suspendedDurationSeconds": 0}
    p = run_params(s, idle_minutes=5, max_duration_s=600, client_token="t", payload="{}")
    assert p["imageIdentifier"] == IMG and p["idlePolicy"]["suspendedDurationSeconds"] == 600


# ---------------------------------------------------------------- environment variables

def test_env_goes_to_the_payload_and_names_to_the_view(env):
    v = env.create(env={"FOO": "bar", "_X2": "a b-c.d/e:f=g'h", "EMPTY": ""}).json()
    payload = json.loads(env.mv.runs[0]["payload"])["env"]
    assert (payload["FOO"], payload["_X2"], payload["EMPTY"]) == ("bar", "a b-c.d/e:f=g'h", "")
    assert payload["SANDBOX_NAME"] == "s1" and payload["ANTHROPIC_API_KEY"] == "sk-ant-placeholder"
    assert v["env_names"] == ["FOO", "_X2", "EMPTY"]
    assert "env" not in v and "bar" not in json.dumps(v)          # the API returns names, not values
    assert env.store.get("alice", v["id"])["env"]["FOO"] == "bar"  # stored, as the form says


@pytest.mark.parametrize("env_vars,needle", [
    ({"1BAD": "x"}, "isn't a valid name"), ({"A-B": "x"}, "isn't a valid name"),
    ({"ANTHROPIC_API_KEY": SECRET_LOOKING}, "ANTHROPIC_API_KEY is reserved"),
    ({"AWS_PROFILE": SECRET_LOOKING}, "AWS_PROFILE is reserved"), ({"aws_x": "v"}, "aws_x is reserved"),
    ({"SANDBOX_NAME": "x"}, "SANDBOX_NAME is reserved"), ({"SSL_CERT_FILE": "x"}, "SSL_CERT_FILE is reserved"),
    ({"NODE_EXTRA_CA_CERTS": "x"}, "reserved"), ({"PATH": "x"}, "PATH is reserved"), ({"LD_PRELOAD": "x"}, "reserved"),
    ({"GH_TOKEN": "x"}, "reserved"), ({"AWS_SDK_UA_APP_ID": "x"}, "reserved"),
    ({"X": "$(id)" + SECRET_LOOKING}, "no $"), ({"X": "`id`"}, "no $"), ({"X": 'a"b'}, "no $"), ({"X": "a\\b"}, "no $"),
    ({"X": "line1\nline2" + SECRET_LOOKING}, "printable ASCII"), ({"X": "café"}, "printable ASCII"),
    ({"X": "x" * 2049}, "over 2048"),
])
def test_env_is_validated_and_values_never_echoed(env, env_vars, needle):
    r = env.create(env=env_vars)
    assert r.status_code == 422, r.text
    assert needle in json.dumps(r.json())
    assert SECRET_LOOKING not in r.text
    assert env.mv.runs == []


def test_env_too_many_and_custom_credential_names():
    e = Env()
    r = e.create(env={f"V{i}": "x" for i in range(sandbox_env.MAX_VARS + 1)})
    assert r.status_code == 422
    reserved = sandbox_env.reserved_names(e.s.placeholders, "us-east-1", {"TEST_KEY": "m2c-ph-test"})
    with pytest.raises(sandbox_env.EnvError, match="TEST_KEY is reserved"):
        sandbox_env.check({"TEST_KEY": "x"}, reserved)


def test_env_payload_limit(env):
    big = {f"V{i}": "x" * 400 for i in range(10)}    # about 4 KB on top of the base payload
    r = env.create(env=big)
    assert r.status_code == 409 and "limit 4,096" in r.json()["detail"] and "your variables take" in r.json()["detail"]
    assert env.mv.runs == []


def test_entry_bytes_matches_the_payload():
    from app.microvm import build_run_payload
    base = build_run_payload({}, "us-east-1", {"SANDBOX_NAME": "x"})
    more = build_run_payload({}, "us-east-1", {"SANDBOX_NAME": "x", "ABC": "hello world"})
    size = lambda p: len(json.dumps(p, separators=(",", ":")).encode())  # noqa: E731
    assert size(more) - size(base) == sandbox_env.entry_bytes("ABC", "hello world")


# ---------------------------------------------------------------- /api/config

def test_config_lists_sizes_limits_and_reserved_names(env):
    env.mv.missing_images.add(IMAGES["xlarge"])
    c = env.c.get("/api/config", headers=hdr()).json()
    sizes = {x["id"]: x for x in c["sizes"]}
    assert list(sizes) == ["small", "medium", "large", "xlarge"] and c["default_size"] == "medium"
    assert [sizes[k]["usd_per_hour"] for k in sizes] == [0.0631, 0.1261, 0.2522, 0.5044]
    assert sizes["medium"]["default"] is True and sizes["medium"]["available"] is True
    assert sizes["xlarge"]["available"] is False and "isn't built" in sizes["xlarge"]["unavailable_reason"]
    assert sizes["xlarge"]["note"].startswith("Needs all 8 GB of this account's MicroVM memory quota: AWS will refuse")
    assert sizes["large"]["note"] is None
    assert c["lifetime"] == {"min_minutes": 1, "max_minutes": 480, "default_minutes": 480}
    assert c["suspended"]["min_minutes"] == 0 and c["idle"]["min_minutes"] == 1
    assert {"ANTHROPIC_API_KEY", "SANDBOX_NAME", "AWS_ACCESS_KEY_ID", "SSL_CERT_FILE", "PATH"} <= set(c["env"]["reserved"])
    assert c["env"]["reserved_prefixes"][0] == "AWS_" and c["run_payload"]["limit"] == MAX_PAYLOAD
    assert 0 < c["run_payload"]["used_bytes"] < MAX_PAYLOAD


# ---------------------------------------------------------------- auto-resume off in the terminal

async def test_terminal_does_not_wake_a_sandbox_with_auto_resume_off():
    b, cp, up = FakeBrowser(), FakeControl("SUSPENDED"), FakeUpstream()
    task = await start(make(b, cp, Connector(up), auto_resume=False))
    await b.wait_for(lambda: "suspended" in b.states())
    await asyncio.sleep(0.05)
    assert cp.resumed == 0
    st = [x for x in b.statuses() if x["state"] == "suspended"][-1]
    assert st["detail"].startswith("Suspended: press Resume") and st["auto_resume"] is False
    cp.st = "RUNNING"            # someone pressed Resume
    await b.wait_for(lambda: "attached" in b.states())
    assert cp.resumed == 0
    b.leave()
    await finish(task)


def test_terminal_route_passes_auto_resume(env):
    v = env.create(auto_resume=False).json()
    env.mv.vms[v["id"]]["state"] = "SUSPENDED"
    from starlette.websockets import WebSocketDisconnect
    with env.c.websocket_connect(f"/ws/terminal/{v['id']}", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "tok-alice"}))
        seen = []
        try:
            for _ in range(4):
                m = json.loads(ws.receive_text())
                seen.append(m)
                if m.get("state") == "suspended":
                    break
        except WebSocketDisconnect:
            pass
    assert any(m.get("state") == "suspended" and m.get("auto_resume") is False for m in seen), seen
    assert ("RUNNING", v["id"]) not in env.mv.calls        # never resumed
