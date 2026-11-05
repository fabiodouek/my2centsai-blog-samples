"""/ws/events plumbing: one shared CloudWatch Logs tail per process (iron-proxy audit + Route 53 Resolver
query logs), fanned out to every subscriber, plus per-user lifecycle polling.

Lifecycle events flow to every connection. The tail runs in a background thread only while at least one
connection says it is showing a feed panel (`{"type":"feeds","on":true}`): Live Tail is billed per session
minute, and an open tab that shows the terminal, a hidden tab or a closed panel needs no feed. It stops 60 s
after the last such connection turns its interest off or leaves.

It prefers StartLiveTail and restarts it when the session ends (sessions have a maximum length);
if Live Tail errors (not permitted, throttled, unavailable) it falls back to FilterLogEvents every 3 s for
a while, then tries Live Tail again. A log group that doesn't exist yet (the DNS group is created by the
infra stack) is skipped and re-checked every 5 minutes."""

from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .audit import SecretNames, parse_line
from .config import Settings
from .credentials import log_group_name

log = logging.getLogger("events")

POLL_S = 3.0
POLL_LOOKBACK_MS = 120_000
FALLBACK_HOLD_S = 120
RECHECK_GROUPS_S = 300
PRIME_WINDOW_MS = 10 * 60_000
BACKLOG = 300
IDLE_STOP_S = 60.0


def log_group_arn(ref: str, region: str, account: str) -> str:
    """Live Tail wants ARNs without the trailing ':*'."""
    if ref.startswith("arn:"):
        return ref[:-2] if ref.endswith(":*") else ref
    return f"arn:aws:logs:{region}:{account}:log-group:{ref}"


class LogTail:
    """Background thread delivering parsed events through `emit` (called from the thread)."""

    def __init__(self, s: Settings, emit: Callable[[dict], None], logs_client=None, account: str | None = None,
                 names: SecretNames | None = None):
        self.s, self.emit = s, emit
        self.names = names or SecretNames(s.secret_arns)
        # StartLiveTail sometimes never answers (seen in testing: the call blocks until botocore's 60 s read
        # timeout, then retries). Short timeouts and no retries let the polling fallback take over fast;
        # an established session sends an update every second, so 15 s of silence also means a dead stream.
        self.logs = logs_client or boto3.client("logs", config=Config(
            region_name=s.region, connect_timeout=5, read_timeout=15, retries={"max_attempts": 1, "mode": "standard"}))
        self._account = account
        # One stop event per run. A run that is still shutting down (e.g. blocked in StartLiveTail) holds the
        # old, set event: it can't change the mode or take over the stream of the run that replaced it.
        self._stop = threading.Event()
        self._stop.set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._seen: collections.deque = collections.deque(maxlen=5000)
        self._seen_set: set = set()
        self._stream = None
        self._covered_ms = int(time.time() * 1000)   # lines before this have been delivered (or primed)
        self._primed = False
        self.mode = "idle"
        self.groups: list[str] = []
        self.missing: list[str] = []

    @property
    def account(self) -> str:
        if not self._account:
            self._account = boto3.client("sts", config=Config(region_name=self.s.region)).get_caller_identity()["Account"]
        return self._account

    def status_event(self) -> dict:
        return {"type": "status", "source": self.mode, "log_groups": [log_group_name(g) for g in self.groups],
                "missing_log_groups": [log_group_name(g) for g in self.missing]}

    def _mode_locked(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.emit(self.status_event())

    def _set_mode(self, mode: str, stop: threading.Event) -> None:
        """Mode change from a run; a stopped or superseded run changes nothing."""
        with self._lock:
            if stop is self._stop and not stop.is_set():
                self._mode_locked(mode)

    def configured(self) -> list[str]:
        return [g for g in (self.s.proxy_log_group, self.s.dns_log_group) if g]

    # ---- lifecycle ----
    # Live Tail is billed per session minute, so the Hub runs this only while a browser shows a feed panel.
    # Modes: idle (not running) -> starting (priming or backfilling, opening the session) -> live_tail, or
    # polling / waiting; back to idle on stop.
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._stop.is_set():
                return
            self._stop = stop = threading.Event()
            self._mode_locked("starting")
            self._thread = threading.Thread(target=self._run, args=(stop,), name="log-tail", daemon=True)
            self._thread.start()
        log.info("log feed: starting Live Tail")

    def stop(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            st, self._stream = self._stream, None
            self._mode_locked("idle")
        if st is not None:
            try:
                st.close()
            except Exception:
                pass
        log.info("log feed: stopping Live Tail")

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # ---- helpers ----
    def _deliver(self, message: str, ts_ms, event_id: str | None = None) -> None:
        # Content key, not eventId: Live Tail results have no eventId, and switching between Live Tail and
        # polling (which looks back 2 minutes) must not deliver the same line twice.
        key = hashlib.sha1(f"{ts_ms}|{message}".encode()).hexdigest()
        if key in self._seen_set:
            return
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        ev = parse_line(message, self.names, ts_ms)
        if ev:
            self.emit(ev)

    def _exists(self, name: str) -> bool:
        try:
            self.logs.filter_log_events(logGroupName=name, limit=1, startTime=int(time.time() * 1000))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return False
            raise

    def _resolve_groups(self) -> None:
        groups, missing = [], []
        for ref in self.configured():
            try:
                (groups if self._exists(log_group_name(ref)) else missing).append(ref)
            except ClientError as e:
                log.warning("checking log group %s: %s", log_group_name(ref), e.response.get("Error", {}).get("Code"))
                groups.append(ref)
        self.groups, self.missing = groups, missing
        if missing:
            log.info("log groups not found yet (skipped): %s", [log_group_name(m) for m in missing])

    def _gap_from(self) -> int:
        """Where a backfill starts: a little before the coverage mark, at most 10 minutes back."""
        return max(self._covered_ms - 10_000, int(time.time() * 1000) - PRIME_WINDOW_MS)

    def _backfill(self, since_ms: int) -> None:
        """Lines written while no Live Tail session was open (StartLiveTail can take 10+ s to answer). Content
        dedupe makes overlap harmless."""
        for ref in self.groups:
            kw = {"logGroupName": log_group_name(ref), "startTime": since_ms}
            try:
                for _ in range(5):
                    r = self.logs.filter_log_events(**kw)
                    for e in r.get("events", []):
                        self._deliver(e.get("message", ""), e.get("timestamp"))
                    if not r.get("nextToken"):
                        break
                    kw["nextToken"] = r["nextToken"]
            except ClientError as e:
                log.info("backfill %s: %s", kw["logGroupName"], e.response.get("Error", {}).get("Code"))

    def _prime(self, stop: threading.Event) -> None:
        """Recent history so a fresh feed isn't empty (FilterLogEvents, last 10 minutes)."""
        for ref in self.groups:
            kw = {"logGroupName": log_group_name(ref), "startTime": int(time.time() * 1000) - PRIME_WINDOW_MS}
            events, pages = [], 0
            try:
                while pages < 5 and not stop.is_set():
                    r = self.logs.filter_log_events(**kw)
                    events += r.get("events", [])
                    pages += 1
                    if not r.get("nextToken"):
                        break
                    kw["nextToken"] = r["nextToken"]
            except ClientError as e:
                log.warning("prime %s: %s", kw["logGroupName"], e.response.get("Error", {}).get("Code"))
            for e in sorted(events, key=lambda e: e.get("timestamp", 0))[-BACKLOG:]:
                self._deliver(e.get("message", ""), e.get("timestamp"))

    # ---- main loop ----
    def _run(self, stop: threading.Event) -> None:
        fallback_until = 0.0
        last_check = 0.0
        caught_up = False
        while not stop.is_set():
            try:
                if time.monotonic() - last_check > RECHECK_GROUPS_S or not self.groups:
                    self._resolve_groups()
                    last_check = time.monotonic()
                if not self.groups:
                    self._set_mode("waiting", stop)
                    stop.wait(30)
                    continue
                if not self._primed:
                    # First run: the last 10 minutes.
                    self._covered_ms = int(time.time() * 1000)
                    self._prime(stop)
                    self._primed = True
                elif not caught_up:
                    # A restart after an idle stop: fill the gap since the previous run now, before
                    # StartLiveTail answers (it can take 10+ s), so a panel that just opened catches up at once.
                    self._backfill(self._gap_from())
                caught_up = True
                if time.monotonic() < fallback_until:
                    self._poll_once(stop)
                    stop.wait(POLL_S)
                    continue
                self._live_tail(stop, lambda: time.monotonic() - last_check > RECHECK_GROUPS_S and bool(self.missing))
            except Exception as e:
                if stop.is_set():
                    break
                code = e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else type(e).__name__
                log.warning("log feed error (%s); polling every %ss for %ss before trying Live Tail again",
                            code, POLL_S, FALLBACK_HOLD_S)
                fallback_until = time.monotonic() + FALLBACK_HOLD_S
                self._set_mode("polling", stop)

    def _live_tail(self, stop: threading.Event, should_restart: Callable[[], bool]) -> None:
        arns = [log_group_arn(g, self.s.region, self.account) for g in self.groups]
        started = time.monotonic()
        gap_from = self._gap_from()
        stream = self.logs.start_live_tail(logGroupIdentifiers=arns)["responseStream"]
        with self._lock:
            current = stop is self._stop and not stop.is_set()
            if current:
                self._stream = stream            # so stop() can close it
        if not current:                          # stopped while StartLiveTail was answering
            try:
                stream.close()
            except Exception:
                pass
            return
        self._set_mode("live_tail", stop)
        # Backfill the gap before the session existed, again a little later (FilterLogEvents lags ingestion).
        backfills = [started, started + 15, started + 45]
        try:
            for ev in stream:
                if stop.is_set() or should_restart():
                    break
                upd = ev.get("sessionUpdate")
                if upd:
                    for res in upd.get("sessionResults") or []:
                        self._deliver(res.get("message", ""), res.get("timestamp"))
                elif "sessionStart" in ev:
                    log.info("live tail session started on %d log group(s)", len(arns))
                if backfills and time.monotonic() >= backfills[0]:
                    backfills.pop(0)
                    self._backfill(gap_from)
                self._covered_ms = int(time.time() * 1000)
        finally:
            with self._lock:
                if self._stream is stream:
                    self._stream = None
            try:
                stream.close()
            except Exception:
                pass
            log.info("live tail session ended after %.0f s%s", time.monotonic() - started,
                     " (feed stopping)" if stop.is_set() else "")
        # A normal end (session max length) falls through and the loop starts a new session; don't spin if
        # sessions end right away.
        if time.monotonic() - started < 5:
            stop.wait(5)

    def _poll_once(self, stop: threading.Event) -> None:
        self._set_mode("polling", stop)
        now = int(time.time() * 1000)
        start = min(now - POLL_LOOKBACK_MS, self._covered_ms - 10_000)
        self._covered_ms = now
        for ref in self.groups:
            kw = {"logGroupName": log_group_name(ref), "startTime": start}
            for _ in range(10):
                r = self.logs.filter_log_events(**kw)
                for e in r.get("events", []):
                    self._deliver(e.get("message", ""), e.get("timestamp"))
                if not r.get("nextToken"):
                    break
                kw["nextToken"] = r["nextToken"]


class Hub:
    """Fan-out to /ws/events subscribers. Log events are shared; lifecycle events are per user.

    The log tail follows feed interest: each connection reports whether its page shows a feed panel right
    now (`want_feeds`). The tail starts when the first connection wants it and stops `idle_stop_s` after the
    last one stops wanting it or disconnects. Proxy and DNS events go to every connection while the tail runs,
    so a page that opens a panel later already has them."""

    def __init__(self, tail_factory: Callable[[Callable[[dict], None]], LogTail] | None, idle_stop_s: float = IDLE_STOP_S):
        self._subs: dict[asyncio.Queue, str] = {}
        self._feeds: set[asyncio.Queue] = set()   # connections showing a feed panel now
        self._backlog: collections.deque = collections.deque(maxlen=BACKLOG)
        self._pokes: dict[str, set[asyncio.Event]] = collections.defaultdict(set)
        self._life: dict[tuple[str, str], str] = {}   # (sub, sandbox id) -> last state pushed
        self._tail_factory = tail_factory
        self.idle_stop_s = idle_stop_s
        self.tail: LogTail | None = None
        self._stop_handle: asyncio.TimerHandle | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def status(self) -> dict:
        if self.tail:
            return self.tail.status_event()
        return {"type": "status", "source": "idle" if self._tail_factory else "disabled", "log_groups": [],
                "missing_log_groups": []}

    def feed_interest(self) -> int:
        return len(self._feeds)

    def _emit_threadsafe(self, ev: dict) -> None:
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.publish, ev)

    def publish(self, ev: dict) -> None:
        if ev.get("type") != "status":
            self._backlog.append(ev)
        for q in list(self._subs):
            offer(q, ev)

    def subscribe(self, sub: str) -> asyncio.Queue:
        """A new /ws/events connection: replays the recent feed events, starts nothing."""
        self.loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        for ev in list(self._backlog):
            offer(q, ev)
        self._subs[q] = sub
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.pop(q, None)
        self.want_feeds(q, False)

    def want_feeds(self, q: asyncio.Queue, on: bool) -> None:
        """Record whether this connection's page shows a feed panel. Idempotent per connection."""
        if on and q in self._subs and q not in self._feeds:
            self._feeds.add(q)
            log.info("log feed: a connection is showing a feed panel (%d now)", len(self._feeds))
            if self._stop_handle:
                self._stop_handle.cancel()
                self._stop_handle = None
                log.info("log feed: interest is back within the grace period; Live Tail keeps running")
            if self._tail_factory and self.tail is None:
                self.tail = self._tail_factory(self._emit_threadsafe)
            if self.tail:
                self.tail.start()   # no-op if already running
        elif not on and q in self._feeds:
            self._feeds.discard(q)
            log.info("log feed: a connection stopped showing a feed panel (%d now)", len(self._feeds))
            if not self._feeds and self.tail and self.tail.running() and self.loop:
                # Keep the tail a while so a page reload or a quick tab switch doesn't restart the session.
                if self._stop_handle:
                    self._stop_handle.cancel()
                self._stop_handle = self.loop.call_later(self.idle_stop_s, self._stop_if_idle)
                log.info("log feed: no feed panel open; stopping Live Tail in %.0f s unless one opens", self.idle_stop_s)

    def _stop_if_idle(self) -> None:
        self._stop_handle = None
        if not self._feeds and self.tail and self.tail.running():
            log.info("log feed: no feed panel open for %.0f s", self.idle_stop_s)
            self.tail.stop()   # the instance stays: its dedupe memory and coverage mark drive the backfill

    def observe(self, sub: str, vm_id: str, name: str | None, state: str | None) -> bool:
        """Record a sandbox state seen anywhere (pollers, the post-action settle loop) and push a lifecycle
        event to the owner's /ws/events subscribers if it changed. Central, so no duplicates."""
        if not state or state == "UNKNOWN":
            return False
        prev = self._life.get((sub, vm_id))
        if prev == state:
            return False
        self._life[(sub, vm_id)] = state
        ev = {"type": "lifecycle", "id": vm_id, "name": name, "state": state, "prev": prev, "ts": _now(), "initial": False}
        for q, owner in list(self._subs.items()):
            if owner == sub:
                offer(q, ev)
        return True

    def snapshot_event(self, sub: str, vm_id: str, name: str | None, state: str) -> dict:
        """The per-connection initial snapshot (prev null, initial true); seeds the central state."""
        self._life.setdefault((sub, vm_id), state)
        return {"type": "lifecycle", "id": vm_id, "name": name, "state": state, "prev": None, "ts": _now(), "initial": True}

    def poke(self, sub: str) -> None:
        """Wake this user's lifecycle pollers now (after a suspend/resume/terminate/create)."""
        for ev in list(self._pokes.get(sub, ())):
            ev.set()

    def poke_event(self, sub: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._pokes[sub].add(ev)
        return ev

    def drop_poke_event(self, sub: str, ev: asyncio.Event) -> None:
        self._pokes.get(sub, set()).discard(ev)

    def shutdown(self) -> None:
        if self.tail:
            self.tail.stop()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def offer(q: asyncio.Queue, ev: dict) -> None:
    """Queue for a subscriber; a slow subscriber loses events rather than blocking everyone."""
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:
        pass
