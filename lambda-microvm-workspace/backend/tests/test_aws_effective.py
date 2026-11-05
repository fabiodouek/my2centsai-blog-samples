"""AWS access picks: what can be picked (outside the role too), how much of each pick is effective with the role,
the effective access per service (role ∩ session policy), the SSM Deny for wildcards that cover parameter reads, and
iron-proxy's lists (only services with at least one effective action). Against the recorded catalog slice."""

import json

import pytest

from app import aws_catalog as cat
from tests.catalog_fixture import make_catalog
from tests.test_policy import BASELINE, Env, hdr


@pytest.fixture(scope="module")
def catalog():
    return make_catalog(BASELINE)


def cov(pv, action):
    return next(c for c in pv["coverage"] if c["action"] == action)


def row(pv, sid):
    return next(r for r in pv["effective"] if r["service"] == sid)


# ---------------------------------------------------------------- what can be picked

@pytest.mark.parametrize("role", ["view-only", "read-only", "data-reader", "builder"])
def test_normalize_keeps_outside_role_picks_and_wildcards(catalog, role):
    picks = ["s3:PutObject", "dynamodb:Scan", "S3:*", "ssm:Get*", "lambda:List*", "kms:Describe*", "iam:CreateRole"]
    assert catalog.normalize(picks, role) == sorted(["s3:PutObject", "dynamodb:Scan", "s3:*", "ssm:Get*", "lambda:List*",
                                                     "kms:Describe*", "iam:CreateRole"])


@pytest.mark.parametrize("action,needle", [
    ("secretsmanager:GetSecretValue", "fetches its own secrets"), ("secretsmanager:Get*", "fetches its own secrets"),
    ("secretsmanager:*", "not in the Service Authorization Reference"),        # never offered
    ("ssm:GetParameter", "SecureString"), ("ssm:GetParametersByPath", "SecureString"),
    ("cloudshell:DescribeEnvironments", "no endpoint mapping"), ("cloudshell:*", "no endpoint mapping"),
    ("nope:Nothing", "not in the Service Authorization Reference"), ("s3:Nothing", "not in the Service Authorization")])
def test_normalize_still_refuses(catalog, action, needle):
    for role in ("view-only", "builder"):
        with pytest.raises(cat.CatalogError, match=needle):
            catalog.normalize(["s3:ListAllMyBuckets", action], role)


def test_selection_survives_a_role_switch(catalog):
    picks = ["s3:GetObject", "dynamodb:Scan", "s3:ListAllMyBuckets"]
    out = {r: catalog.preview(picks, r)["actions"] for r in ("view-only", "read-only", "data-reader", "builder")}
    assert len({tuple(v) for v in out.values()}) == 1


def test_get_caller_identity_is_always_effective(catalog):
    rec = catalog.snapshot("view-only")["index"]["sts:getcalleridentity"]
    assert rec["allowed"] and rec["pickable"]
    assert catalog.preview(["sts:GetCallerIdentity"], "data-reader")["allowed_services"] == ["sts"]


# ---------------------------------------------------------------- coverage per pick

def test_coverage_counts_and_tones(catalog):
    snap = catalog.snapshot("view-only")
    s3 = snap["services"]["s3"]
    pv = catalog.preview(["s3:*", "dynamodb:Describe*", "dynamodb:Scan", "s3:ListAllMyBuckets"], "view-only")
    c = cov(pv, "s3:*")
    assert (c["kind"], c["covers"], c["effective"]) == ("wildcard", len(s3["actions"]), s3["allowed"])
    assert c["tone"] == "partial" and c["reason"].startswith(f"{s3['allowed']} of {len(s3['actions'])} effective")
    assert "outside the view-only role" in c["reason"]
    d = cov(pv, "dynamodb:Describe*")
    assert d["tone"] in ("all", "partial") and d["effective"] > 0
    single = cov(pv, "dynamodb:Scan")
    assert (single["kind"], single["covers"], single["effective"], single["tone"]) == ("action", 1, 0, "none")
    assert single["reason"].startswith("no effect with view-only")
    assert cov(pv, "s3:ListAllMyBuckets")["tone"] == "all" and cov(pv, "s3:ListAllMyBuckets")["reason"] is None
    # the same picks with builder: more of s3:* is effective, dynamodb:Scan now counts
    pb = catalog.preview(["s3:*", "dynamodb:Scan"], "builder")
    assert cov(pb, "s3:*")["effective"] > c["effective"] and cov(pb, "dynamodb:Scan")["tone"] == "all"


def test_wildcard_rows_in_the_service_view(catalog):
    s = catalog.service("s3", "view-only")
    every = s["wildcards"][0]
    assert every["action"] == "s3:*" and every["pickable"] and every["tone"] == "partial"
    assert every["covers"] == len(s["actions"]) and every["effective"] == sum(a["allowed"] for a in s["actions"])
    put = next(a for a in s["actions"] if a["action"] == "s3:PutObject")
    assert put["pickable"] is True and put["allowed"] is False and "outside the view-only role" in put["reason"]
    sm = catalog.service("secretsmanager", "builder")
    assert all(not a["pickable"] for a in sm["actions"]) and all(not w["pickable"] for w in sm["wildcards"])


# ---------------------------------------------------------------- effective access per service

def test_effective_rows(catalog):
    pv = catalog.preview(["s3:*", "dynamodb:Scan", "s3:ListAllMyBuckets"], "view-only")
    s3, ddb = row(pv, "s3"), row(pv, "dynamodb")
    snap = catalog.snapshot("view-only")["services"]
    assert s3["asked"] == len(snap["s3"]["actions"])                 # s3:* covers the single pick too
    assert s3["effective"] == snap["s3"]["allowed"] == s3["role_allows"] == len(s3["actions"])
    assert "s3:ListAllMyBuckets" in s3["actions"] and "s3:PutObject" not in s3["actions"]
    assert (ddb["asked"], ddb["effective"], ddb["no_effect"]) == (1, 0, True)
    assert "bare 403" in ddb["reason"] and ddb["role_allows"] > 0


def test_limited_actions_are_flagged(catalog):
    pv = catalog.preview(["s3:Get*", "dynamodb:Scan"], "data-reader")
    s3 = row(pv, "s3")
    assert "s3:GetObject" in s3["actions"] and "s3:GetObject" in s3["limited_actions"] and s3["limited"] >= 1
    assert cov(pv, "dynamodb:Scan")["limited"] == 1 and cov(pv, "dynamodb:Scan")["tone"] == "all"


def test_effective_with_the_session_policy_off_is_the_role(catalog):
    pv = catalog.preview([], "data-reader", session_policy=False)
    assert [r["service"] for r in pv["effective"]] == ["dynamodb", "s3", "sts"]
    assert all(r["asked"] is None and r["actions"] is None and r["effective"] == r["role_allows"] > 0 for r in pv["effective"])
    assert pv["allowed_services"] == ["dynamodb", "s3", "sts"]
    big = catalog.preview([], "view-only", session_policy=False)
    assert len(big["effective"]) > 10 and "secretsmanager" not in {r["service"] for r in big["effective"]}


# ---------------------------------------------------------------- the SSM Deny

@pytest.mark.parametrize("pick,deny", [("ssm:*", True), ("ssm:Get*", True), ("ssm:Describe*", False),
                                        ("ssm:List*", False), ("ssm:DescribeParameters", False)])
def test_ssm_deny_when_a_pick_covers_parameter_reads(catalog, pick, deny):
    pv = catalog.preview([pick, "s3:ListAllMyBuckets"], "read-only")
    stmts = pv["policy"]["Statement"]
    has = any(s.get("Effect") == "Deny" for s in stmts)
    assert has is deny and pv["deny_ssm"] is deny and pv["statements"] == stmts[1:]
    if deny:
        d = next(s for s in stmts if s.get("Effect") == "Deny")
        assert d == {"Sid": "NoParameterValues", "Effect": "Deny", "Action": ["ssm:GetParameter*"], "Resource": "*"}
        c = cov(pv, pick)
        assert c["tone"] == "partial" and "ssm:GetParameter" in c["reason"] and "denied in the session policy" in c["reason"]
        assert not any(a.startswith("ssm:GetParameter") for a in row(pv, "ssm")["actions"])
    # never a Deny for secretsmanager: the baseline Allow is the proxy's own secret read
    assert not any(s.get("Effect") == "Deny" and "secretsmanager" in json.dumps(s) for s in stmts)


# ---------------------------------------------------------------- iron-proxy's lists and the policy's size

def test_iron_proxy_gets_only_effective_services(catalog):
    pv = catalog.preview(["s3:ListAllMyBuckets", "dynamodb:Scan", "lambda:InvokeFunction"], "view-only")
    assert pv["allowed_services"] == ["s3"] and pv["services"] == ["dynamodb", "lambda", "s3"]
    assert not any("dynamodb" in h or "lambda" in h for h in pv["hosts"]) and "*.s3.amazonaws.com" in pv["hosts"]
    # every pick without effect: nothing for iron-proxy, never an empty list
    assert catalog.preview(["dynamodb:Scan"], "view-only")["allowed_services"] == [cat.NO_SERVICE]


def test_policy_size_with_service_wildcards(catalog):
    pv = catalog.preview(["s3:*"], "read-only")
    assert pv["fits"] and pv["chars"] < 700 and pv["policy"]["Statement"][1]["Action"] == ["s3:*"]
    with_ssm = catalog.preview(["s3:*", "ssm:*"], "read-only")
    assert with_ssm["chars"] > pv["chars"] + 60 and with_ssm["fits"]


# ---------------------------------------------------------------- through the API and into the bucket

def test_apply_writes_the_deny_and_only_effective_services():
    env = Env()
    r = env.c.put("/api/policy/aws/scope", json={"role": "read-only", "session_policy": True,
                                                 "actions": ["ssm:*", "s3:ListAllMyBuckets", "iam:CreateRole"]},
                  headers=hdr("admin"))
    assert r.status_code == 200, r.text
    scope = env.s3.json("aws-scope.json")
    assert scope["statements"][-1]["Effect"] == "Deny" and scope["statements"][-1]["Action"] == ["ssm:GetParameter*"]
    assert scope["actions"] == ["iam:CreateRole", "s3:ListAllMyBuckets", "ssm:*"]
    from app.policy import ConfigDoc
    assert ConfigDoc(env.s3.text("iron-proxy.yaml")).allowed_services() == ["s3", "ssm"]      # iam: no effect
    pv = env.c.get("/api/policy/aws/scope", headers=hdr("viewer")).json()["preview"]
    assert cov(pv, "iam:CreateRole")["tone"] == "none" and row(pv, "iam")["no_effect"] is True
