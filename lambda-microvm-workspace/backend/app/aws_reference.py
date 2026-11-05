"""Where the AWS access catalog comes from, cached for an hour:

- AWS's Service Authorization Reference (https://servicereference.us-east-1.amazonaws.com/): an index of ~460
  services, one JSON file each with its actions, their annotations (IsList, IsWrite, IsPermissionManagement,
  IsTaggingOnly) and the SDK operations that need them. About 17 MB in all; we keep a compact copy.
- The default version of AWS's ReadOnlyAccess policy, the ceiling of the role sandbox calls are signed with.

TTLCache serves the cached value for `ttl` seconds. After that it still serves it, flagged stale, while one
background thread refreshes it; a failed refresh keeps the old copy and records the error. Only a cold start
(nothing in memory or on disk) waits for the fetch. A copy on local disk survives a process restart."""

from __future__ import annotations

import concurrent.futures as cf
import gzip
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("aws_reference")

LEVELS = ("List", "Read", "Write", "Permissions", "Tagging")
REFERENCE_URL = "https://servicereference.us-east-1.amazonaws.com/"
READ_ONLY_ARN = "arn:aws:iam::aws:policy/ReadOnlyAccess"


def iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Unavailable(RuntimeError):
    """Nothing cached and the fetch failed."""


# ---------------------------------------------------------------- the reference

def access_level(annotations: dict | None) -> str:
    p = (annotations or {}).get("Properties") or {}
    if p.get("IsPermissionManagement"):
        return "Permissions"
    if p.get("IsTaggingOnly"):
        return "Tagging"
    if p.get("IsWrite"):
        return "Write"
    if p.get("IsList"):
        return "List"
    return "Read"


def boto3_clients(service: dict) -> list[str]:
    """The boto3 clients whose operations belong to this IAM prefix: an operation counts when one of its
    authorized actions has the operation's own name in this prefix, or its first authorized action is in it.
    (s3's file also lists s3control operations; sso's lists some that are s3control's.)"""
    prefix, out = service.get("Name"), set()
    for op in service.get("Operations") or []:
        aa = [a for a in op.get("AuthorizedActions") or [] if isinstance(a, dict)]
        own = any(a.get("Service") == prefix and a.get("Name") == op.get("Name") for a in aa)
        if own or (aa and aa[0].get("Service") == prefix):
            out |= {s["Name"] for s in op.get("SDK") or [] if s.get("Package") == "Boto3" and s.get("Name")}
    return sorted(out)


def compact(service: dict) -> dict:
    return {"id": service["Name"], "name": service.get("ServiceName") or service["Name"],
            "actions": sorted([[a["Name"], access_level(a.get("Annotations"))] for a in service.get("Actions") or []
                               if a.get("Name")], key=lambda x: x[0].lower()),
            "clients": boto3_clients(service)}


def http_json(url: str, client: httpx.Client) -> tuple[object, int]:
    r = client.get(url, headers={"Accept-Encoding": "gzip"})
    r.raise_for_status()
    return r.json(), len(r.content)


def fetch_reference(url: str = REFERENCE_URL, get: Callable[[str], tuple[object, int]] | None = None,
                    workers: int = 16) -> dict:
    """The index, then every service file in parallel (each failed file retried twice). All or nothing: a
    partial catalog would silently drop services, so any file that still fails fails the fetch."""
    t0 = time.time()
    own = None
    if get is None:
        own = httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=True)
        get = lambda u: http_json(u, own)  # noqa: E731
    try:
        index, nbytes = get(url)
        entries = [e for e in index if isinstance(e, dict) and e.get("url") and e.get("service")]

        def one(e):
            err = None
            for _ in range(3):
                try:
                    return get(e["url"])
                except Exception as ex:  # retried
                    err = ex
            raise RuntimeError(f"{e['service']}: {type(err).__name__}: {err}") from err

        services, total = {}, nbytes
        with cf.ThreadPoolExecutor(workers) as ex:
            for e, (doc, n) in zip(entries, ex.map(one, entries)):
                total += n
                if isinstance(doc, dict) and doc.get("Name"):
                    services[doc["Name"]] = compact(doc)
    finally:
        if own:
            own.close()
    return {"url": url, "services": services, "files": len(entries) + 1, "bytes": total,
            "fetch_s": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- the ceiling

def fetch_ceiling(iam, arn: str = READ_ONLY_ARN) -> dict:
    version = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
    doc = iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"]["Document"]
    if isinstance(doc, str):
        doc = json.loads(doc)
    allow, deny, skipped = [], [], 0
    stmts = doc.get("Statement") or []
    for st in stmts if isinstance(stmts, list) else [stmts]:
        acts = st.get("Action")
        if acts is None or st.get("NotAction") is not None:
            skipped += 1
            continue
        acts = acts if isinstance(acts, list) else [acts]
        if st.get("Effect") == "Deny":
            deny += acts
        elif st.get("Condition"):
            skipped += 1   # conditional allows don't count (s3express:CreateSession in v191)
        else:
            allow += acts
    return {"policy": arn.rsplit("/", 1)[-1], "version": version, "allow": allow, "deny": deny,
            "skipped_statements": skipped}


class Ceiling:
    """IAM action patterns (`*` and `?`, case-insensitive), compiled per service prefix."""

    def __init__(self, allow: list[str], deny: list[str] = ()):
        self._allow = self._compile(allow)
        self._deny = self._compile(deny)

    @staticmethod
    def _compile(patterns) -> dict:
        exact: dict[str, set] = {}
        wild: dict[str, list] = {}
        anywhere = []
        for p in patterns:
            p = str(p).lower()
            if p == "*":
                anywhere.append(re.compile(".*"))
                continue
            svc, _, name = p.partition(":")
            if "*" in svc or "?" in svc:
                anywhere.append(re.compile(re.escape(p).replace(r"\*", ".*").replace(r"\?", ".") + "$"))
            elif "*" in name or "?" in name:
                wild.setdefault(svc, []).append(re.escape(name).replace(r"\*", ".*").replace(r"\?", "."))
            else:
                exact.setdefault(svc, set()).add(name)
        return {"exact": exact, "wild": {s: re.compile("(?:" + "|".join(ps) + ")$") for s, ps in wild.items()},
                "anywhere": anywhere}

    @staticmethod
    def _hit(c: dict, svc: str, name: str) -> bool:
        if name in c["exact"].get(svc, ()):
            return True
        r = c["wild"].get(svc)
        if r and r.match(name):
            return True
        return any(r.match(f"{svc}:{name}") for r in c["anywhere"])

    def allows(self, action: str) -> bool:
        svc, _, name = action.lower().partition(":")
        return self._hit(self._allow, svc, name) and not self._hit(self._deny, svc, name)


# ---------------------------------------------------------------- the cache

class TTLCache:
    def __init__(self, name: str, fetch: Callable[[], dict], ttl: float = 3600, disk: Path | None = None,
                 clock: Callable[[], float] = time.time, background: bool = True):
        self.name, self.fetch, self.ttl, self.disk, self.clock = name, fetch, ttl, disk, clock
        self.background = background
        self.value: dict | None = None
        self.fetched_at: float | None = None
        self.error: str | None = None
        self.version = 0     # bumped on every new value, so derived indexes know to rebuild
        self._lock = threading.Lock()
        self._refreshing = False
        self._load_disk()

    def _load_disk(self) -> None:
        if not self.disk or not self.disk.exists():
            return
        try:
            d = json.loads(gzip.decompress(self.disk.read_bytes()))
            self.value, self.fetched_at, self.version = d["value"], d["fetched_at"], 1
            log.info("%s: loaded the copy on disk from %s", self.name, iso(self.fetched_at))
        except Exception as e:
            log.warning("%s: ignoring the copy on disk: %s", self.name, e)

    def _save_disk(self) -> None:
        if not self.disk:
            return
        try:
            self.disk.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.disk.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(json.dumps({"value": self.value, "fetched_at": self.fetched_at}).encode()))
            tmp.replace(self.disk)
        except Exception as e:
            log.warning("%s: couldn't write the copy on disk: %s", self.name, e)

    def _refresh(self) -> None:
        try:
            v = self.fetch()
            with self._lock:
                self.value, self.fetched_at, self.error = v, self.clock(), None
                self.version += 1
            self._save_disk()
            log.info("%s: refreshed", self.name)
        except Exception as e:
            with self._lock:
                self.error = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("%s: refresh failed, serving the cached copy: %s", self.name, self.error)
        finally:
            with self._lock:
                self._refreshing = False

    def age(self) -> float | None:
        return None if self.fetched_at is None else max(0.0, self.clock() - self.fetched_at)

    def get(self) -> dict:
        with self._lock:
            have, age = self.value is not None, self.age()
            start = have and age is not None and age >= self.ttl and not self._refreshing
            if start:
                self._refreshing = True
        if not have:
            with self._lock:
                if self.value is None:      # cold: everyone waits for one fetch
                    self._refreshing = True
                    try:
                        v = self.fetch()
                    except Exception as e:
                        self.error = f"{type(e).__name__}: {str(e)[:300]}"
                        raise Unavailable(f"{self.name} couldn't be fetched: {self.error}") from e
                    finally:
                        self._refreshing = False
                    self.value, self.fetched_at, self.error = v, self.clock(), None
                    self.version += 1
                    self._save_disk()
            return self.value
        if start:
            if self.background:
                threading.Thread(target=self._refresh, name=f"refresh-{self.name}", daemon=True).start()
            else:
                self._refresh()
        return self.value

    def state(self) -> dict:
        age = self.age()
        return {"fetched_at": iso(self.fetched_at), "age_s": None if age is None else int(age),
                "stale": bool(self.error) or (age is not None and age >= self.ttl), "error": self.error}
