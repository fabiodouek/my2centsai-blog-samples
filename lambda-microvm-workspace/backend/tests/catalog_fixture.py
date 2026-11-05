"""A Catalog built from a recorded slice of AWS's Service Authorization Reference (24 services, trimmed to the
fields we read) and the four sandbox roles' policies as part 2 renders them (fake account, ViewOnlyAccess trimmed to
those services, ReadOnlyAccess v191's matching patterns, PowerUserAccess whole). No network."""

from __future__ import annotations

import json
from pathlib import Path

from app import aws_endpoints
from app.aws_catalog import Catalog
from app.aws_reference import REFERENCE_URL, TTLCache, fetch_reference
from app.aws_roles import fetch_roles

DATA = Path(__file__).parent / "data" / "service_reference"
ROLES = json.loads((DATA / "roles.json").read_text())
ROLE_DEFS = {k: {"arn": v["arn"], "description": v["description"], "default": v["default"]} for k, v in ROLES["roles"].items()}


def fixture_get(url: str):
    name = "index.json" if url == REFERENCE_URL else url.rsplit("/", 1)[-1]
    body = (DATA / name).read_bytes()
    return json.loads(body), len(body)


class FakeIAM:
    """The IAM calls fetch_roles makes, served from roles.json."""

    def __init__(self, roles: dict | None = None):
        self.data = json.loads(json.dumps(ROLES))
        if roles:
            self.data["roles"].update(roles)
        self.calls = 0

    def _role(self, name):
        return next(v for v in self.data["roles"].values() if v["arn"].endswith("/" + name))

    def list_attached_role_policies(self, RoleName, **kw):
        self.calls += 1
        return {"AttachedPolicies": [{"PolicyArn": a, "PolicyName": self.data["managed"][a]["name"]}
                                     for a in self._role(RoleName)["managed"]], "IsTruncated": False}

    def get_policy(self, PolicyArn):
        return {"Policy": {"DefaultVersionId": self.data["managed"][PolicyArn]["version"]}}

    def get_policy_version(self, PolicyArn, VersionId):
        return {"PolicyVersion": {"VersionId": VersionId, "Document": self.data["managed"][PolicyArn]["doc"]}}

    def list_role_policies(self, RoleName, **kw):
        return {"PolicyNames": list(self._role(RoleName)["inline"]), "IsTruncated": False}

    def get_role_policy(self, RoleName, PolicyName):
        return {"PolicyName": PolicyName, "PolicyDocument": self._role(RoleName)["inline"][PolicyName]}


def make_catalog(baseline: dict, get=fixture_get, iam=None, clock=None, ttl=3600, region="us-east-1",
                 role_defs: dict | None = None) -> Catalog:
    iam = iam or FakeIAM()
    defs = role_defs or ROLE_DEFS
    kw = {"clock": clock} if clock else {}
    ref = TTLCache("service reference", lambda: fetch_reference(get=get, workers=4), ttl=ttl, background=False, **kw)
    roles = TTLCache("sandbox roles", lambda: fetch_roles(iam, defs), ttl=ttl, background=False, **kw)
    return Catalog(ref, roles, defs, aws_endpoints.load()["clients"], region, baseline)
