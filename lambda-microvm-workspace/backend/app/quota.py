"""The account's MicroVM memory quota, read from Service Quotas instead of assumed.

Lambda MicroVMs count every live MicroVM's memory (suspended ones too) against one regional quota: service code
`lambda`, quota "Max allocated ARM_64 MicroVM memory" (L-CD1C0CC4, in GB, adjustable, so it differs per account).
GetServiceQuota returns the value applied to this account. The AWS default for the same code is much larger (2,048 GB
on 2026-11-04) and isn't what a new account gets, so it's never used here: when the call fails, the fallback is
QUOTA_GB (8). The value is cached for an hour (a failure for five minutes)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from .config import Settings

log = logging.getLogger("quota")

TTL_S = 3600.0
RETRY_S = 300.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class MemoryQuota:
    def __init__(self, s: Settings, client=None, clock=time.time):
        self.s = s
        self._client = client
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, float, dict] | None = None     # (expires at, GB, source)

    @property
    def client(self):
        if self._client is None:
            # One quick attempt: a failure is cached for five minutes and the fallback is served meanwhile
            self._client = boto3.client("service-quotas", config=Config(
                region_name=self.s.region, retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=3, read_timeout=5))
        return self._client

    def get(self) -> tuple[float, dict]:
        """(GB, where it came from). Never raises."""
        now = self.clock()
        with self._lock:
            if self._cached and now < self._cached[0]:
                return self._cached[1], self._cached[2]
        try:
            q = self.client.get_service_quota(ServiceCode=self.s.quota_service_code, QuotaCode=self.s.quota_code)["Quota"]
            value, unit = float(q["Value"]), str(q.get("Unit") or "Gigabytes")
            gb = value / 1024 if unit.lower().startswith("mega") else value
            if gb <= 0:
                raise ValueError(f"quota value {value} {unit}")
            source = {"kind": "service_quotas", "service_code": self.s.quota_service_code, "quota_code": self.s.quota_code,
                      "name": q.get("QuotaName"), "adjustable": q.get("Adjustable"), "unit": unit,
                      "fetched_at": _iso(now), "error": None}
            ttl = TTL_S
        except Exception as e:   # no permission, throttled, unknown code: the configured default
            log.warning("service quota %s/%s: %s", self.s.quota_service_code, self.s.quota_code, e)
            gb = float(self.s.quota_gb)
            source = {"kind": "fallback", "service_code": self.s.quota_service_code, "quota_code": self.s.quota_code,
                      "name": None, "adjustable": None, "unit": "Gigabytes", "fetched_at": _iso(now),
                      "error": f"{type(e).__name__}: {str(e)[:160]}"}
            ttl = RETRY_S
        with self._lock:
            self._cached = (now + ttl, gb, source)
        return gb, source

    def gb(self) -> float:
        return self.get()[0]

    def peek(self) -> float:
        """The last value read (or the fallback), without calling AWS: for code on the event loop."""
        with self._lock:
            return self._cached[1] if self._cached else float(self.s.quota_gb)
