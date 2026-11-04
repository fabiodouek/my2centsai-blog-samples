"""Parsing iron-proxy audit lines and Route 53 Resolver query-log lines into the events the UI shows.

Proxy records carry the secret's ARN when a placeholder was swapped; the UI only ever sees the short name
(anthropic/github/demo). No secret values exist in these logs, and none are emitted."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone


def ms_iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _norm_ts(v, fallback_ms) -> str | None:
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except ValueError:
            pass
    if isinstance(v, (int, float)):
        return ms_iso(v if v > 1e11 else v * 1000)
    return ms_iso(fallback_ms)


class SecretNames:
    """ARN (or anything containing the secret's name) -> its short name (anthropic/github/demo, or a custom
    credential's name). Takes a dict, or a callable returning one: custom credentials come and go, so a
    callable is re-read at most every `ttl` seconds."""

    def __init__(self, secret_arns, ttl: float = 15.0):
        self._source = secret_arns
        self._ttl = ttl
        self._at = -1e9
        self.by_arn: dict[str, str] = {}
        self.by_secret_name: dict[str, str] = {}
        self._load(secret_arns if isinstance(secret_arns, dict) else {})

    def _load(self, secret_arns: dict) -> None:
        self.by_arn = {arn: name for name, arn in secret_arns.items() if arn}
        self.by_secret_name = {}
        for name, arn in secret_arns.items():
            if arn and ":secret:" in arn:
                full = arn.split(":secret:", 1)[1]
                base = full.rsplit("-", 1)[0] if len(full.rsplit("-", 1)[-1]) == 6 else full
                self.by_secret_name[base] = name

    def _refresh(self) -> None:
        if callable(self._source) and time.monotonic() - self._at > self._ttl:
            self._at = time.monotonic()
            try:
                self._load(self._source())
            except Exception:  # keep the last known names
                pass

    def name(self, ref: str | None) -> str | None:
        if not ref:
            return None
        self._refresh()
        if ref in self.by_arn:
            return self.by_arn[ref]
        # Longest match first, so "my2cents-ip/user/key" doesn't win over "my2cents-ip/user/key-2"
        for base, name in sorted(self.by_secret_name.items(), key=lambda kv: -len(kv[0])):
            if base and base in ref:
                return name
        return None


def swapped_secrets(rec: dict, names: SecretNames) -> list[str]:
    out = []
    for t in rec.get("request_transforms") or []:
        if not isinstance(t, dict):
            continue
        for s in ((t.get("annotations") or {}).get("swapped") or []):
            n = names.name(s.get("secret") if isinstance(s, dict) else str(s))
            label = n or "unknown"
            if label not in out:
                out.append(label)
    return out


def aws_info(rec: dict) -> dict | None:
    """What aws_auth did, from its annotations: {"service", "region", "signed", "rejected"}, or None when it
    didn't act (not an AWS host). On success it annotates injected/service/region; on a refusal, rejected plus
    the service or region that was refused."""
    for t in rec.get("request_transforms") or []:
        if not isinstance(t, dict) or t.get("name") != "aws_auth":
            continue
        ann = t.get("annotations") or {}
        if not isinstance(ann, dict) or not ann:
            continue
        rejected = ann.get("rejected")
        return {"service": ann.get("service"), "region": ann.get("region"),
                "signed": bool(ann.get("injected")) and not rejected,
                "rejected": str(rejected) if rejected else None}
    if rec.get("rejected_by") == "aws_auth":
        return {"service": None, "region": None, "signed": False, "rejected": "rejected"}
    return None


def _host(h: str) -> str:
    """CONNECT requests are logged as host:443; show plain names so the feed groups by host."""
    for port in (":443", ":80"):
        if h.endswith(port):
            return h[: -len(port)]
    return h


def proxy_event(rec: dict, names: SecretNames, fallback_ms=None) -> dict | None:
    a = rec.get("audit")
    if not isinstance(a, dict) or not a.get("host"):
        return None
    return {
        "type": "proxy",
        "ts": _norm_ts(rec.get("time"), fallback_ms),
        "action": a.get("action"),
        "status": a.get("status_code"),
        "method": a.get("method"),
        "host": _host(str(a.get("host"))),
        "path": a.get("path"),
        "mode": a.get("mode"),
        "duration_ms": a.get("duration_ms"),
        "rejected_by": rec.get("rejected_by"),
        "secrets": swapped_secrets(rec, names),
        "aws": aws_info(rec),
    }


def dns_event(rec: dict, fallback_ms=None) -> dict | None:
    if "query_name" not in rec:
        return None
    return {
        "type": "dns",
        "ts": _norm_ts(rec.get("query_timestamp"), fallback_ms),
        "query": rec.get("query_name"),
        "qtype": rec.get("query_type"),
        "rcode": rec.get("rcode"),
        "srcaddr": rec.get("srcaddr"),
    }


def parse_line(message: str, names: SecretNames, timestamp_ms=None) -> dict | None:
    try:
        rec = json.loads(message)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict):
        return None
    if "query_name" in rec:
        return dns_event(rec, timestamp_ms)
    return proxy_event(rec, names, timestamp_ms)
