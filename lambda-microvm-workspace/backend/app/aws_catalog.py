"""The AWS access catalog: every service and action in AWS's Service Authorization Reference, marked against
the scoped role's ceiling (ReadOnlyAccess) and mapped to what iron-proxy needs, plus the session policy built
from a selection.

- Access level from the action's annotations (aws_reference.access_level).
- `allowed` (effective): the selected role allows it (RoleEvaluator, from the role's own policies) and it isn't
  blocked here. `pickable`: it can go in a session policy: known, its service mapped, not blocked. An action outside
  the role is pickable but has no effect with that role, so a policy can be built before switching role.
- Wildcards: `<service>:*` (every action) and `Get*`, `List*`, `Describe*`, always pickable in a mapped service except
  Secrets Manager; each reports how many of the actions it covers are effective with the role.
- Mapping: IAM prefix -> boto3 clients (from the reference's own Operations[].SDK entries) -> SigV4 signing
  names and us-east-1 hosts (aws_endpoints, generated from botocore). No client, no mapping: the service is
  listed with enabled=false.
- Blocked whatever the ceiling says: every secretsmanager action (iron-proxy fetches its own secrets with the
  same session, so allowing the service would hand sandboxes the real keys), and ssm:GetParameter* (reads
  SecureString parameters decrypted with the AWS-managed key, such as the playground's own origin secret)."""

from __future__ import annotations

import json
import re
import threading
from collections import Counter

from .aws_reference import LEVELS, TTLCache
from .aws_roles import RoleEvaluator

BUILDER_WARNING = ("builder can create billable resources in this account. Changes apply to every sandbox on this "
                   "proxy.")
POLICY_CHARS = 2048          # STS: a session policy's plaintext can't exceed 2,048 characters
NO_SERVICE = "none"          # iron-proxy 0.55.0 treats an empty allowed_services list as "every service"
WILDCARD_VERBS = ("List", "Describe", "Get")

UNMAPPED_REASON = "can't be enabled here (no endpoint mapping)"
OUTSIDE_REASON = ("can't be enabled here (no endpoint mapping under amazonaws.com: its endpoint is under api.aws, "
                  "which iron-proxy doesn't re-sign here)")
SECRETS_REASON = "never enabled here: iron-proxy fetches its own secrets with the same session"
SSM_REASON = ("never enabled here: reads SecureString parameters, such as this playground's own origin secret")
BLOCKED = [(re.compile(r"^secretsmanager:", re.I), SECRETS_REASON),
           (re.compile(r"^ssm:GetParameter", re.I), SSM_REASON)]
# When a pick covers a blocked SSM action (ssm:*, ssm:Get*), the session policy denies these explicitly: a wildcard
# must never grant them. Secrets Manager isn't denied there: the baseline Allow is the proxy's own secret read, and
# iron-proxy never lets sandbox calls reach the service.
SSM_DENY = ["ssm:GetParameter*"]
SSM_DENY_SID = "NoParameterValues"
# AWS requires no permission for these, and no policy can deny them (sts:GetCallerIdentity): always effective
ALWAYS_EFFECTIVE = {"sts:getcalleridentity"}
NO_EFFECT = "no effect with {role}"
NO_EFFECT_PROXY = ("no effect with {role}: no picked action in it is allowed, so iron-proxy refuses its calls "
                   "(a bare 403) before they reach AWS")

# Hosts SDKs and the CLI use besides the client's default us-east-1 endpoint
EXTRA_HOSTS = {"s3": ["*.s3.{region}.amazonaws.com"], "sts": ["sts.amazonaws.com"]}


class CatalogError(ValueError):
    pass


def statements(actions: list[str], deny_ssm: bool = False) -> list[dict]:
    """The scope's own statements; the sidecar puts the proxy's baseline statement in front. `deny_ssm` adds the
    explicit Deny for the SSM parameter reads a picked wildcard covers."""
    if not actions:
        return []
    out = [{"Sid": "Scope", "Effect": "Allow", "Action": list(actions), "Resource": "*"}]
    if deny_ssm:
        out.append({"Sid": SSM_DENY_SID, "Effect": "Deny", "Action": list(SSM_DENY), "Resource": "*"})
    return out


def session_policy_doc(actions: list[str], baseline: dict, deny_ssm: bool = False) -> dict:
    """Exactly what the sidecar sends to STS (jq -c of the same structure)."""
    return {"Version": "2012-10-17", "Statement": [baseline, *statements(actions, deny_ssm)]}


def minified(policy: dict) -> str:
    return json.dumps(policy, separators=(",", ":"))


def blocked(action: str) -> str | None:
    return next((reason for r, reason in BLOCKED if r.search(action)), None)


# ---- the Secrets Manager guard
# iron-proxy 0.55.0's aws_auth takes the service from the client's own credential scope, and an empty
# allowed_services list means every service. Whatever the role and the session policy, sandboxes must never reach
# Secrets Manager through it: the session that signs their calls can read the proxy's secrets.
SECRETS_SERVICES = {"secretsmanager"}


def secrets_endpoints(region: str) -> list[str]:
    return [f"secretsmanager.{region}.amazonaws.com", f"secretsmanager-fips.{region}.amazonaws.com",
            "secretsmanager.amazonaws.com", f"x.secretsmanager.{region}.amazonaws.com"]


def guard_services(names) -> list[str]:
    """allowed_services without Secrets Manager; never empty (an empty list would allow everything)."""
    out = sorted({str(n) for n in names if str(n).strip() and str(n).lower() not in SECRETS_SERVICES})
    return out or [NO_SERVICE]


def _glob(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower(), host.lower()
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) or host == pattern[2:]
    return pattern == host


def guard_hosts(hosts, region: str) -> list[str]:
    """Allowlist hosts minus anything that would match a Secrets Manager endpoint."""
    eps = secrets_endpoints(region)
    out = []
    for h in hosts:
        if "secretsmanager" in h.lower() or any(_glob(h, e) for e in eps):
            continue
        if h not in out:
            out.append(h)
    return out


def tone(effective: int, covers: int) -> str:
    return "all" if covers and effective == covers else ("none" if effective == 0 else "partial")


def coverage_of(covered: list[dict], role: str) -> dict:
    """How many of the actions a pick covers are effective with the role, and why the rest aren't."""
    eff = [a for a in covered if a["allowed"]]
    blk = [a for a in covered if a.get("blocked")]
    outside = len(covered) - len(eff) - len(blk)
    parts = []
    if outside:
        parts.append(f"{outside} outside the {role} role" if len(covered) > 1 else
                     f"{NO_EFFECT.format(role=role)}: {next((a['reason'] for a in covered if not a['allowed']), '')}")
    if blk:
        names = sorted({a["action"] for a in blk})
        parts.append(f"{len(blk)} never enabled here ({', '.join(names[:2])}{', ...' if len(names) > 2 else ''}), "
                     "denied in the session policy")
    reason = None
    if parts:
        reason = (f"{len(eff)} of {len(covered)} effective: " if len(covered) > 1 else "") + "; ".join(parts)
    return {"covers": len(covered), "effective": len(eff), "limited": sum(1 for a in eff if a["limited"]),
            "blocked": len(blk), "tone": tone(len(eff), len(covered)), "reason": reason}


class Catalog:
    """`roles` caches each sandbox role's policy statements (aws_roles.fetch_roles); `role_defs` is part 2's
    {role id: {"arn", "description", "default"}}. Every view is evaluated for one role (the default unless asked)."""

    def __init__(self, reference: TTLCache, roles: TTLCache, role_defs: dict, endpoints: dict, region: str,
                 baseline: dict):
        self.reference, self.roles, self.role_defs, self.endpoints = reference, roles, role_defs, endpoints
        self.region, self.baseline = region, baseline
        self._lock = threading.Lock()
        self._snaps: dict[tuple, dict] = {}

    @property
    def default_role(self) -> str:
        return next((r for r, d in self.role_defs.items() if d.get("default")), next(iter(self.role_defs), ""))

    def role(self, role: str | None) -> str:
        r = role or self.default_role
        if r not in self.role_defs:
            raise CatalogError(f"role must be one of {', '.join(self.role_defs)}")
        return r

    def role_list(self) -> list[dict]:
        return [{"id": r, "description": d.get("description"), "default": bool(d.get("default")),
                 "warning": BUILDER_WARNING if r == "builder" else None} for r, d in self.role_defs.items()]

    # ---- the derived index per role, rebuilt when the reference or the role policies change
    def snapshot(self, role: str | None = None) -> dict:
        role = self.role(role)
        ref, roles = self.reference.get(), self.roles.get()
        key = (self.reference.version, self.roles.version, role)
        with self._lock:
            if key in self._snaps:
                return self._snaps[key]
        if role not in roles:
            raise CatalogError(f"no policies were read for the {role} role")
        snap = self._build(ref, RoleEvaluator(role, roles[role]["statements"]))
        with self._lock:
            self._snaps = {k: v for k, v in self._snaps.items() if k[:2] == key[:2]}
            self._snaps[key] = snap
        return snap

    def _clients(self, sid: str, clients: list[str]) -> tuple[list[str], str | None]:
        """boto3 clients for an IAM prefix: from the reference's Operations[].SDK join; when that finds none (about
        100 reference files carry no SDK entries), a client named after the prefix or whose endpoint prefix is the
        prefix; failing that, one whose signing name is the prefix. Returns the clients and how they were found."""
        known = [c for c in clients if c in self.endpoints]
        if known:
            return known, "operations"
        for how, field in (("name", None), ("endpoint prefix", "endpoint_prefix"), ("signing name", "signing_name")):
            guess = sorted(c for c, e in self.endpoints.items() if sid == (c if field is None else e.get(field)))
            if guess:
                return guess, how
        return [], None

    def _map(self, sid: str, clients: list[str]) -> tuple[list[str], list[str], str | None]:
        """(signing names, allowlist hosts, reason it can't be enabled). Only endpoints under amazonaws.com count:
        iron-proxy's aws_auth rule (and the allowlist's AWS section) covers *.amazonaws.com, so a service served
        only from api.aws (datazone, qbusiness, notifications, ...) can't be re-signed here."""
        known, _ = self._clients(sid, clients)
        if not known:
            return [], [], UNMAPPED_REASON
        usable = [c for c in known if str(self.endpoints[c].get("host") or "").endswith(".amazonaws.com")]
        if not usable:
            return [], [], OUTSIDE_REASON
        signing = sorted({self.endpoints[c]["signing_name"] for c in usable})
        hosts: list[str] = []
        for c in usable:
            h = f"*.{self.endpoints[c]['host']}"
            if h not in hosts:
                hosts.append(h)
        hosts += [h.format(region=self.region) for h in EXTRA_HOSTS.get(sid, []) if h.format(region=self.region) not in hosts]
        return signing, hosts, None

    def _build(self, ref: dict, ev: RoleEvaluator) -> dict:
        services, index, flat, host_owner = {}, {}, [], {}
        for sid in sorted(ref["services"]):
            s = ref["services"][sid]
            signing, hosts, unmapped = self._map(sid, s.get("clients") or [])
            enabled = unmapped is None
            acts, levels, n_allowed = [], Counter(), 0
            for name, level in s["actions"]:
                full = f"{sid}:{name}"
                reason, limited = blocked(full), False
                if not reason and not enabled:
                    reason = unmapped
                pickable = reason is None
                if pickable:
                    if full.lower() in ALWAYS_EFFECTIVE:
                        ok, limited, why = True, False, None
                    else:
                        ok, limited, why = ev.verdict(full)
                    reason = None if ok else why
                rec = {"action": full, "service": sid, "name": name, "level": level, "allowed": reason is None,
                       "pickable": pickable, "limited": bool(limited and reason is None), "reason": reason,
                       "blocked": bool(blocked(full))}
                acts.append(rec)
                levels[level] += 1
                n_allowed += reason is None
                index[full.lower()] = rec
                flat.append((name.lower(), full.lower(), rec))
            wildcards = []
            for verb in ("", *WILDCARD_VERBS):
                if not verb and sid in SECRETS_SERVICES:
                    continue            # never offer secretsmanager:*
                covered = [a for a in acts if a["name"].startswith(verb)]
                if not covered:
                    continue
                if not enabled:
                    pickable, reason = False, unmapped
                elif sid in SECRETS_SERVICES:
                    pickable, reason = False, SECRETS_REASON
                else:
                    pickable, reason = True, None
                cov = coverage_of(covered, ev.role)
                if pickable and cov["effective"] < cov["covers"]:
                    reason = cov["reason"]
                w = {"action": f"{sid}:{verb}*", "service": sid, "name": f"{verb}*",
                     "label": f"Every {verb} action" if verb else "Every action", "level": "Wildcard",
                     "covers": cov["covers"], "effective": cov["effective"] if pickable else 0,
                     "tone": cov["tone"] if pickable else "none", "pickable": pickable,
                     "allowed": pickable and cov["effective"] == cov["covers"],
                     "limited": pickable and cov["limited"] > 0, "reason": reason, "_covered": covered}
                wildcards.append(w)
                index[w["action"].lower()] = w
            for h in hosts:
                host_owner.setdefault(h, sid)
            services[sid] = {"id": sid, "name": s.get("name") or sid, "enabled": enabled,
                             "reason": unmapped, "signing_names": signing, "hosts": hosts,
                             "actions": acts, "wildcards": wildcards, "levels": {lv: levels.get(lv, 0) for lv in LEVELS},
                             "allowed": n_allowed}
        # Session policy off: what iron-proxy lets through is every mapped service the role allows at least one
        # action in, explicitly (an empty allowed_services would mean every service), never Secrets Manager
        enabled = [s for s in services.values() if s["enabled"] and s["allowed"] > 0]
        open_services = guard_services({n for s in enabled for n in s["signing_names"]})
        open_hosts = guard_hosts([h for s in enabled if s["id"] not in SECRETS_SERVICES for h in s["hosts"]], self.region)
        return {"services": services, "index": index, "flat": flat, "host_owner": host_owner, "role": ev.role,
                "open": {"allowed_services": open_services, "hosts": open_hosts},
                "counts": {"services": len(services), "actions": len(flat),
                           "mapped": sum(1 for s in services.values() if s["enabled"]),
                           "allowed": sum(s["allowed"] for s in services.values())}}

    # ---- the API views
    def listing(self, role: str | None = None) -> dict:
        snap = self.snapshot(role)
        ref, roles = self.reference.get(), self.roles.get()
        return {
            "region": self.region,
            "role": snap["role"],
            "roles": self.role_list(),
            "source": {"url": ref.get("url"), **self.reference.state(), "services": snap["counts"]["services"],
                       "actions": snap["counts"]["actions"], "mapped_services": snap["counts"]["mapped"],
                       "fetch_s": ref.get("fetch_s"), "bytes": ref.get("bytes")},
            "ceiling": {"role": snap["role"], "policies": roles[snap["role"]]["policies"], **self.roles.state()},
            "levels": list(LEVELS),
            "services": [{"id": s["id"], "name": s["name"], "actions": len(s["actions"]), "allowed": s["allowed"],
                          "levels": s["levels"], "enabled": s["enabled"], "reason": s["reason"],
                          "pickable": s["enabled"] and s["id"] not in SECRETS_SERVICES,
                          "signing_names": s["signing_names"]} for s in snap["services"].values()],
            "baseline": self.baseline,
            "limits": {"policy_chars": POLICY_CHARS},
        }

    def service(self, sid: str, role: str | None = None) -> dict | None:
        s = self.snapshot(role)["services"].get(sid)
        if not s:
            return None
        return {k: s[k] for k in ("id", "name", "enabled", "reason", "signing_names", "hosts")} | {
            "actions": [{k: a[k] for k in ("action", "name", "level", "allowed", "pickable", "limited", "reason")}
                        for a in s["actions"]],
            "wildcards": [{k: w[k] for k in ("action", "label", "covers", "effective", "tone", "allowed", "pickable",
                                              "limited", "reason")} for w in s["wildcards"]],
            "role": self.role(role)}

    def search(self, q: str = "", level: str | None = None, limit: int = 100, role: str | None = None) -> dict:
        """Substring search over action names (`GetObject` or `s3:GetObject`), service ids and names. Exact
        matches first, then prefix matches, then the rest; within each, usable actions first."""
        snap = self.snapshot(role)
        q = (q or "").strip().lower()
        if level and level not in LEVELS:
            raise CatalogError(f"level must be one of {', '.join(LEVELS)}")
        if not q and not level:
            return {"q": q, "level": level, "total": 0, "truncated": False, "services": [], "actions": []}
        ranked: list[tuple[int, str, dict]] = []
        per_service: Counter = Counter()
        svc_hit = {sid for sid, s in snap["services"].items() if q and (q in sid.lower() or q in s["name"].lower())}
        for name, full, rec in snap["flat"]:
            if level and rec["level"] != level:
                continue
            if not q:
                rank = 2
            elif q in (name, full):
                rank = 0
            elif name.startswith(q) or full.startswith(q):
                rank = 1
            elif q in name or q in full:
                rank = 2
            else:
                continue
            ranked.append((rank, full, rec))
            per_service[rec["service"]] += 1
        # Within a rank, effective actions come first, then the rest that can be picked
        ranked.sort(key=lambda t: (t[0], not t[2]["allowed"], not t[2]["pickable"], t[1]))
        out = [{k: r[k] for k in ("action", "service", "name", "level", "allowed", "pickable", "limited", "reason")}
               for _, _, r in ranked[:limit]]
        sids = sorted(svc_hit | set(per_service), key=lambda sid: (sid not in svc_hit, -per_service[sid], sid))
        services = [{"id": sid, "name": snap["services"][sid]["name"], "enabled": snap["services"][sid]["enabled"],
                     "matches": per_service[sid]} for sid in sids]
        return {"q": q, "level": level, "role": snap["role"], "total": len(ranked), "truncated": len(ranked) > limit,
                "services": services, "actions": out}

    def host_owner(self) -> dict[str, str]:
        try:
            return self.snapshot(None)["host_owner"]
        except Exception:
            return {}

    # ---- a selection -> session policy, allowed_services, hosts
    def normalize(self, actions: list[str], role: str | None = None) -> list[str]:
        """Canonical names, deduped and sorted. Refuses unknown actions, anything in Secrets Manager, ssm:GetParameter*
        on its own and services with no endpoint mapping. Actions outside the role are kept: they have no effect
        with it (preview says so), and a selection survives a role switch."""
        index = self.snapshot(role)["index"]
        out, unknown, refused = [], [], []
        for a in actions:
            a = str(a).strip()
            if not a:
                continue
            rec = index.get(a.lower())
            if not rec:
                unknown.append(a)
            elif not rec["pickable"]:
                refused.append(f"{rec['action']} ({rec['reason']})")
            elif rec["action"] not in out:
                out.append(rec["action"])
        problems = []
        if unknown:
            problems.append("not in the Service Authorization Reference: " + ", ".join(unknown[:5]) +
                            (f" and {len(unknown) - 5} more" if len(unknown) > 5 else ""))
        if refused:
            problems.append("can't be applied: " + "; ".join(refused[:3]) +
                            (f" and {len(refused) - 3} more" if len(refused) > 3 else ""))
        if problems:
            raise CatalogError("; ".join(problems))
        return sorted(out)

    def known(self, actions: list[str]) -> list[str]:
        """Canonical names of the actions the catalog knows, whatever the role (kept while the policy is off)."""
        index = self.snapshot(None)["index"]
        return sorted({index[str(a).strip().lower()]["action"] for a in actions if str(a).strip().lower() in index})

    def _covered(self, rec: dict) -> list[dict]:
        return rec["_covered"] if "_covered" in rec else [rec]

    def preview(self, actions: list[str], role: str | None = None, session_policy: bool = True) -> dict:
        """The session policy for a selection, what iron-proxy is told (allowed_services, hosts: services with at least
        one effective action), `coverage` per pick (how many of the actions it covers are effective with the role)
        and `effective` per service (role ∩ session policy). Effective = allowed by the role's own policies as
        RoleEvaluator reads them (unconditional Denies; Allows tied to resources or conditions are `limited`)."""
        role = self.role(role)
        snap = self.snapshot(role)
        services = snap["services"]
        if not session_policy:
            # The role's full permissions: no Policy to STS. iron-proxy still needs explicit lists.
            rows = [{"service": s["id"], "name": s["name"], "asked": None, "effective": s["allowed"],
                     "role_allows": s["allowed"], "total": len(s["actions"]),
                     "limited": sum(1 for a in s["actions"] if a["allowed"] and a["limited"]),
                     "actions": None, "limited_actions": None, "no_effect": False, "reason": None}
                    for s in services.values() if s["enabled"] and s["allowed"] > 0 and s["id"] not in SECRETS_SERVICES]
            return {"role": role, "session_policy": False, "actions": self.known(actions), "services": [],
                    "allowed_services": snap["open"]["allowed_services"], "hosts": snap["open"]["hosts"],
                    "policy": None, "policy_json": None, "chars": 0, "limit": POLICY_CHARS, "fits": True,
                    "statements": [], "deny_ssm": False, "coverage": [], "effective": rows}
        acts = self.normalize(actions, role)
        index = snap["index"]
        coverage, asked, deny = [], {}, False
        for a in acts:
            rec = index[a.lower()]
            covered = self._covered(rec)
            cov = coverage_of(covered, role)
            deny = deny or cov["blocked"] > 0
            coverage.append({"action": rec["action"], "kind": "wildcard" if "_covered" in rec else "action",
                             **{k: cov[k] for k in ("covers", "effective", "limited", "tone", "reason")}})
            bucket = asked.setdefault(rec["service"], {})
            for c in covered:
                bucket[c["action"]] = c
        rows = []
        for sid in sorted(asked):
            s, covered = services[sid], list(asked[sid].values())
            eff = sorted(c["action"] for c in covered if c["allowed"])
            lim = sorted(c["action"] for c in covered if c["allowed"] and c["limited"])
            rows.append({"service": sid, "name": s["name"], "asked": len(covered), "effective": len(eff),
                         "role_allows": s["allowed"], "total": len(s["actions"]), "limited": len(lim),
                         "actions": eff, "limited_actions": lim, "no_effect": not eff,
                         "reason": NO_EFFECT_PROXY.format(role=role) if not eff else None})
        live = [r["service"] for r in rows if r["effective"] > 0]
        signing = guard_services({n for sid in live for n in services[sid]["signing_names"]})
        hosts: list[str] = []
        for sid in live:
            hosts += [h for h in services[sid]["hosts"] if h not in hosts]
        hosts = guard_hosts(hosts, self.region)
        policy = session_policy_doc(acts, self.baseline, deny)
        text = minified(policy)
        return {"role": role, "session_policy": True, "actions": acts, "services": sorted(asked),
                "allowed_services": signing, "hosts": hosts, "policy": policy, "policy_json": text, "chars": len(text),
                "limit": POLICY_CHARS, "fits": len(text) <= POLICY_CHARS,
                "statements": statements(acts, deny), "deny_ssm": deny, "coverage": coverage, "effective": rows}
