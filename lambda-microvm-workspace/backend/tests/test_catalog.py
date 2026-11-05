"""The AWS access catalog from a recorded slice of the Service Authorization Reference: access levels, the
ReadOnlyAccess ceiling, the IAM prefix -> signing name / host mapping, search, wildcards, validation, and the
one-hour cache with stale-on-error. No network."""

import json

import pytest

from app import aws_catalog as cat
from app.aws_reference import Ceiling, TTLCache, Unavailable, access_level, boto3_clients
from app.aws_roles import RoleEvaluator
from tests.catalog_fixture import DATA, FakeIAM, fixture_get, make_catalog

BASELINE = {"Sid": "ProxySecrets", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": ["arn:x"]}


@pytest.fixture(scope="module")
def catalog():
    return make_catalog(BASELINE)


# ---------------------------------------------------------------- levels and the join

@pytest.mark.parametrize("props,level", [
    ({}, "Read"), ({"IsList": True}, "List"), ({"IsWrite": True}, "Write"),
    ({"IsWrite": True, "IsPermissionManagement": True}, "Permissions"),
    ({"IsWrite": True, "IsTaggingOnly": True}, "Tagging")])
def test_access_level(props, level):
    assert access_level({"Properties": props}) == level


def test_operations_join_uses_the_operations_own_prefix():
    s3 = json.loads((DATA / "s3.json").read_text())
    assert boto3_clients(s3) == ["s3", "s3control"]
    # an operation listed in sso's file but authorized first by another prefix doesn't make its client sso's
    doc = {"Name": "sso", "Operations": [
        {"Name": "AssociateAccessGrantsIdentityCenter", "SDK": [{"Name": "s3control", "Package": "Boto3"}],
         "AuthorizedActions": [{"Name": "AssociateAccessGrantsIdentityCenter", "Service": "s3"},
                               {"Name": "CreateApplication", "Service": "sso"}]},
        {"Name": "CreateApplication", "SDK": [{"Name": "sso-admin", "Package": "Boto3"}],
         "AuthorizedActions": [{"Name": "CreateApplication", "Service": "sso"}]}]}
    assert boto3_clients(doc) == ["sso-admin"]


# ---------------------------------------------------------------- mapping: the tricky ones

@pytest.mark.parametrize("sid,signing,hosts", [
    ("cloudwatch", ["cloudwatch", "monitoring"], ["*.monitoring.us-east-1.amazonaws.com"]),
    ("es", ["es"], ["*.es.us-east-1.amazonaws.com"]),
    ("elasticloadbalancing", ["elasticloadbalancing"], ["*.elasticloadbalancing.us-east-1.amazonaws.com"]),
    ("states", ["states"], ["*.states.us-east-1.amazonaws.com"]),
    ("ses", ["ses"], ["*.email.us-east-1.amazonaws.com"]),
    ("s3", ["s3"], ["*.s3.amazonaws.com", "*.s3.us-east-1.amazonaws.com", "*.s3-control.us-east-1.amazonaws.com"]),
    ("sts", ["sts"], ["*.sts.us-east-1.amazonaws.com", "sts.amazonaws.com"]),
    ("iam", ["iam"], ["*.iam.amazonaws.com"]),
    ("ecr", ["ecr"], ["*.api.ecr.us-east-1.amazonaws.com"]),
    ("aws-marketplace", ["aws-marketplace"], ["*.catalog.marketplace.us-east-1.amazonaws.com"]),
    ("pricing", ["pricing"], ["*.api.pricing.us-east-1.amazonaws.com"]),
    ("elasticmapreduce", ["elasticmapreduce"], ["*.elasticmapreduce.us-east-1.amazonaws.com"]),
    ("dynamodb", ["dynamodb"], ["*.dynamodb.us-east-1.amazonaws.com", "*.streams.dynamodb.us-east-1.amazonaws.com"]),
    ("lambda", ["lambda"], ["*.lambda.us-east-1.amazonaws.com"]),
    ("sso", ["sso"], ["*.sso.us-east-1.amazonaws.com"]),
    # no SDK entries in its reference file: found by endpoint prefix (apigatewaymanagementapi), not by signing name
    # (connectparticipant also signs as execute-api, but isn't governed by these actions)
    ("execute-api", ["execute-api"], ["*.execute-api.us-east-1.amazonaws.com"]),
])
def test_mapping(catalog, sid, signing, hosts):
    s = catalog.service(sid)
    assert s["enabled"] and s["reason"] is None
    assert s["signing_names"] == signing
    assert set(hosts) <= set(s["hosts"])


def test_fallback_join_prefers_name_and_endpoint_prefix(catalog):
    assert catalog.service("execute-api")["hosts"] == ["*.execute-api.us-east-1.amazonaws.com"]
    clients, how = catalog._clients("execute-api", [])
    assert (clients, how) == (["apigatewaymanagementapi"], "endpoint prefix")
    assert catalog._clients("opensearch", []) == (["opensearch"], "name")
    assert catalog._clients("neptune-db", []) == (["neptunedata"], "endpoint prefix")


def test_only_amazonaws_endpoints_can_be_enabled(catalog):
    # datazone is served from datazone.us-east-1.api.aws: iron-proxy's aws_auth rule covers *.amazonaws.com only
    assert catalog._map("datazone", ["datazone"]) == ([], [], cat.OUTSIDE_REASON)
    # eks keeps its own endpoint and drops eks-auth's api.aws one
    signing, hosts, reason = catalog._map("eks", ["eks", "eks-auth"])
    assert reason is None and hosts == ["*.eks.us-east-1.amazonaws.com"] and signing == ["eks"]


@pytest.mark.parametrize("sid", ["s3-object-lambda", "cloudshell", "a4b"])
def test_unmapped_services_are_listed_but_disabled(catalog, sid):
    s = catalog.service(sid)
    assert s["enabled"] is False and s["reason"] == cat.UNMAPPED_REASON and s["hosts"] == []
    assert s["actions"] and all(not a["allowed"] and a["reason"] == cat.UNMAPPED_REASON for a in s["actions"])


# ---------------------------------------------------------------- roles: ceilings and blocked actions

OUT = "outside the {}"


@pytest.mark.parametrize("role,action,allowed,limited,reason", [
    ("view-only", "s3:ListAllMyBuckets", True, False, None),
    ("view-only", "dynamodb:ListTables", True, False, None),
    ("view-only", "s3:GetObject", False, False, "outside the view-only role"),
    ("view-only", "dynamodb:Scan", False, False, "outside the view-only role"),
    ("read-only", "s3:GetObject", True, False, None),
    ("read-only", "sts:GetCallerIdentity", True, False, None),
    ("read-only", "ssm:DescribeParameters", True, False, None),
    ("read-only", "s3:PutObject", False, False, "outside the read-only role"),
    ("read-only", "kms:Decrypt", False, False, "outside the read-only role"),
    ("data-reader", "s3:GetObject", True, True, None),
    ("data-reader", "dynamodb:Scan", True, True, None),
    ("data-reader", "s3:ListAllMyBuckets", False, False, "outside the data-reader role"),
    ("data-reader", "lambda:ListFunctions", False, False, "outside the data-reader role"),
    ("builder", "s3:PutObject", True, False, None),
    ("builder", "dynamodb:PutItem", True, False, None),
    ("builder", "sts:AssumeRole", False, False, "denied by the builder role (NoAssumeRole)"),
    ("builder", "iam:CreateRole", False, False, "outside the builder role"),
    # blocked here whatever the role says
    ("builder", "secretsmanager:ListSecrets", False, False, cat.SECRETS_REASON),
    ("read-only", "secretsmanager:GetSecretValue", False, False, cat.SECRETS_REASON),
    ("builder", "ssm:GetParameter", False, False, cat.SSM_REASON),
    ("read-only", "ssm:GetParametersByPath", False, False, cat.SSM_REASON),
])
def test_role_ceilings(catalog, role, action, allowed, limited, reason):
    rec = catalog.snapshot(role)["index"][action.lower()]
    assert (rec["allowed"], rec["limited"]) == (allowed, limited), rec
    if reason:
        assert rec["reason"].startswith(reason), rec["reason"]
    else:
        assert rec["reason"] is None


def test_role_evaluator():
    ev = RoleEvaluator("r", [
        {"Effect": "Allow", "NotAction": ["iam:*", "organizations:*"], "Resource": "*"},             # PowerUserAccess
        {"Effect": "Allow", "Action": ["iam:ListRoles"], "Resource": "*"},
        {"Sid": "Global", "Effect": "Deny", "Action": ["sts:AssumeRole"], "Resource": "*"},
        {"Sid": "Region", "Effect": "Deny", "NotAction": ["iam:*"], "Resource": "*",
         "Condition": {"StringNotEquals": {"aws:RequestedRegion": ["us-east-1"]}}},
        {"Sid": "Stack", "Effect": "Deny", "NotAction": ["s3:Get*"], "Resource": ["arn:aws:s3:::my2cents-*"]},
        {"Sid": "Secrets", "Effect": "Deny", "Action": "secretsmanager:*", "NotResource": ["arn:x"]},
    ])
    assert ev.verdict("ec2:RunInstances") == (True, False, None)       # the Region lock is conditional
    assert ev.verdict("S3:putobject") == (True, False, None)           # the stack Deny names resources
    assert ev.verdict("iam:ListRoles") == (True, False, None)
    assert ev.verdict("iam:CreateRole")[:2] == (False, False)          # NotAction excludes iam
    assert ev.verdict("sts:AssumeRole") == (False, False, "denied by the r role (Global)")
    assert ev.verdict("secretsmanager:ListSecrets")[0] is True         # NotResource: not everywhere
    scoped = RoleEvaluator("d", [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::b/*"},
                                 {"Effect": "Allow", "Action": ["s3:Get*"], "Resource": "*", "Condition": {"Bool": {"x": "y"}}}])
    assert scoped.verdict("s3:GetObject") == (True, True, None)        # resource- or condition-bound only
    assert scoped.verdict("s3:PutObject")[0] is False


def test_unknown_role(catalog):
    with pytest.raises(cat.CatalogError):
        catalog.snapshot("root")
    assert catalog.default_role == "view-only"
    roles = {r["id"]: r for r in catalog.role_list()}
    assert roles["view-only"]["default"] and roles["builder"]["warning"] == cat.BUILDER_WARNING


def test_ceiling_matcher():
    c = Ceiling(["s3:Get*", "ec2:Describe*", "iam:GetRole", "logs:Get?ogEvents", "*:ListTagsForResource"], ["s3:GetObjectAcl"])
    assert c.allows("s3:GetObject") and c.allows("S3:getbucketpolicy") and c.allows("ec2:DescribeVpcs")
    assert c.allows("iam:GetRole") and not c.allows("iam:GetRolePolicy")
    assert c.allows("logs:GetLogEvents") and c.allows("kms:ListTagsForResource")
    assert not c.allows("s3:GetObjectAcl")            # deny wins
    assert not c.allows("s3:PutObject") and not c.allows("ec2:RunInstances")


# ---------------------------------------------------------------- the Secrets Manager guard

def test_open_lists_never_include_secrets_manager(catalog):
    for role in ("view-only", "read-only", "data-reader", "builder"):
        pv = catalog.preview(["s3:ListAllMyBuckets"], role, session_policy=False)
        assert pv["policy"] is None and pv["chars"] == 0 and pv["session_policy"] is False
        # only services the role allows something in; data-reader: its demo bucket and table, and sts:GetCallerIdentity
        assert "secretsmanager" not in pv["allowed_services"]
        assert len(pv["allowed_services"]) > 10 if role != "data-reader" else pv["allowed_services"] == ["dynamodb", "s3", "sts"]
        assert pv["allowed_services"] != [] and "none" not in pv["allowed_services"]
        for ep in cat.secrets_endpoints("us-east-1"):
            assert not any(cat._glob(h, ep) for h in pv["hosts"]), ep
        assert "*.amazonaws.com" not in pv["hosts"] and "*.us-east-1.amazonaws.com" not in pv["hosts"]
        assert pv["actions"] == ["s3:ListAllMyBuckets"]                # kept for when the policy is switched on


def test_guards():
    assert cat.guard_services(["s3", "secretsmanager", "SecretsManager", "sts"]) == ["s3", "sts"]
    assert cat.guard_services(["secretsmanager"]) == [cat.NO_SERVICE]
    assert cat.guard_services([]) == [cat.NO_SERVICE]
    hosts = ["*.s3.amazonaws.com", "*.secretsmanager.us-east-1.amazonaws.com", "*.us-east-1.amazonaws.com",
             "*.amazonaws.com", "secretsmanager-fips.us-east-1.amazonaws.com", "*.sts.us-east-1.amazonaws.com"]
    assert cat.guard_hosts(hosts, "us-east-1") == ["*.s3.amazonaws.com", "*.sts.us-east-1.amazonaws.com"]


@pytest.mark.parametrize("role", ["view-only", "read-only", "data-reader", "builder"])
def test_wildcards_report_how_much_is_effective(catalog, role):
    snap = catalog.snapshot(role)
    for sid, s in snap["services"].items():
        names = [w["action"] for w in s["wildcards"]]
        if sid == "secretsmanager":
            assert f"{sid}:*" not in names and all(not w["pickable"] for w in s["wildcards"])
        elif s["wildcards"]:
            assert names[0] == f"{sid}:*" and s["wildcards"][0]["label"] == "Every action"
        for w in s["wildcards"]:
            verb = w["name"][:-1]
            covered = [a for a in s["actions"] if a["name"].startswith(verb)]
            eff = sum(a["allowed"] for a in covered)
            assert w["covers"] == len(covered)
            assert w["pickable"] == (s["enabled"] and sid != "secretsmanager"), w["action"]
            if w["pickable"]:
                assert w["effective"] == eff and w["tone"] == cat.tone(eff, len(covered))
                assert w["allowed"] == (eff == len(covered))
    if role != "read-only":
        return
    s3 = catalog.service("s3", role)
    assert next(w for w in s3["wildcards"] if w["action"] == "s3:List*")["tone"] == "all"
    assert next(w for w in catalog.service("secretsmanager", role)["wildcards"] if w["action"].endswith("Get*"))["reason"] == cat.SECRETS_REASON
    ssm_get = next(w for w in catalog.service("ssm", role)["wildcards"] if w["action"] == "ssm:Get*")
    assert ssm_get["pickable"] is True and ssm_get["tone"] == "partial" and "denied in the session policy" in ssm_get["reason"]


# ---------------------------------------------------------------- search

def test_search_exact_first_and_usable_first(catalog):
    r = catalog.search("GetObject", role="read-only")
    assert r["actions"][0]["action"] == "s3:GetObject"
    assert all(a["name"] == "GetObject" for a in r["actions"][:2])          # exact name matches lead
    assert {a["action"] for a in r["actions"]} >= {"s3:GetObjectAcl", "s3-object-lambda:GetObject"}
    assert catalog.search("s3:getobject", role="read-only")["actions"][0]["action"] == "s3:GetObject"
    # the default role (view-only) can't read objects: the same hit, not usable
    assert catalog.search("s3:getobject")["actions"][0]["allowed"] is False


def test_search_services_and_levels(catalog):
    r = catalog.search("dynamodb")
    assert r["services"][0]["id"] == "dynamodb" and r["total"] >= 80
    r = catalog.search("Describe", level="List", role="read-only")
    assert r["total"] > 0 and all(a["level"] == "List" and "describe" in a["name"].lower() for a in r["actions"])
    assert r["actions"][0]["allowed"]                                         # usable first
    r = catalog.search("", level="Permissions", limit=5)
    assert r["truncated"] and len(r["actions"]) == 5 and all(a["level"] == "Permissions" for a in r["actions"])
    assert catalog.search("")["total"] == 0
    with pytest.raises(cat.CatalogError):
        catalog.search("x", level="Admin")


def test_listing_is_small_and_has_counts(catalog):
    ls = catalog.listing()
    assert ls["source"]["services"] == 24 and ls["source"]["actions"] == sum(s["actions"] for s in ls["services"])
    assert ls["role"] == "view-only" and [r["id"] for r in ls["roles"]] == ["builder", "data-reader", "read-only", "view-only"]
    assert ls["ceiling"]["role"] == "view-only" and "ViewOnlyAccess (v48)" in ls["ceiling"]["policies"]
    assert "inline: sandbox-guards" in ls["ceiling"]["policies"]
    assert catalog.listing("read-only")["ceiling"]["policies"][0] == "ReadOnlyAccess (v191)"
    s3 = next(s for s in ls["services"] if s["id"] == "s3")
    assert s3["name"] == "S3" and s3["levels"]["Write"] > 0 and 0 < s3["allowed"] < s3["actions"]
    assert all(isinstance(s["actions"], int) for s in ls["services"])     # counts only: actions load per service


# ---------------------------------------------------------------- selection -> policy

def test_preview_maps_services_to_signing_names_and_hosts(catalog):
    pv = catalog.preview(["dynamodb:ListTables", "CLOUDWATCH:listmetrics", "dynamodb:ListTables", "lambda:List*"], "read-only")
    assert pv["actions"] == ["cloudwatch:ListMetrics", "dynamodb:ListTables", "lambda:List*"]
    assert pv["allowed_services"] == ["cloudwatch", "dynamodb", "lambda", "monitoring"]
    assert "*.monitoring.us-east-1.amazonaws.com" in pv["hosts"] and "*.dynamodb.us-east-1.amazonaws.com" in pv["hosts"]
    assert pv["policy"]["Statement"][0] == BASELINE and pv["chars"] == len(pv["policy_json"])
    assert catalog.preview([], "read-only")["allowed_services"] == [cat.NO_SERVICE]


@pytest.mark.parametrize("action,needle", [
    ("secretsmanager:GetSecretValue", "secrets"),
    ("ssm:GetParameter", "SecureString"), ("cloudshell:DescribeEnvironments", "no endpoint mapping"),
    ("secretsmanager:*", "not in the Service Authorization Reference"), ("secretsmanager:List*", "secrets"),
    ("nope:Nothing", "not in the Service Authorization Reference"),
    ("s3:GetNothingHere", "not in the Service Authorization Reference")])
def test_preview_refuses(catalog, action, needle):
    with pytest.raises(cat.CatalogError) as e:
        catalog.preview(["s3:GetObject", action], "read-only")
    assert needle in str(e.value)


# ---------------------------------------------------------------- the cache

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_cache_ttl_and_stale_on_error(tmp_path):
    clock, calls = Clock(), []
    state = {"fail": False}

    def fetch():
        calls.append(clock.t)
        if state["fail"]:
            raise RuntimeError("servicereference unreachable")
        return {"n": len(calls)}

    c = TTLCache("ref", fetch, ttl=3600, disk=tmp_path / "ref.json.gz", clock=clock, background=False)
    assert c.get() == {"n": 1} and c.state()["stale"] is False
    clock.t += 3599
    assert c.get() == {"n": 1} and len(calls) == 1                       # inside the hour: no fetch
    clock.t += 2
    assert c.get() == {"n": 2} and c.state()["age_s"] == 0              # expired: refreshed (inline here)
    state["fail"] = True
    clock.t += 3700
    assert c.get() == {"n": 2}                                           # refresh failed: old copy kept
    st = c.state()
    assert st["stale"] is True and "unreachable" in st["error"] and st["age_s"] == 3700
    # a new process finds the copy on disk and doesn't wait for the network
    c2 = TTLCache("ref", fetch, ttl=3600, disk=tmp_path / "ref.json.gz", clock=clock, background=False)
    assert c2.value == {"n": 2} and c2.state()["age_s"] == 3700


def test_cold_start_failure_is_unavailable():
    c = TTLCache("ref", lambda: (_ for _ in ()).throw(RuntimeError("down")), background=False)
    with pytest.raises(Unavailable):
        c.get()


def test_partial_fetch_fails_the_whole_refresh():
    def flaky(url):
        if url.endswith("/s3.json"):
            raise OSError("reset")
        return fixture_get(url)
    from app.aws_reference import fetch_reference
    with pytest.raises(RuntimeError, match="s3"):
        fetch_reference(get=flaky, workers=2)


def test_expired_copy_is_served_while_a_background_refresh_runs():
    import threading
    clock, gate, calls = Clock(), threading.Event(), []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            gate.wait(5)
        return {"n": len(calls)}

    c = TTLCache("ref", fetch, ttl=10, clock=clock, background=True)
    assert c.get() == {"n": 1}
    clock.t += 11
    assert c.get() == {"n": 1} and c.state()["stale"] is True          # no waiting on the refresh
    assert c.get() == {"n": 1} and len(calls) == 2                      # one refresh, not one per request
    gate.set()
    for _ in range(100):
        if c.value == {"n": 2}:
            break
        threading.Event().wait(0.01)
    assert c.get() == {"n": 2} and c.state()["stale"] is False
