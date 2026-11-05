"""The sandbox roles part 2 defines (view-only, read-only, data-reader, builder), their effective permissions, and
a local evaluator for "would this role allow this action?" used to grey out the catalog.

The policies are read once an hour (iam:ListAttachedRolePolicies + GetPolicy/GetPolicyVersion for managed ones,
iam:ListRolePolicies + GetRolePolicy for inline ones) and evaluated here, not with one simulator call per action:

- an action is `allowed` when some Allow statement matches it (Action or NotAction, `*`/`?` wildcards,
  case-insensitive) and no Deny that applies everywhere (Resource "*", no Condition) matches it;
- `limited` when every matching Allow is tied to specific resources or to a condition (data-reader's bucket);
- Denies tied to resources or conditions (builder's stack guardrails, its Region lock) don't grey an action out:
  they only bite for those resources or Regions.

Session policies and resource policies aren't part of this: it's the role's own ceiling."""

from __future__ import annotations

import logging

from .aws_reference import Ceiling

log = logging.getLogger("aws_roles")


def fetch_roles(iam, roles: dict[str, dict]) -> dict:
    """{role id: {"policies": [names], "statements": [...]}} for every role, from IAM."""
    out = {}
    for rid, r in roles.items():
        name = r["arn"].rsplit("/", 1)[-1]
        names, statements = [], []
        kw = {"RoleName": name}
        while True:
            page = iam.list_attached_role_policies(**kw)
            for p in page.get("AttachedPolicies", []):
                ver = iam.get_policy(PolicyArn=p["PolicyArn"])["Policy"]["DefaultVersionId"]
                doc = iam.get_policy_version(PolicyArn=p["PolicyArn"], VersionId=ver)["PolicyVersion"]["Document"]
                names.append(f"{p['PolicyName']} ({ver})")
                statements += _statements(doc, p["PolicyName"])
            if not page.get("IsTruncated"):
                break
            kw["Marker"] = page["Marker"]
        kw = {"RoleName": name}
        while True:
            page = iam.list_role_policies(**kw)
            for pn in page.get("PolicyNames", []):
                doc = iam.get_role_policy(RoleName=name, PolicyName=pn)["PolicyDocument"]
                names.append(f"inline: {pn}")
                statements += _statements(doc, pn)
            if not page.get("IsTruncated"):
                break
            kw["Marker"] = page["Marker"]
        out[rid] = {"policies": names, "statements": statements}
    return out


def _statements(doc, source: str) -> list[dict]:
    if isinstance(doc, str):
        import json
        doc = json.loads(doc)
    st = doc.get("Statement") or []
    out = []
    for s in st if isinstance(st, list) else [st]:
        if isinstance(s, dict):
            out.append({**s, "_source": source})
    return out


def _list(v) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


class _Stmt:
    def __init__(self, s: dict):
        self.sid = s.get("Sid") or s.get("_source") or "statement"
        self.effect = s.get("Effect")
        self.negated = "NotAction" in s
        pats = _list(s.get("NotAction") if self.negated else s.get("Action"))
        self.matcher = Ceiling(pats)
        resources = _list(s.get("Resource"))
        self.everywhere = (not s.get("NotResource")) and resources == ["*"] and not s.get("Condition")

    def matches(self, action: str) -> bool:
        hit = self.matcher.allows(action)
        return not hit if self.negated else hit


class RoleEvaluator:
    def __init__(self, role: str, statements: list[dict]):
        self.role = role
        st = [_Stmt(s) for s in statements if s.get("Effect") in ("Allow", "Deny")]
        self.allows = [s for s in st if s.effect == "Allow"]
        # Only Denies that apply to every resource, unconditionally, grey an action out
        self.denies = [s for s in st if s.effect == "Deny" and s.everywhere]

    def verdict(self, action: str) -> tuple[bool, bool, str | None]:
        """(allowed, limited, reason when not allowed)."""
        for d in self.denies:
            if d.matches(action):
                return False, False, f"denied by the {self.role} role ({d.sid})"
        matching = [a for a in self.allows if a.matches(action)]
        if not matching:
            return False, False, f"outside the {self.role} role's permissions"
        return True, all(not a.everywhere for a in matching), None
