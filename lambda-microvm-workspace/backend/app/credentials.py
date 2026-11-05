"""Credential usage (from the iron-proxy audit log) and revoke/restore. This module never reads a secret value.

Revoke = a Secrets Manager resource policy denying the iron-proxy task role GetSecretValue, then a forced
new deployment of the iron-proxy ECS service. The restart is what makes it real: part 2 measured that
iron-proxy keeps injecting its cached value when a refresh fails, so a policy alone changes nothing until
the task is replaced. The new task starts without the secret and forwards the placeholder unswapped, so
the upstream rejects it (Anthropic answers 401). Status stays "revoking" until that deployment completes.

Restore = delete the deny statement only; no restart. The running proxy refetches on its own (within its
failure TTL, about a minute). Status stays "restoring" until the audit log shows a swap of that secret
again, or 90 s pass.

With the proxy control plane on (part 2's control_plane=true), iron-proxy fetches secrets as the sandbox role in
use, so the deny names all four roles too, and revoke/restore write a new config version instead of restarting the proxy:
the control sidecar's POST /v1/reload rebuilds iron-proxy's secrets cache in place. The sidecar checks the bucket
every 30 s (part 2's control_poll_s), so that takes up to about 30 s. Status stays "revoking" until the sidecar
reports that version applied."""

from __future__ import annotations

import json
import logging
import threading
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .audit import SecretNames, ms_iso, parse_line
from .config import Settings

log = logging.getLogger("credentials")

REVOKE_SID = "My2centsWorkspaceRevoke"
WINDOW_S = 15 * 60
CACHE_S = 10
MAX_EVENTS = 20000
PAGE_BUDGET_S = 8.0   # stop paginating after this; the counts are then a lower bound

REVOKE_EFFECTIVE_S = 180
RESTORE_EFFECTIVE_S = 60
RELOAD_MARGIN_S = 5   # a reload is in effect within the sidecar's poll interval plus this
RESTORE_GIVE_UP_S = 90
SERVICE_CACHE_S = 3

REVOKE_WARNING = ("Revoking for every sandbox on this proxy: iron-proxy is restarting so it drops its cached copy "
                  "of the key (about 3 minutes; requests keep flowing meanwhile). After that, sandboxes' "
                  "placeholder goes out unswapped and the upstream rejects it.")
RESTORE_WARNING = ("Restored for every sandbox on this proxy. No restart: iron-proxy fetches the key again within "
                   "about a minute.")
RELOAD_REVOKE_WARNING = ("Revoking for every sandbox on this proxy: at its next check (within about {poll} s) the "
                         "proxy reloads its policy, with no restart, and drops its cached copy of the key. Until then "
                         "the key is still swapped in; after that, sandboxes' placeholder goes out unswapped and the "
                         "upstream rejects it.")
RELOAD_RESTORE_WARNING = ("Restored for every sandbox on this proxy: at its next check (within about {poll} s) the "
                          "proxy reloads its policy and fetches the key again on the next request.")
WARNING = REVOKE_WARNING  # kept for imports


def log_group_name(ref: str) -> str:
    """ARN (arn:aws:logs:r:a:log-group:NAME[:*]) or bare name -> name."""
    if ref.startswith("arn:"):
        name = ref.split(":log-group:", 1)[1]
        return name[:-2] if name.endswith(":*") else name
    return ref


def deny_statement(*principals: str) -> dict:
    """Deny GetSecretValue to the identities iron-proxy fetches secrets as (a role ARN covers its sessions)."""
    arns = [p for p in principals if p]
    return {"Sid": REVOKE_SID, "Effect": "Deny", "Principal": {"AWS": arns[0] if len(arns) == 1 else arns},
            "Action": "secretsmanager:GetSecretValue", "Resource": "*"}


class Credentials:
    """`catalog()` returns {name: {"arn", "placeholder", "kind"}}: part 2's three secrets, plus the custom ones
    when the control plane is on. `policy` (a ProxyPolicy) switches revoke/restore to a reload."""

    def __init__(self, s: Settings, logs_client=None, sm_client=None, ecs_client=None, catalog=None, policy=None):
        self.s = s
        self.catalog = catalog or (lambda: {n: {"arn": a, "placeholder": str(s.placeholders.get(n) or ""), "kind": "builtin"}
                                            for n, a in s.secret_arns.items()})
        self.policy = policy if (policy is not None and getattr(policy, "enabled", False)) else None
        self.names = SecretNames(lambda: {n: c["arn"] for n, c in self.catalog().items()})
        cfg = Config(region_name=s.region, retries={"max_attempts": 3, "mode": "standard"})
        self.logs = logs_client or boto3.client("logs", config=cfg)
        self.sm = sm_client or boto3.client("secretsmanager", config=cfg)
        self.ecs = ecs_client or boto3.client("ecs", config=cfg)
        self._cache: tuple[float, list[dict]] | None = None
        self._summary: tuple[float, list[dict]] | None = None
        self._service: tuple[float, dict] | None = None
        self._ops: dict[str, dict] = {}   # name -> in-flight revoke/restore
        self._lock = threading.Lock()

    # ---- revoke / restore ----
    def _policy(self, arn: str) -> dict | None:
        r = self.sm.get_resource_policy(SecretId=arn)
        p = r.get("ResourcePolicy")
        return json.loads(p) if p else None

    def is_revoked(self, arn: str) -> bool:
        p = self._policy(arn) or {}
        stmts = p.get("Statement") or []
        return any(isinstance(st, dict) and st.get("Sid") == REVOKE_SID for st in (stmts if isinstance(stmts, list) else [stmts]))

    def _put_deny(self, arn: str) -> None:
        p = self._policy(arn) or {"Version": "2012-10-17", "Statement": []}
        stmts = p.get("Statement") or []
        stmts = [st for st in (stmts if isinstance(stmts, list) else [stmts]) if st.get("Sid") != REVOKE_SID]
        # With the control plane on, iron-proxy fetches secrets as whichever sandbox role is in use: deny them all
        principals = [self.s.task_role_arn] + (self.s.sandbox_role_arns if self.policy else [])
        p["Statement"] = stmts + [deny_statement(*principals)]
        self.sm.put_resource_policy(SecretId=arn, ResourcePolicy=json.dumps(p), BlockPublicPolicy=True)

    def _drop_deny(self, arn: str) -> None:
        p = self._policy(arn)
        if p is not None:
            stmts = p.get("Statement") or []
            rest = [st for st in (stmts if isinstance(stmts, list) else [stmts]) if st.get("Sid") != REVOKE_SID]
            if rest:
                p["Statement"] = rest
                self.sm.put_resource_policy(SecretId=arn, ResourcePolicy=json.dumps(p), BlockPublicPolicy=True)
            else:
                self.sm.delete_resource_policy(SecretId=arn)

    def revoked_names(self) -> list[str]:
        """Credentials (built-in and custom) whose secret carries the revoke deny statement now."""
        out = []
        for name, c in self.catalog().items():
            try:
                if self.is_revoked(c["arn"]):
                    out.append(name)
            except ClientError as e:
                log.warning("resource policy %s: %s", name, e.response.get("Error", {}).get("Code"))
        return sorted(out)

    def clear_denies(self) -> list[str]:
        """Factory reset: take the deny statement off every credential's secret. Returns the names it was on."""
        names = self.revoked_names()
        for name in names:
            self._drop_deny(self.arn(name))
        with self._lock:
            self._ops.clear()
        self.invalidate()
        return names

    def arn(self, name: str) -> str:
        c = self.catalog().get(name)
        if not c:
            raise KeyError(name)
        return c["arn"]

    def known(self, name: str) -> bool:
        return name in self.catalog()

    def revoke(self, name: str, user=None) -> dict:
        """Deny policy, then make iron-proxy drop its cached value: a reload (control plane) or a restart. All or
        nothing: if that can't be started, the policy is taken back off so the UI never says "revoked" while the
        cached key still works."""
        if not self.s.task_role_arn:
            raise RuntimeError("TASK_ROLE_ARN is not configured")
        arn = self.arn(name)
        self._put_deny(arn)
        if self.policy:
            try:
                r = self.policy.bump(user, "credential.revoke", f"Revoked {name}", {"name": name})
            except Exception:
                self._drop_deny(arn)
                self.invalidate()
                raise
            now = time.time()
            with self._lock:
                self._ops[name] = {"op": "revoke", "since": now, "method": "reload",
                                   "config_version": r["apply"]["config_version"]}
            self.invalidate()
            log.info("revoke %s: deny policy in place, config v%s", name, r["apply"]["config_version"])
            poll = self.policy.poll_s()
            return {"name": name, "state": "revoking", "revoked": "pending", "method": "reload",
                    "effective_after_s": round(poll + RELOAD_MARGIN_S), "since": ms_iso(now * 1000), "apply": r["apply"],
                    "change": r["change"],
                    "detail": ("Deny policy in place; at its next check (within about "
                               f"{poll:g} s) iron-proxy reloads its policy and drops its cached copy of the key."),
                    "warning": RELOAD_REVOKE_WARNING.format(poll=f"{poll:g}")}
        try:
            r = self.ecs.update_service(cluster=self.s.proxy_ecs_cluster, service=self.s.proxy_ecs_service,
                                        forceNewDeployment=True)
        except Exception:
            self._drop_deny(arn)
            self.invalidate()
            raise
        dep = next((d for d in r["service"].get("deployments", []) if d.get("status") == "PRIMARY"), {})
        now = time.time()
        with self._lock:
            self._ops[name] = {"op": "revoke", "since": now, "deployment": dep.get("id"), "method": "restart"}
            self._service = None
        self.invalidate()
        log.info("revoke %s: deny policy in place, iron-proxy deployment %s started", name, dep.get("id"))
        return {"name": name, "state": "revoking", "revoked": "pending", "method": "restart",
                "effective_after_s": REVOKE_EFFECTIVE_S,
                "since": ms_iso(now * 1000), "deployment_id": dep.get("id"),
                "detail": "Deny policy in place; iron-proxy is restarting so it drops its cached copy of the key.",
                "warning": REVOKE_WARNING}

    def restore(self, name: str, user=None) -> dict:
        arn = self.arn(name)
        self._drop_deny(arn)
        now = time.time()
        if self.policy:
            r = self.policy.bump(user, "credential.restore", f"Restored {name}", {"name": name})
            with self._lock:
                self._ops[name] = {"op": "restore", "since": now, "method": "reload",
                                   "config_version": r["apply"]["config_version"]}
            self.invalidate()
            log.info("restore %s: deny policy removed, config v%s", name, r["apply"]["config_version"])
            poll = self.policy.poll_s()
            return {"name": name, "state": "restoring", "revoked": False, "method": "reload",
                    "effective_after_s": round(poll + RELOAD_MARGIN_S), "since": ms_iso(now * 1000), "apply": r["apply"],
                    "change": r["change"],
                    "detail": ("Deny policy removed; at its next check (within about "
                               f"{poll:g} s) iron-proxy reloads its policy and fetches the key again."),
                    "warning": RELOAD_RESTORE_WARNING.format(poll=f"{poll:g}")}
        with self._lock:
            self._ops[name] = {"op": "restore", "since": now, "method": "restart"}
        self.invalidate()
        log.info("restore %s: deny policy removed", name)
        return {"name": name, "state": "restoring", "revoked": False, "method": "restart",
                "effective_after_s": RESTORE_EFFECTIVE_S,
                "since": ms_iso(now * 1000),
                "detail": "Deny policy removed; iron-proxy picks the key up again on its next refresh.",
                "warning": RESTORE_WARNING}

    # ---- in-flight status ----
    def _proxy_service(self) -> dict:
        with self._lock:
            if self._service and time.monotonic() - self._service[0] < SERVICE_CACHE_S:
                return self._service[1]
        r = self.ecs.describe_services(cluster=self.s.proxy_ecs_cluster, services=[self.s.proxy_ecs_service])
        svc = (r.get("services") or [{}])[0]
        with self._lock:
            self._service = (time.monotonic(), svc)
        return svc

    def _revoke_progress(self, op: dict) -> tuple[bool, str]:
        """Done once the service runs a single deployment, created after the deny policy, rolled out."""
        try:
            svc = self._proxy_service()
        except ClientError as e:
            return False, f"can't read the iron-proxy service ({e.response.get('Error', {}).get('Code')})"
        deps = svc.get("deployments") or []
        primary = next((d for d in deps if d.get("status") == "PRIMARY"), None)
        if not primary:
            return False, "waiting for the iron-proxy deployment"
        created = primary.get("createdAt")
        created_s = created.timestamp() if hasattr(created, "timestamp") else 0
        fresh = created_s >= op["since"] - 5
        state = primary.get("rolloutState")
        if fresh and len(deps) == 1 and state == "COMPLETED" and primary.get("runningCount", 0) >= max(1, primary.get("desiredCount", 1)):
            return True, "iron-proxy restarted without access to the secret"
        if state == "FAILED":
            return False, "the iron-proxy deployment failed; the old task (with the cached key) may still be serving"
        old = sum(d.get("runningCount", 0) for d in deps if d is not primary)
        new = f"new task {primary.get('runningCount', 0)}/{primary.get('desiredCount', 1)} running"
        if old:
            return False, f"iron-proxy restarting: {new}, {old} old task(s) still serving the cached key"
        # Measured: the old task keeps answering some connections while the NLB drains it, until the
        # rollout completes (about 3 minutes in all).
        return False, f"iron-proxy restarting: {new}, old task draining (it can still swap the key until it stops)"

    def _reload_progress(self, op: dict) -> tuple[bool, str]:
        try:
            if self.policy.config_applied(op["config_version"]):
                return True, "iron-proxy reloaded its policy"
            st = self.policy.status(max_age=1.0)
        except Exception as e:
            return False, f"can't read the control sidecar's status ({type(e).__name__})"
        if st.get("error"):
            return False, st["error"]
        return False, f"waiting for iron-proxy to reload config v{op['config_version']}"

    def _overlay(self, item: dict, swaps: list[dict]) -> dict:
        name, policy = item["name"], item["revoked"]
        with self._lock:
            op = dict(self._ops.get(name) or {})
        status, detail = ("revoked" if policy else "active" if policy is False else "unknown"), None
        since = None
        if op.get("op") == "revoke":
            if policy is False:                      # restored meanwhile
                self._end(name, op)
            else:
                done, detail = (self._reload_progress(op) if op.get("method") == "reload" else self._revoke_progress(op))
                if done:
                    self._end(name, op)
                    log.info("revoke %s effective after %.0f s", name, time.time() - op["since"])
                else:
                    status, since = "revoking", op["since"]
        elif op.get("op") == "restore":
            seen = any(name in e["secrets"] and (e.get("_ms") or 0) >= op["since"] * 1000 for e in swaps)
            reloaded = op.get("method") != "reload" or self._reload_progress(op)[0]
            if policy:                               # revoked again meanwhile
                self._end(name, op)
            elif reloaded and (seen or time.time() - op["since"] > RESTORE_GIVE_UP_S):
                self._end(name, op)
                detail = None if seen else "no swap seen yet; the next request should get the real key"
                log.info("restore %s settled after %.0f s (swap seen: %s)", name, time.time() - op["since"], seen)
            else:
                status, since = "restoring", op["since"]
                detail = ("waiting for iron-proxy to reload its policy" if not reloaded else
                          "waiting for iron-proxy to fetch the key again (no swap of it seen since the restore)")
        out = dict(item)
        out["status"] = status
        out["status_detail"] = detail
        out["status_since"] = ms_iso(since * 1000) if since else None
        if status == "revoking":
            out["revoked"] = "pending"
        elif status == "restoring":
            out["revoked"] = False
        return out

    def _end(self, name: str, op: dict) -> None:
        with self._lock:
            if self._ops.get(name, {}).get("since") == op.get("since"):
                self._ops.pop(name, None)

    # ---- usage ----
    def invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._summary = None

    def _swaps(self) -> list[dict]:
        """Proxy events with a swapped secret in the last 15 minutes (cached 10 s)."""
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < CACHE_S:
                return self._cache[1]
        out = []
        if self.s.proxy_log_group:
            kw = {"logGroupName": log_group_name(self.s.proxy_log_group), "filterPattern": '"swapped"',
                  "startTime": int((time.time() - WINDOW_S) * 1000)}
            n, deadline = 0, time.monotonic() + PAGE_BUDGET_S
            while n < MAX_EVENTS and time.monotonic() < deadline:
                r = self.logs.filter_log_events(**kw)
                for e in r.get("events", []):
                    n += 1
                    ev = parse_line(e.get("message", ""), self.names, e.get("timestamp"))
                    if ev and ev["type"] == "proxy" and ev["secrets"]:
                        ev["_ms"] = e.get("timestamp")
                        out.append(ev)
                if not r.get("nextToken"):
                    break
                kw["nextToken"] = r["nextToken"]
        with self._lock:
            self._cache = (time.monotonic(), out)
        return out

    def summary(self) -> list[dict]:
        with self._lock:
            hit = self._summary if self._summary and time.monotonic() - self._summary[0] < CACHE_S else None
        if hit:
            base = hit[1]
        else:
            base = self._compute()
            with self._lock:
                self._summary = (time.monotonic(), base)
        swaps = self._swaps()
        return [self._overlay(item, swaps) for item in base]

    def _compute(self) -> list[dict]:
        swaps = self._swaps()
        result = []
        for name, c in self.catalog().items():
            arn = c["arn"]
            mine = [e for e in swaps if name in e["secrets"]]
            hosts: dict[str, int] = {}
            for e in mine:
                hosts[e["host"]] = hosts.get(e["host"], 0) + 1
            last = max((e["_ms"] for e in mine if e.get("_ms")), default=None)
            try:
                revoked = self.is_revoked(arn)
            except ClientError as e:
                log.warning("get_resource_policy %s: %s", name, e.response.get("Error", {}).get("Code"))
                revoked = None
            ph = str(c.get("placeholder") or "")
            result.append({"name": name, "kind": c.get("kind", "builtin"),
                           "placeholder_prefix": (ph[:14] + "…") if len(ph) > 14 else ph,
                           "revoked": revoked, "uses_15m": len(mine), "hosts": hosts, "last_used": ms_iso(last)})
        return result
