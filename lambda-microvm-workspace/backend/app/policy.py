"""The proxy control plane: iron-proxy's policy in part 2's control bucket, edited by admins from the workspace.

    s3://<bucket>/iron-proxy.yaml    iron-proxy's config. We change only the allowlist's domains, the secrets list
                                     and aws_auth.allowed_services; comments and everything else stay as they are.
    s3://<bucket>/aws-scope.json     the session policy statements for sandbox AWS calls
    s3://<bucket>/credentials.json   our metadata for the credentials we added (env var, who, when; no values)
    s3://<bucket>/status.json        written by part 2's control sidecar: what it applied, and when

Every write reads the object, edits it, and puts it back with If-Match on the ETag it read, so two admins can't
silently overwrite each other. The config's first line carries its version (`# my2cents control plane: config
v13, <when>, <who>`); the sidecar reports the versions it applied after iron-proxy's POST /v1/reload.

Secret values only ever go to Secrets Manager (create_secret / put_secret_value). Nothing here reads one."""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import re
import secrets as pysecrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Callable

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as Q

from . import aws_catalog as cat
from . import aws_endpoints
from .aws_reference import TTLCache, Unavailable, fetch_reference
from .aws_roles import fetch_roles
from .config import Settings

log = logging.getLogger("policy")

HEADER_RE = re.compile(r"^# my2cents control plane: config v(\d+)\b")
SCOPE_NOTE = "Changes apply to every sandbox on this proxy, through a reload: no restart."
STALE_S = 90
RECOVERY_DAYS = 7
SECRET_TTL = "30s"
# A custom credential's failed fetch (e.g. while revoked) is retried after 5 s instead of iron-proxy's default
# minute, so a restore takes effect within seconds of the reload.
SECRET_FAILURE_TTL = "5s"

BUILTIN_ENV = {"anthropic": "ANTHROPIC_API_KEY", "github": "GH_TOKEN", "demo": "DEMO_TOKEN"}

# Wildcards over these would cover sites run by unrelated parties
PUBLIC_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "ne.jp",
    "com.br", "com.cn", "com.mx", "co.in", "co.za", "com.sg", "com.tr", "co.kr", "com.tw", "com.hk",
    "github.io", "gitlab.io", "herokuapp.com", "appspot.com", "cloudfront.net", "azurewebsites.net",
    "blob.core.windows.net", "vercel.app", "netlify.app", "pages.dev", "workers.dev", "web.app",
    "firebaseapp.com", "fly.dev", "onrender.com", "ngrok.io", "ngrok-free.app", "lambda-url.on.aws", "on.aws",
}
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
RESERVED_ENV = {
    "PATH", "HOME", "USER", "SHELL", "TERM", "PWD", "LANG", "HOSTNAME", "TZ", "IFS", "PS1", "ENV", "PROMPT_COMMAND",
    "TMUX", "SANDBOX_NAME", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY", "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "GH_TOKEN", "GITHUB_TOKEN", "DEMO_TOKEN", "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
}
RESERVED_ENV_PREFIXES = ("AWS_", "LD_", "BASH_", "LC_", "PYTHON", "CLAUDE_", "XDG_")
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer",
               "proxy-authorization", "proxy-authenticate", "keep-alive"}


# ---------------------------------------------------------------- errors

class PolicyError(Exception):
    status = 422

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class Invalid(PolicyError):
    status = 422


class Conflict(PolicyError):
    status = 409


class Missing(PolicyError):
    status = 404


class Disabled(PolicyError):
    status = 503


def now_iso(t: float | None = None) -> str:
    d = datetime.fromtimestamp(time.time() if t is None else t, tz=timezone.utc)
    return d.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(v) -> float | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _ascii(v: str) -> str:
    return v.encode("ascii", "replace").decode()


# ---------------------------------------------------------------- hosts

def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return False


def is_aws_host(h: str) -> bool:
    base = h[2:] if h.startswith("*.") else h
    return base == "amazonaws.com" or base.endswith(".amazonaws.com")


def normalize_host(raw, *, allow_aws: bool = False) -> str:
    """One allowlist entry: a host name, or `*.` plus at least two labels that aren't a public suffix.
    iron-proxy's glob also matches the bare domain for `*.example.com`."""
    h = str(raw or "").strip().lower()
    if h.endswith("."):
        h = h[:-1]
    if not h:
        raise Invalid("enter a host name")
    if "://" in h or "/" in h or "?" in h or "#" in h or "@" in h:
        raise Invalid("a host name only, without a scheme, user or path")
    if _is_ip(h):
        raise Invalid("IP addresses aren't allowed; use a host name")
    if ":" in h:
        raise Invalid("no port: the proxy applies the allowlist on every port it serves")
    if h == "*" or h == "*.":
        raise Invalid("a bare * would allow every host")
    wildcard = h.startswith("*.")
    base = h[2:] if wildcard else h
    if "*" in base:
        raise Invalid("a wildcard can only be a leading *. (as in *.example.com)")
    labels = base.split(".")
    if wildcard and len(labels) < 2:
        raise Invalid(f"{h} would allow a whole top-level domain")
    if len(labels) < 2:
        raise Invalid("use a fully qualified name such as example.com")
    if len(h) > 253 or not all(LABEL_RE.match(lb) for lb in labels) or labels[-1].isdigit():
        raise Invalid("not a valid host name")
    if wildcard and base in PUBLIC_SUFFIXES:
        raise Invalid(f"{h} would cover a public suffix that many unrelated sites share")
    if not allow_aws and is_aws_host(h):
        raise Invalid("AWS endpoints follow the services chosen under AWS access")
    return h


def glob_match(pattern: str, host: str) -> bool:
    """iron-proxy's hostmatch.MatchGlob for the patterns we allow."""
    pattern, host = pattern.lower(), host.lower()
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) or host == pattern[2:]
    return pattern == host


# ---------------------------------------------------------------- the config document

def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def _flow(items) -> CommentedSeq:
    seq = CommentedSeq([Q(str(i)) for i in items])
    seq.fa.set_flow_style()
    return seq


class ConfigDoc:
    """iron-proxy.yaml, round-tripped with comments. Only three things are ever edited."""

    def __init__(self, text: str):
        lines = text.splitlines(keepends=True)
        self.version = 0
        while lines and (m := HEADER_RE.match(lines[0])):
            self.version = max(self.version, int(m.group(1)))
            lines.pop(0)
        self.data = _yaml().load("".join(lines))
        if not isinstance(self.data, CommentedMap) or not isinstance(self.data.get("transforms"), list):
            raise PolicyError("the config in S3 has no transforms list")

    def _transform(self, name: str, create: bool = False) -> CommentedMap | None:
        for t in self.data["transforms"]:
            if isinstance(t, dict) and t.get("name") == name:
                if not isinstance(t.get("config"), dict):
                    t["config"] = CommentedMap()
                return t["config"]
        if not create:
            return None
        raise PolicyError(f"the config has no {name} transform")

    # ---- allowlist
    @property
    def domains(self) -> CommentedSeq:
        cfg = self._transform("allowlist", create=True)
        if not isinstance(cfg.get("domains"), list):
            cfg["domains"] = CommentedSeq()
        return cfg["domains"]

    def hosts(self) -> list[str]:
        return [str(d) for d in self.domains]

    def add_host(self, host: str) -> None:
        d = self.domains
        # Keep the AWS endpoints together at the end, where AWS access manages them
        at = next((i for i, h in enumerate(d) if is_aws_host(str(h))), len(d))
        d.insert(at, Q(host))

    def remove_host(self, host: str) -> None:
        d = self.domains
        for i, h in enumerate(d):
            if str(h).lower() == host:
                del d[i]
                return
        raise Missing(f"{host} isn't on the allowlist")

    def set_aws_hosts(self, hosts: list[str], region: str = "us-east-1") -> None:
        d = self.domains
        for i in reversed(range(len(d))):
            if is_aws_host(str(d[i])):
                del d[i]
        # The Secrets Manager guard, again where the config is written: no endpoint of it, ever
        for h in cat.guard_hosts(hosts, region):
            d.append(Q(h))

    # ---- aws_auth
    def allowed_services(self) -> list[str]:
        cfg = self._transform("aws_auth")
        return [str(x) for x in (cfg or {}).get("allowed_services") or []]

    def set_allowed_services(self, names: list[str]) -> None:
        cfg = self._transform("aws_auth", create=True)
        # Never secretsmanager, never empty (iron-proxy 0.55.0 reads an empty list as every service)
        cfg["allowed_services"] = _flow(cat.guard_services(names))

    # ---- secrets
    @property
    def secrets(self) -> CommentedSeq:
        cfg = self._transform("secrets", create=True)
        if not isinstance(cfg.get("secrets"), list):
            cfg["secrets"] = CommentedSeq()
        return cfg["secrets"]

    def secret_entries(self) -> list[dict]:
        """[{secret_id, placeholder, headers, query, body, hosts}] for every replace-mode secret."""
        out = []
        for e in self.secrets:
            if not isinstance(e, dict):
                continue
            src = e.get("source") or {}
            rep = e.get("replace") or {}
            out.append({"secret_id": str(src.get("secret_id") or ""), "placeholder": str(rep.get("proxy_value") or ""),
                        "headers": [str(h) for h in rep.get("match_headers") or []],
                        "query": bool(rep.get("match_query")), "body": bool(rep.get("match_body")),
                        "hosts": [str(r.get("host")) for r in e.get("rules") or [] if isinstance(r, dict) and r.get("host")]})
        return out

    def add_secret(self, arn: str, placeholder: str, hosts: list[str], headers: list[str], query: bool, body: bool) -> None:
        source = CommentedMap([("type", "aws_sm"), ("secret_id", Q(arn)),
                               ("ttl", Q(SECRET_TTL)), ("failure_ttl", Q(SECRET_FAILURE_TTL))])
        replace = CommentedMap([("proxy_value", Q(placeholder)), ("match_headers", _flow(headers))])
        if query:
            replace["match_query"] = True
        if body:
            replace["match_body"] = True
        rules = CommentedSeq([CommentedMap([("host", Q(h))]) for h in hosts])
        self.secrets.append(CommentedMap([("source", source), ("replace", replace), ("rules", rules)]))

    def remove_secret(self, arn: str) -> None:
        s = self.secrets
        for i, e in enumerate(s):
            if isinstance(e, dict) and str((e.get("source") or {}).get("secret_id") or "") == arn:
                del s[i]
                return
        raise Missing("that credential isn't in the proxy config")

    def dump(self, version: int, at: str, actor: str) -> str:
        out = io.StringIO()
        _yaml().dump(self.data, out)
        return f"# my2cents control plane: config v{version}, {at}, {_ascii(actor)}\n{out.getvalue()}"


# ---------------------------------------------------------------- S3 objects

class ControlStore:
    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket

    def get(self, key: str) -> tuple[bytes, str, dict] | None:
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return r["Body"].read(), r["ETag"], r.get("Metadata") or {}

    def get_json(self, key: str) -> tuple[dict, str | None]:
        got = self.get(key)
        if not got:
            return {}, None
        try:
            d = json.loads(got[0])
        except ValueError:
            d = {}
        return (d if isinstance(d, dict) else {}), got[1]

    def head(self, key: str) -> dict | None:
        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        return {"etag": r["ETag"], "metadata": r.get("Metadata") or {}}

    def put(self, key: str, body: str, content_type: str, *, if_match: str | None = None, create: bool = False,
            metadata: dict | None = None) -> str:
        kw = {"Bucket": self.bucket, "Key": key, "Body": body.encode(), "ContentType": content_type}
        if if_match:
            kw["IfMatch"] = if_match
        elif create:
            kw["IfNoneMatch"] = "*"
        if metadata:
            kw["Metadata"] = {k: _ascii(str(v)) for k, v in metadata.items()}
        try:
            return self.s3.put_object(**kw)["ETag"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
                raise Conflict("the policy changed meanwhile; reload and retry") from e
            raise


# ---------------------------------------------------------------- change log

def _entry(actor, kind, summary, detail, config_version, scope_version, t: float | None = None) -> dict:
    at = now_iso(t)
    return {"id": f"{at}#{pysecrets.token_hex(3)}", "at": at, "actor": actor, "kind": kind, "summary": summary,
            "detail": detail or {}, "config_version": config_version, "scope_version": scope_version}


class MemoryChangeLog:
    def __init__(self):
        self.items: list[dict] = []
        self._lock = threading.Lock()

    def record(self, actor, kind, summary, detail, config_version, scope_version) -> dict:
        e = _entry(actor, kind, summary, detail, config_version, scope_version)
        with self._lock:
            self.items.append(e)
        return e

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return sorted(self.items, key=lambda e: e["id"], reverse=True)[:limit]


class DynamoChangeLog:
    """pk "proxy", sk "<at>#<rand>"; detail stored as JSON text. Entries expire after a year."""

    PK = "proxy"

    def __init__(self, table: str, region: str, resource=None):
        res = resource or boto3.resource("dynamodb", config=Config(region_name=region, retries={"mode": "standard"}))
        self.table = res.Table(table)

    def record(self, actor, kind, summary, detail, config_version, scope_version) -> dict:
        e = _entry(actor, kind, summary, detail, config_version, scope_version)
        item = {"pk": self.PK, "sk": e["id"], "at": e["at"], "actor": actor, "kind": kind, "summary": summary,
                "detail": json.dumps(e["detail"], separators=(",", ":")), "ttl": int(time.time()) + 365 * 86400}
        for k in ("config_version", "scope_version"):
            if e[k] is not None:
                item[k] = e[k]
        self.table.put_item(Item=item)
        return e

    def recent(self, limit: int = 50) -> list[dict]:
        r = self.table.query(KeyConditionExpression=Key("pk").eq(self.PK),
                             ScanIndexForward=False, Limit=limit)
        out = []
        for i in r.get("Items", []):
            try:
                detail = json.loads(i.get("detail") or "{}")
            except ValueError:
                detail = {}
            out.append({"id": i["sk"], "at": i.get("at"), "actor": i.get("actor"), "kind": i.get("kind"),
                        "summary": i.get("summary"), "detail": detail,
                        "config_version": int(i["config_version"]) if isinstance(i.get("config_version"), Decimal) else i.get("config_version"),
                        "scope_version": int(i["scope_version"]) if isinstance(i.get("scope_version"), Decimal) else i.get("scope_version")})
        return out


# ---------------------------------------------------------------- the AWS access catalog

def default_catalog(s: Settings) -> cat.Catalog:
    """The Service Authorization Reference and the sandbox roles' policies, each cached for an hour."""
    iam = boto3.client("iam", config=Config(region_name=s.region, retries={"max_attempts": 3, "mode": "standard"}))
    cache = Path(s.catalog_cache_dir) if s.catalog_cache_dir else None
    reference = TTLCache("service reference", lambda: fetch_reference(s.service_reference_url), ttl=s.catalog_ttl_s,
                         disk=cache / "reference.json.gz" if cache else None)
    roles = TTLCache("sandbox roles", lambda: fetch_roles(iam, s.sandbox_roles), ttl=s.catalog_ttl_s,
                     disk=cache / "roles.json.gz" if cache else None)
    return cat.Catalog(reference, roles, s.sandbox_roles, aws_endpoints.load()["clients"], s.region, s.scope_baseline)


# ---------------------------------------------------------------- the control plane

class ProxyPolicy:
    def __init__(self, s: Settings, *, s3=None, sm=None, changes=None, payload_size: Callable[[dict], int] | None = None,
                 catalog: cat.Catalog | None = None):
        self.s = s
        self.enabled = s.control_plane
        cfg = Config(region_name=s.region, retries={"max_attempts": 3, "mode": "standard"})
        self.store = ControlStore(s3 or boto3.client("s3", config=cfg), s.control_bucket or "") if self.enabled else None
        self.sm = sm or (boto3.client("secretsmanager", config=cfg) if self.enabled else None)
        if changes is not None:
            self.changes = changes
        elif s.changes_table:
            self.changes = DynamoChangeLog(s.changes_table, s.region)
        else:
            self.changes = MemoryChangeLog()
        # Size of a new sandbox's run hook payload with these extra env vars (set by main.py)
        self.payload_size = payload_size or (lambda extra: 0)
        self.keys = s.control_keys
        self._lock = threading.Lock()
        self._index_cache: tuple[float, dict] | None = None
        self._status_cache: tuple[float, dict] | None = None
        self.builtin_arns = dict(s.secret_arns)
        self.aws_catalog = catalog if catalog is not None else (default_catalog(s) if self.enabled else None)

    # ---- plumbing
    def _need(self) -> None:
        if not self.enabled:
            raise Disabled(self.disabled_reason())

    @staticmethod
    def disabled_reason() -> str:
        return ("The proxy control plane is off. Apply part 2 with control_plane=true, then redeploy the workspace, "
                "to edit iron-proxy's policy here.")

    def _load_config(self) -> tuple[ConfigDoc, str]:
        got = self.store.get(self.keys["config"])
        if not got:
            raise PolicyError("the control bucket has no iron-proxy.yaml")
        return ConfigDoc(got[0].decode()), got[1]

    def _scope(self) -> tuple[dict, str | None]:
        return self.store.get_json(self.keys["scope"])

    def _index(self) -> tuple[dict, str | None]:
        d, etag = self.store.get_json(self.keys["credentials"])
        creds = d.get("credentials") if isinstance(d.get("credentials"), dict) else {}
        return {"credentials": creds}, etag

    def _edit(self, user, mutate: Callable[[ConfigDoc], object], kind: str, summary: str, detail: dict,
              scope_version: int | None = None) -> tuple[object, dict, dict]:
        """Read, mutate, write back with If-Match; one retry on a concurrent edit; record the change."""
        for attempt in range(2):
            doc, etag = self._load_config()
            result = mutate(doc)
            version, at = doc.version + 1, now_iso()
            body = doc.dump(version, at, user.actor)
            try:
                self.store.put(self.keys["config"], body, "application/yaml", if_match=etag,
                               metadata={"version": version, "updated-at": at, "updated-by": user.actor})
                break
            except Conflict:
                if attempt:
                    raise
        if scope_version is None:
            scope_version = int(self._scope()[0].get("version") or 0)
        change = self.changes.record(user.actor, kind, summary, detail, version, scope_version)
        self._status_cache = None
        log.info("%s by %s: config v%s", kind, user.actor, version)
        return result, change, {"config_version": version, "scope_version": scope_version, "requested_at": at}

    # ---- overview
    def overview(self, user, is_admin: bool) -> dict:
        if not self.enabled:
            return {"enabled": False, "editable": False, "reason": self.disabled_reason(), "note": SCOPE_NOTE}
        doc, _ = self._load_config()
        scope, _ = self._scope()
        return {"enabled": True, "editable": is_admin, "note": SCOPE_NOTE, "config_version": doc.version,
                "scope_version": int(scope.get("version") or 0), "region": self.s.region,
                "limits": {"policy_chars": cat.POLICY_CHARS, "run_payload_bytes": 4096,
                           "run_payload_bytes_used": self.payload_size(self.custom_env())}}

    # ---- allowlist
    def allowlist(self) -> dict:
        self._need()
        doc, _ = self._load_config()
        services = self.aws_catalog.host_owner() if self.aws_catalog else {}
        hosts = []
        for h in doc.hosts():
            if is_aws_host(h):
                hosts.append({"host": h, "managed": "aws", "service": services.get(h)})
            else:
                hosts.append({"host": h, "managed": None})
        return {"version": doc.version, "hosts": hosts}

    def add_host(self, user, raw: str) -> dict:
        self._need()
        host = normalize_host(raw)

        def mutate(doc: ConfigDoc):
            if host in [h.lower() for h in doc.hosts()]:
                raise Conflict(f"{host} is already on the allowlist")
            doc.add_host(host)
        with self._lock:
            _, change, apply = self._edit(user, mutate, "allowlist.add", f"Allowed {host}", {"host": host})
        return {"version": apply["config_version"], "host": host, "change": change, "apply": apply}

    def remove_host(self, user, raw: str) -> dict:
        self._need()
        host = str(raw or "").strip().lower().rstrip(".")
        if is_aws_host(host):
            raise Conflict("AWS endpoints follow the services chosen under AWS access")

        def mutate(doc: ConfigDoc):
            doc.remove_host(host)
        with self._lock:
            _, change, apply = self._edit(user, mutate, "allowlist.remove", f"Removed {host}", {"host": host})
        return {"version": apply["config_version"], "host": host, "change": change, "apply": apply}

    # ---- credentials
    def _custom_name(self, arn: str) -> str | None:
        marker = f":secret:{self.s.user_secret_prefix}"
        if marker not in arn:
            return None
        rest = arn.split(marker, 1)[1]
        return rest[:-7] if len(rest) > 7 and rest[-7] == "-" else rest

    def credential_catalog(self) -> dict[str, dict]:
        """name -> {arn, placeholder, kind, env}: built-ins from part 2, custom ones from the index (cached 5 s).
        Used by the audit parser, revoke/restore and new sandboxes; works when the control plane is off too."""
        out = {n: {"arn": a, "placeholder": str(self.s.placeholders.get(n) or ""), "kind": "builtin",
                   "env": BUILTIN_ENV.get(n)} for n, a in self.builtin_arns.items()}
        if not self.enabled:
            return out
        now = time.monotonic()
        if self._index_cache and now - self._index_cache[0] < 5:
            idx = self._index_cache[1]
        else:
            try:
                idx = self._index()[0]
            except Exception as e:   # the feed and sandbox creation must keep working
                log.warning("credentials index: %s", e)
                idx = self._index_cache[1] if self._index_cache else {"credentials": {}}
            self._index_cache = (now, idx)
        for name, c in idx["credentials"].items():
            if c.get("secret_arn"):
                out[name] = {"arn": c["secret_arn"], "placeholder": c.get("placeholder") or "", "kind": "custom",
                             "env": c.get("env")}
        return out

    def custom_env(self) -> dict[str, str]:
        return {c["env"]: c["placeholder"] for c in self.credential_catalog().values()
                if c["kind"] == "custom" and c.get("env") and c.get("placeholder")}

    def list_credentials(self) -> list[dict]:
        self._need()
        doc, _ = self._load_config()
        idx = self._index()[0]["credentials"]
        allow = doc.hosts()
        by_arn = {arn: name for name, arn in self.builtin_arns.items()}
        builtins, custom = [], []
        for e in doc.secret_entries():
            arn = e["secret_id"]
            name = by_arn.get(arn) or self._custom_name(arn)
            if not name:
                continue
            kind = "builtin" if arn in by_arn else "custom"
            meta = idx.get(name) or {}
            item = {"name": name, "kind": kind, "env": BUILTIN_ENV.get(name) if kind == "builtin" else meta.get("env"),
                    "placeholder": e["placeholder"], "hosts": e["hosts"], "headers": e["headers"], "query": e["query"],
                    "body": e["body"], "secret_name": (self.s.user_secret_prefix + name) if kind == "custom" else None,
                    "on_allowlist": all(any(glob_match(a, h.removeprefix("*.")) for a in allow) for h in e["hosts"]),
                    "created_at": meta.get("created_at"), "created_by": meta.get("created_by"),
                    "rotated_at": meta.get("rotated_at"), "rotated_by": meta.get("rotated_by"),
                    "rotatable": kind == "custom", "deletable": kind == "custom"}
            (builtins if kind == "builtin" else custom).append(item)
        order = list(self.builtin_arns)
        builtins.sort(key=lambda i: order.index(i["name"]) if i["name"] in order else 99)
        return builtins + sorted(custom, key=lambda i: i["name"])

    def _validate_new(self, body: dict) -> dict:
        name = str(body.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise Invalid("name: 2 to 32 lower-case letters, digits or dashes, starting with a letter")
        env = str(body.get("env") or "").strip()
        if not ENV_RE.match(env):
            raise Invalid("env: an upper-case shell variable name such as TEST_KEY")
        if env in RESERVED_ENV or env.startswith(RESERVED_ENV_PREFIXES):
            raise Invalid(f"env: {env} is reserved (proxy, AWS, shell or Claude Code settings)")
        hosts = []
        for h in body.get("hosts") or []:
            h = normalize_host(h)
            if h not in hosts:
                hosts.append(h)
        if not 1 <= len(hosts) <= 10:
            raise Invalid("hosts: 1 to 10 host names")
        headers = []
        for h in body.get("headers") or []:
            h = str(h).strip()
            if not HEADER_NAME_RE.match(h):
                raise Invalid(f"headers: {h[:40]!r} isn't a header name")
            if h.lower() in HOP_HEADERS:
                raise Invalid(f"headers: {h} can't carry a credential")
            if h.lower() not in [x.lower() for x in headers]:
                headers.append(h)
        if len(headers) > 10:
            raise Invalid("headers: at most 10")
        return {"name": name, "env": env, "hosts": hosts, "headers": headers,
                "query": bool(body.get("query")), "body": bool(body.get("body"))}

    @staticmethod
    def _check_value(value) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 8192:
            raise Invalid("value: 1 to 8,192 characters")
        if "\r" in value or "\n" in value:
            raise Invalid("value: no line breaks (it goes into HTTP headers)")
        return value

    def _put_new_secret(self, name: str, value: str, actor: str) -> tuple[str, bool]:
        """Create my2cents-ip/user/<name>, or restore and overwrite one still in its recovery window.
        Returns (arn, created)."""
        secret_name = self.s.user_secret_prefix + name
        try:
            r = self.sm.create_secret(Name=secret_name, SecretString=value,
                                      Description=f"Injected by iron-proxy for workspace sandboxes; added by {_ascii(actor)}",
                                      Tags=[{"Key": "my2cents:credential", "Value": name}])
            return r["ARN"], True
        except ClientError as e:
            code, msg = e.response.get("Error", {}).get("Code"), e.response.get("Error", {}).get("Message") or ""
            pending = code == "InvalidRequestException" and "deletion" in msg
            if code == "ResourceExistsException":
                d = self.sm.describe_secret(SecretId=secret_name)
                pending = bool(d.get("DeletedDate"))
                if not pending:
                    raise Conflict(f"a secret named {secret_name} already exists; pick another name") from e
            if not pending:
                raise
        self.sm.restore_secret(SecretId=secret_name)
        r = self.sm.put_secret_value(SecretId=secret_name, SecretString=value)
        return r["ARN"], False

    def create_credential(self, user, body: dict) -> dict:
        self._need()
        spec = self._validate_new(body)
        value = self._check_value(body.get("value"))
        name = spec["name"]
        with self._lock:
            creds = self._index()[0]["credentials"]
            if name in self.builtin_arns or name in creds:
                raise Conflict(f"a credential named {name} already exists")
            used_env = set(BUILTIN_ENV.values()) | {c.get("env") for c in creds.values()}
            if spec["env"] in used_env:
                raise Conflict(f"env: {spec['env']} is already used by another credential")
            placeholder = f"m2c-ph-{name}-{pysecrets.token_hex(8)}"
            extra = {**self.custom_env(), spec["env"]: placeholder}
            size = self.payload_size(extra)
            if size > 4096:
                raise Conflict(f"a new sandbox's run hook payload would be {size:,} bytes (limit 4,096); "
                               "delete a custom credential first")
            arn, created = self._put_new_secret(name, value, user.actor)
            value = None  # noqa: F841  (not kept past this point)
            try:
                def mutate(doc: ConfigDoc):
                    if any(e["secret_id"] == arn for e in doc.secret_entries()):
                        doc.remove_secret(arn)
                    doc.add_secret(arn, placeholder, spec["hosts"], spec["headers"], spec["query"], spec["body"])
                    return [h for h in spec["hosts"] if not any(glob_match(a, h.removeprefix("*.")) for a in doc.hosts())]
                detail = {k: spec[k] for k in ("name", "env", "hosts", "headers", "query", "body")}
                off_list, change, apply = self._edit(user, mutate, "credential.create",
                                                     f"Added credential {name} for {', '.join(spec['hosts'])}", detail)
            except Exception:
                if created:
                    self.sm.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
                else:
                    self.sm.delete_secret(SecretId=arn, RecoveryWindowInDays=RECOVERY_DAYS)
                raise
            meta = {"env": spec["env"], "placeholder": placeholder, "secret_arn": arn,
                    "secret_name": self.s.user_secret_prefix + name, "created_at": change["at"],
                    "created_by": user.actor, "rotated_at": None, "rotated_by": None}
            self._write_index(lambda c: c.__setitem__(name, meta))
        warnings = [f"{h} isn't on the allowlist: iron-proxy refuses it before it could swap anything" for h in off_list]
        cred = next((c for c in self.list_credentials() if c["name"] == name), None)
        return {"credential": cred, "change": change, "apply": apply, "warnings": warnings}

    def _write_index(self, fn: Callable[[dict], None]) -> None:
        """Apply fn to the credentials map and write it back (If-Match; fn is re-applied on a concurrent edit)."""
        for attempt in range(3):
            idx, etag = self._index()
            fn(idx["credentials"])
            try:
                self.store.put(self.keys["credentials"], json.dumps(idx, indent=1, sort_keys=True), "application/json",
                               if_match=etag, create=etag is None)
                self._index_cache = None
                return
            except Conflict:
                if attempt == 2:
                    raise

    def _custom(self, name: str) -> dict:
        if name in self.builtin_arns:
            raise Conflict(f"{name} is a built-in credential: its value comes from part 2's .env")
        c = self._index()[0]["credentials"].get(name)
        if not c:
            raise Missing("unknown credential")
        return c

    def rotate(self, user, name: str, value) -> dict:
        self._need()
        value = self._check_value(value)
        with self._lock:
            c = self._custom(name)
            self.sm.put_secret_value(SecretId=c["secret_arn"], SecretString=value)
            value = None  # noqa: F841
            at = now_iso()
            self._write_index(lambda cs: cs[name].update(rotated_at=at, rotated_by=user.actor) if name in cs else None)
            doc, _ = self._load_config()
            scope = int(self._scope()[0].get("version") or 0)
            change = self.changes.record(user.actor, "credential.rotate", f"Rotated {name}", {"name": name},
                                         doc.version, scope)
        log.info("credential.rotate by %s: %s", user.actor, name)
        return {"name": name, "rotated_at": at, "effective_within_s": 30, "change": change}

    def delete_credential(self, user, name: str) -> dict:
        self._need()
        with self._lock:
            c = self._custom(name)
            arn = c["secret_arn"]

            def mutate(doc: ConfigDoc):
                try:
                    doc.remove_secret(arn)
                except Missing:
                    pass
            _, change, apply = self._edit(user, mutate, "credential.delete", f"Deleted credential {name}", {"name": name})
            self._write_index(lambda cs: cs.pop(name, None))
            try:
                self.sm.delete_secret(SecretId=arn, RecoveryWindowInDays=RECOVERY_DAYS)
            except ClientError as e:
                log.warning("delete_secret %s: %s", name, e.response.get("Error", {}).get("Code"))
        return {"name": name, "deleted": True, "recovery_window_days": RECOVERY_DAYS, "change": change, "apply": apply}

    def bump(self, user, kind: str, summary: str, detail: dict) -> dict:
        """A new config version with no other change: the sidecar reloads iron-proxy, which rebuilds its secrets
        cache. Revoke and restore use it instead of restarting the proxy."""
        self._need()
        with self._lock:
            _, change, apply = self._edit(user, lambda doc: None, kind, summary, detail)
        return {"change": change, "apply": apply}

    # ---- AWS access
    def _catalog_call(self, fn, *a):
        self._need()
        try:
            return fn(*a)
        except Unavailable as e:
            raise Disabled(f"the AWS Service Authorization Reference isn't available: {e}") from e
        except cat.CatalogError as e:
            raise Invalid(f"actions: {e}") from e

    def catalog(self, role: str | None = None) -> dict:
        return self._catalog_call(self.aws_catalog.listing, role)

    def aws_service(self, sid: str, role: str | None = None) -> dict:
        out = self._catalog_call(self.aws_catalog.service, sid, role)
        if out is None:
            raise Missing(f"no service {sid!r} in the Service Authorization Reference")
        return out

    def aws_search(self, q: str, level: str | None, limit: int, role: str | None = None) -> dict:
        return self._catalog_call(self.aws_catalog.search, q, level, limit, role)

    def catalog_state(self) -> dict:
        """For the cache headers: when the reference was fetched and how old it is."""
        st = self.aws_catalog.reference.state() if self.aws_catalog else {}
        return {"fetched_at": st.get("fetched_at"), "age_s": st.get("age_s")}

    def preview(self, actions, role: str | None = None, session_policy: bool = True) -> dict:
        return self._catalog_call(self.aws_catalog.preview, list(actions or []), role, bool(session_policy))

    @staticmethod
    def _scope_flags(d: dict, default_role: str) -> tuple[str, bool]:
        """The scope's role and session-policy flag; an older scope has neither (its statements meant "on")."""
        sp = d.get("session_policy")
        if sp is None:
            sp = bool(d.get("statements"))
        return d.get("role") or default_role, bool(sp)

    def scope(self) -> dict:
        self._need()
        d, _ = self._scope()
        actions = [a for a in d.get("actions") or [] if isinstance(a, str)]
        role, sp = self._scope_flags(d, self.aws_catalog.default_role)
        try:
            pv = self.aws_catalog.preview(actions, role, sp)
        except (cat.CatalogError, Unavailable):
            pv = None
        return {"version": int(d.get("version") or 0), "role": role, "session_policy": sp, "actions": actions,
                "services": sorted({a.split(":", 1)[0] for a in actions if ":" in a}),
                "updated_at": d.get("updated_at"), "updated_by": d.get("updated_by"), "preview": pv}

    def apply_scope(self, user, actions, role: str | None = None, session_policy: bool = True) -> dict:
        self._need()
        pv = self.preview(actions, role, session_policy)
        if not pv["fits"]:
            raise Invalid(f"the session policy is {pv['chars']:,} characters; STS allows {cat.POLICY_CHARS:,}. "
                          "Pick fewer actions or a wildcard.")
        role, sp = pv["role"], pv["session_policy"]
        stmts = cat.statements(pv["actions"]) if sp else []
        with self._lock:
            cur, etag = self._scope()
            cur_role, cur_sp = self._scope_flags(cur, self.aws_catalog.default_role)
            if (cur_role, cur_sp) == (role, sp) and sorted(cur.get("actions") or []) == pv["actions"] \
                    and (cur.get("statements") or []) == stmts and not cur.get("seed"):
                doc, _ = self._load_config()
                if doc.allowed_services() == pv["allowed_services"]:
                    v = int(cur.get("version") or 0)
                    return {"scope_version": v, "config_version": doc.version, "preview": pv, "change": None,
                            "apply": None, "unchanged": True}
            version, at = int(cur.get("version") or 0) + 1, now_iso()
            scope = {"version": version, "updated_at": at, "updated_by": user.actor, "role": role,
                     "session_policy": sp, "actions": pv["actions"], "services": pv["services"], "statements": stmts}
            self.store.put(self.keys["scope"], json.dumps(scope, indent=1), "application/json", if_match=etag,
                           create=etag is None, metadata={"version": version, "updated-at": at})

            def mutate(doc: ConfigDoc):
                doc.set_aws_hosts(pv["hosts"], self.s.region)
                doc.set_allowed_services(pv["allowed_services"])
            if sp:
                summary = (f"AWS access: {role}, session policy with {len(pv['actions'])} action(s) on "
                           f"{', '.join(pv['services']) or 'no service'}")
            else:
                summary = f"AWS access: {role}, no session policy ({len(pv['allowed_services'])} services through iron-proxy)"
            _, change, apply = self._edit(user, mutate, "aws.scope", summary,
                                          {"role": role, "session_policy": sp, "actions": pv["actions"] if sp else [],
                                           "allowed_services": len(pv["allowed_services"]), "chars": pv["chars"]},
                                          scope_version=version)
        apply["requested_at"] = at
        return {"scope_version": version, "config_version": apply["config_version"], "preview": pv, "change": change,
                "apply": apply, "unchanged": False}

    def seed_scope(self) -> bool:
        """Part 2 seeds the scope with the default role and no session policy (`seed: true`), but can't compute the
        matching allowed_services and AWS hosts (they come from the AWS catalog). Write them once, the first time the
        catalog is available. Returns True when it wrote."""
        if not self.enabled:
            return False
        d, _ = self._scope()
        if not d.get("seed"):
            return False
        role, sp = self._scope_flags(d, self.aws_catalog.default_role)
        self.apply_scope(_System("workspace (seed)"), d.get("actions") or [], role, sp)
        log.info("scope seeded: %s, session policy %s", role, "on" if sp else "off")
        return True

    # ---- status
    def status(self, max_age: float = 0.5) -> dict:
        """What the sidecar applied (status.json) against what's in the bucket now."""
        if not self.enabled:
            return {"enabled": False, "applied": False, "pending": False, "error": self.disabled_reason()}
        now = time.monotonic()
        if self._status_cache and now - self._status_cache[0] < max_age:
            return self._status_cache[1]
        head = self.store.head(self.keys["config"]) or {"metadata": {}}
        md = head["metadata"]
        cfg_v, cfg_at = md.get("version"), md.get("updated-at")
        if not cfg_v:  # a config written by hand: read the header
            cfg_v = self._load_config()[0].version
        scope, _ = self._scope()
        sc = self.store.get_json(self.keys["status"])[0] or {}
        target = {"config_version": int(cfg_v or 0), "scope_version": int(scope.get("version") or 0)}
        times = [t for t in (cfg_at, scope.get("updated_at")) if parse_iso(t)]
        target["requested_at"] = max(times, key=parse_iso) if times else None
        c, s_, r = sc.get("config") or {}, sc.get("scope") or {}, sc.get("reload") or {}
        updated = parse_iso(sc.get("updated_at"))
        age = round(time.time() - updated, 1) if updated else None
        stale = age is None or age > STALE_S
        applied = (bool(sc) and c.get("applied_version") == target["config_version"]
                   and s_.get("applied_version") == target["scope_version"] and r.get("ok") is True)
        error = None
        if c.get("refused_reason") and c.get("rejected_version") == target["config_version"]:
            # part 2's sidecar checks the Secrets Manager guard itself and doesn't apply a config that breaks it
            error = f"the control sidecar refused config v{c.get('rejected_version')}: {c['refused_reason']}"
        elif r.get("ok") is False and r.get("error"):
            error = f"iron-proxy rejected config v{c.get('rejected_version') or c.get('synced_version')}: {r['error']}"
        elif s_.get("error") and s_.get("synced_version") != target["scope_version"]:
            error = f"the sidecar couldn't assume the scoped role: {s_['error']}"
        elif not sc:
            error = "the control sidecar hasn't reported yet"
        latency = None
        req = parse_iso(target["requested_at"])
        done = [parse_iso(x) for x in (c.get("applied_at"), s_.get("applied_at")) if parse_iso(x)]
        if applied and req and done:
            latency = round(max(done) - req, 2)
            if latency < 0 or latency > 3600:
                latency = None
        out = {"enabled": True, "applied": applied, "pending": not applied and not error, "target": target,
               "latency_s": latency,
               "config": {"version": c.get("applied_version"), "synced_version": c.get("synced_version"),
                          "applied_at": c.get("applied_at"), "rejected_version": c.get("rejected_version")},
               "scope": {"version": s_.get("applied_version"), "role": s_.get("role"),
                         "session_policy": s_.get("session_policy"), "session_name": s_.get("session_name"),
                         "source_identity": s_.get("source_identity"), "expiration": s_.get("expiration"),
                         "assumed_at": s_.get("assumed_at"), "applied_at": s_.get("applied_at"),
                         # STS's AssumeRole answer: share of the maximum session token size, and its size in
                         # bytes; packed_policy_size is the deprecated predecessor, kept for older answers
                         "session_token_utilization": s_.get("session_token_utilization"),
                         "session_token_size": s_.get("session_token_size"),
                         "packed_policy_size": s_.get("packed_policy_size"), "error": s_.get("error")},
               "reload": {"at": r.get("at"), "ok": r.get("ok"), "http_status": r.get("http_status"), "error": r.get("error")},
               "sidecar": {"task": sc.get("task"), "updated_at": sc.get("updated_at"), "heartbeat_age_s": age,
                           "stale": stale},
               "error": error}
        if stale and not error:
            out["error"] = "the control sidecar hasn't written its status for over 90 s"
            out["pending"] = False
        self._status_cache = (now, out)
        return out

    def config_applied(self, version: int) -> bool:
        st = self.status(max_age=1.0)
        v = (st.get("config") or {}).get("version")
        return isinstance(v, int) and v >= version and (st.get("reload") or {}).get("ok") is True

    def recent_changes(self, limit: int = 50) -> list[dict]:
        self._need()
        return self.changes.recent(max(1, min(200, limit)))


class _System:
    """The backend acting on its own (the seed)."""

    def __init__(self, actor: str):
        self.actor = actor
