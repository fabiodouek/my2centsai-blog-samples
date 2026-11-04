"""LogTail and Hub with a fake CloudWatch Logs client: Live Tail path, fallback to polling, dedupe, missing
groups, and the tail following feed interest (start on the first panel, 60 s grace stop, backfill on restart)."""

import asyncio
import json
import threading
import time

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.config import Settings
from app.events import Hub, LogTail
from app.main import create_app
from app.store import MemoryStore
from tests.fakes import FakeMicroVMs, FakeVerifier

ARN = "arn:aws:logs:us-east-1:111122223333:log-group:/my2cents/iron-proxy"
DNS = "arn:aws:logs:us-east-1:111122223333:log-group:/my2cents/dns-queries"


def proxy(host, path="/get"):
    return json.dumps({"time": "2026-11-03T18:00:00Z", "audit": {"host": host, "method": "GET", "path": path,
                       "action": "allow", "status_code": 200}})


class FakeStream:
    """A Live Tail session: yields its events, then stays open until closed (like a real session)."""

    def __init__(self, events):
        self.events = events
        self.closed = threading.Event()

    def __iter__(self):
        for e in self.events:
            yield e
        self.closed.wait(5)

    def close(self):
        self.closed.set()


class FakeLogs:
    def __init__(self, live_error=None, missing=()):
        self.live_error, self.missing = live_error, set(missing)
        self.live_calls, self.filter_calls = [], 0
        self.streams: list[FakeStream] = []
        self.gap_line = None
        self.live_gate = threading.Event()   # cleared = StartLiveTail hangs until set (it can take 10+ s)
        self.live_gate.set()

    def filter_log_events(self, logGroupName, **kw):
        if logGroupName in self.missing:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "FilterLogEvents")
        self.filter_calls += 1
        if kw.get("limit") == 1:
            return {"events": []}
        events = [{"message": proxy("polled.example"), "timestamp": 1_000, "eventId": "1"}]
        if self.gap_line:
            events.append({"message": self.gap_line, "timestamp": 2_000, "eventId": "2"})
        return {"events": events}

    def start_live_tail(self, logGroupIdentifiers):
        self.live_calls.append(logGroupIdentifiers)
        self.live_gate.wait(5)
        if self.gap_line is None:
            self.gap_line = proxy("gap.example")   # written after the prime, before the session answered
        if self.live_error:
            raise ClientError({"Error": {"Code": self.live_error}}, "StartLiveTail")
        now = int(time.time() * 1000)
        res = [{"message": proxy("live.example"), "timestamp": now}, {"message": proxy("live.example"), "timestamp": now},
               {"message": "not json", "timestamp": now}]
        st = FakeStream([{"sessionStart": {}}, {"sessionUpdate": {"sessionResults": res}}])
        self.streams.append(st)
        return {"responseStream": st}


def settings():
    return Settings(proxy_log_group=ARN, dns_log_group=DNS)


def run_tail(fake, groups=(ARN, DNS), seconds=0.5):
    got = []
    s = Settings(proxy_log_group=groups[0], dns_log_group=groups[1] if len(groups) > 1 else None)
    t = LogTail(s, got.append, logs_client=fake, account="111122223333")
    t.start()
    time.sleep(seconds)
    mode = t.mode
    t.stop()
    t._thread.join(5)
    return t, got, mode


def test_live_tail_path_and_dedupe():
    fake = FakeLogs()
    t, got, mode = run_tail(fake)
    assert mode == "live_tail"
    assert fake.live_calls[0] == [ARN, DNS]              # ARNs, no ':*'
    hosts = [e["host"] for e in got if e["type"] == "proxy"]
    assert hosts.count("live.example") == 1               # duplicate line delivered once
    assert hosts.count("polled.example") == 1             # primed from recent history, once (backfill dedupes)
    assert "gap.example" in hosts                         # written while the session was starting: backfilled
    statuses = [e["source"] for e in got if e["type"] == "status"]
    assert statuses == ["starting", "live_tail", "idle"]
    assert fake.streams[0].closed.is_set()


def test_fallback_to_polling_when_live_tail_denied():
    fake = FakeLogs(live_error="AccessDeniedException")
    t, got, mode = run_tail(fake)
    assert mode == "polling"
    assert any(e["type"] == "status" and e["source"] == "polling" for e in got)
    assert any(e["type"] == "proxy" and e["host"] == "polled.example" for e in got)


def test_missing_group_is_skipped():
    fake = FakeLogs(missing={"/my2cents/dns-queries"})
    t, got, mode = run_tail(fake)
    assert fake.live_calls[0] == [ARN]
    assert t.missing == [DNS]


def test_stop_while_live_tail_is_answering_leaves_no_session():
    """StartLiveTail answers after stop(): the stale run closes that session and changes nothing."""
    fake = FakeLogs()
    fake.live_gate.clear()
    got = []
    t = LogTail(settings(), got.append, logs_client=fake, account="111122223333")
    t.start()
    deadline = time.monotonic() + 2
    while not fake.live_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.live_calls and t.mode == "starting"
    t.stop()
    fake.live_gate.set()                                      # the session answers now
    t._thread.join(5)
    assert not t._thread.is_alive()
    assert fake.streams[0].closed.is_set() and t._stream is None
    assert t.mode == "idle"
    assert [e["source"] for e in got if e["type"] == "status"] == ["starting", "idle"]


# ---------------- Hub: the tail follows feed interest ----------------

def make_hub(fake, grace=0.2):
    return Hub(lambda emit: LogTail(settings(), emit, logs_client=fake, account="111122223333"), idle_stop_s=grace)


async def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_connections_alone_start_nothing_and_lifecycle_still_flows():
    fake = FakeLogs()
    hub = make_hub(fake)
    q1, q2 = hub.subscribe("alice"), hub.subscribe("bob")
    await asyncio.sleep(0.2)
    assert hub.tail is None and fake.live_calls == [] and fake.filter_calls == 0
    assert hub.status()["source"] == "idle"
    assert hub.observe("alice", "microvm-1", "a", "RUNNING")
    assert [e["type"] for e in drain(q1)] == ["lifecycle"]   # lifecycle events need no tail
    assert drain(q2) == []                                   # and only go to their owner
    assert Hub(None).status()["source"] == "disabled"


async def test_interest_is_counted_per_connection():
    fake = FakeLogs()
    hub = make_hub(fake)
    q1, q2, q3 = hub.subscribe("alice"), hub.subscribe("bob"), hub.subscribe("carol")
    hub.want_feeds(q1, True)
    assert await wait_until(lambda: hub.tail.mode == "live_tail")
    assert hub.feed_interest() == 1 and len(fake.live_calls) == 1
    hub.want_feeds(q1, True)                                  # repeats (e.g. a re-send) don't count twice
    hub.want_feeds(q2, True)
    assert hub.feed_interest() == 2 and len(fake.live_calls) == 1
    hub.want_feeds(q1, False)
    hub.want_feeds(q1, False)
    assert hub.feed_interest() == 1
    await asyncio.sleep(0.3)                                  # longer than the grace period
    assert hub.tail.running()                                 # q2 still shows a panel
    hub.want_feeds(q2, False)
    assert hub.feed_interest() == 0
    await asyncio.sleep(0.05)
    assert hub.tail.running()                                 # grace period
    assert await wait_until(lambda: not hub.tail.running(), 1.0)
    hub.tail._thread.join(5)
    assert len(fake.live_calls) == 1 and fake.streams[0].closed.is_set()
    # Every connection, interested or not, saw the status changes and the proxy events.
    for q in (q1, q2, q3):
        evs = drain(q)
        assert [e["source"] for e in evs if e["type"] == "status"] == ["starting", "live_tail", "idle"]
        assert any(e["type"] == "proxy" and e["host"] == "live.example" for e in evs)
    assert hub.status()["source"] == "idle"


async def test_grace_period_is_cancelled_when_a_panel_returns():
    fake = FakeLogs()
    hub = make_hub(fake, grace=0.2)
    q = hub.subscribe("alice")
    hub.want_feeds(q, True)
    assert await wait_until(lambda: hub.tail.mode == "live_tail")
    hub.want_feeds(q, False)
    await asyncio.sleep(0.1)
    hub.want_feeds(q, True)                                   # back within the grace period
    await asyncio.sleep(0.3)
    assert hub.tail.running() and len(fake.live_calls) == 1   # same session, never restarted
    hub.shutdown()


async def test_disconnect_counts_as_losing_interest():
    fake = FakeLogs()
    hub = make_hub(fake, grace=0.1)
    q = hub.subscribe("alice")
    hub.want_feeds(q, True)
    assert await wait_until(lambda: hub.tail.mode == "live_tail")
    hub.unsubscribe(q)
    assert hub.feed_interest() == 0
    assert await wait_until(lambda: not hub.tail.running(), 1.0)
    hub.want_feeds(q, True)                                   # a late message from a closed connection: ignored
    assert hub.feed_interest() == 0 and not hub.tail.running()


async def test_restart_backfills_the_gap_before_live_tail_answers():
    fake = FakeLogs()
    hub = make_hub(fake, grace=0.05)
    q = hub.subscribe("alice")
    hub.want_feeds(q, True)
    assert await wait_until(lambda: hub.tail.mode == "live_tail")
    hub.want_feeds(q, False)
    assert await wait_until(lambda: not hub.tail.running(), 1.0)
    hub.tail._thread.join(5)
    drain(q)
    fake.gap_line = proxy("while-stopped.example")            # logged while nobody watched
    fake.live_gate.clear()                                    # and StartLiveTail is slow this time
    hub.want_feeds(q, True)
    assert await wait_until(lambda: len(fake.live_calls) == 2)
    await asyncio.sleep(0.05)
    evs = drain(q)
    assert [e["source"] for e in evs if e["type"] == "status"] == ["starting"]
    assert any(e["type"] == "proxy" and e["host"] == "while-stopped.example" for e in evs)  # before the session
    fake.live_gate.set()
    assert await wait_until(lambda: hub.tail.mode == "live_tail")
    hub.shutdown()


# ---------------- through the WebSocket ----------------

def test_feeds_message_drives_the_tail():
    fake = FakeLogs()
    hub = make_hub(fake, grace=0.1)
    s = Settings(origin_secret="s3cret", user_pool_id="p", client_id="c", table_name="t", image_arn="arn:img",
                 connector_arn="arn:conn", nlb_ips=["10.50.10.10"], proxy_log_group=ARN, dns_log_group=DNS)
    c = TestClient(create_app(s, microvms=FakeMicroVMs(), store=MemoryStore(), verifier=FakeVerifier(), hub=hub))

    def until_pong(ws):
        seen = []
        while True:
            m = ws.receive_json()
            if m["type"] == "pong":
                return seen
            seen.append(m)

    with c.websocket_connect("/ws/events", headers={"X-Origin-Verify": "s3cret"}) as ws:
        ws.send_json({"type": "auth", "token": "tok-alice"})
        assert ws.receive_json() == {"type": "status", "source": "idle", "log_groups": [], "missing_log_groups": []}
        ws.send_json({"type": "ping"})
        until_pong(ws)
        assert fake.live_calls == [] and hub.feed_interest() == 0
        ws.send_json({"type": "feeds", "on": True})
        ws.send_json({"type": "ping"})
        until_pong(ws)
        assert hub.feed_interest() == 1
        deadline = time.monotonic() + 2
        while hub.tail.mode != "live_tail" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(fake.live_calls) == 1
        ws.send_json({"type": "feeds", "on": "yes"})          # only a JSON true turns it on; anything else is off
        ws.send_json({"type": "ping"})
        until_pong(ws)
        assert hub.feed_interest() == 0
        deadline = time.monotonic() + 2
        while hub.tail.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not hub.tail.running()
        ws.send_json({"type": "feeds", "on": True})           # and on again, then the socket closes
        ws.send_json({"type": "ping"})
        until_pong(ws)
        assert hub.feed_interest() == 1
    deadline = time.monotonic() + 2
    while hub.feed_interest() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hub.feed_interest() == 0
