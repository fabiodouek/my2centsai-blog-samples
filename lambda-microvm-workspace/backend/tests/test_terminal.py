"""Terminal bridge with fake WebSockets on both sides and a fake control plane. Time is scaled 1000x
(the bridge's sleep/clock are injected), so the 60 s reconnect window takes ~60 ms."""

import asyncio
import json
import time

import pytest

from app.terminal import TMUX_CMD, TerminalBridge
from tests.fakes import Connector, FakeBrowser, FakeControl, FakeUpstream


def make(browser, cp, connector, **kw):
    return TerminalBridge(browser, cp, connector, sleep=lambda d: asyncio.sleep(d / 1000),
                          clock=lambda: time.monotonic() * 1000, watch_interval=kw.pop("watch_interval", 5),
                          suspended_poll=2, init_timeout=kw.pop("init_timeout", 0.5),
                          first_resize_wait=kw.pop("first_resize_wait", 0.2), heartbeat_s=kw.pop("heartbeat_s", 0), **kw)


def texts(sent):
    return [json.loads(x) for x in sent if isinstance(x, str)]


async def start(bridge):
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0)
    return task


async def finish(task, timeout=2):
    await asyncio.wait_for(task, timeout)


async def test_attach_resize_then_tmux_and_bridging():
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    b.type(json.dumps({"type": "resize", "rows": 33, "cols": 101}))
    bridge = make(b, cp, Connector(up))
    task = await start(bridge)
    await b.wait_for(lambda: "attached" in b.states())
    # resize goes up first (text JSON), then the tmux command (binary)
    assert json.loads(up.sent[0]) == {"type": "resize", "rows": 33, "cols": 101}
    assert up.sent[1] == TMUX_CMD and isinstance(up.sent[1], bytes)
    assert b.statuses()[-1]["session_id"] == "sess-1"
    # keystrokes: browser binary -> upstream binary; "input" convenience -> binary too
    b.type(b"echo hello\r")
    b.type(json.dumps({"type": "input", "data": "claude\n"}))
    # resize while attached -> text JSON upstream
    b.type(json.dumps({"type": "resize", "rows": 50, "cols": 200}))
    b.type(json.dumps({"type": "resize", "rows": 0, "cols": 99999}))  # invalid: ignored
    b.type("not json")                                               # ignored
    await b.wait_for(lambda: len(up.sent) >= 5)
    assert up.sent[2] == b"echo hello\r"
    assert up.sent[3] == b"claude\n"
    assert json.loads(up.sent[4]) == {"type": "resize", "rows": 50, "cols": 200}
    # output: binary -> binary, text -> binary; control JSON from the shell isn't shown
    up.emit(b"hello\r\n")
    up.emit("plain text output")
    up.emit(json.dumps({"type": "session_init", "session_id": "again"}))
    up.emit(b"$ ")
    await b.wait_for(lambda: b.output().endswith(b"$ "))
    assert b.output() == b"hello\r\nplain text output$ "
    # ping/pong with the browser
    b.type(json.dumps({"type": "ping"}))
    await b.wait_for(lambda: any(k == "text" and json.loads(d).get("type") == "pong" for k, d in b.out))
    # browser leaves -> upstream closed, bridge ends
    b.leave()
    await finish(task)
    assert up.closed
    assert cp.tokens == 1


async def test_default_size_when_browser_sends_none():
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    task = await start(make(b, cp, Connector(up), first_resize_wait=0.05))
    await b.wait_for(lambda: "attached" in b.states())
    assert json.loads(up.sent[0]) == {"type": "resize", "rows": 40, "cols": 120}
    b.leave()
    await finish(task)


async def test_input_before_attach_is_buffered_and_flushed_after_tmux():
    b, cp = FakeBrowser(), FakeControl()
    up = FakeUpstream(init=False)   # session_init arrives late
    b.type(json.dumps({"type": "resize", "rows": 24, "cols": 80}))
    b.type(b"ls\r")
    task = await start(make(b, cp, Connector(up)))
    await asyncio.sleep(0.05)
    assert up.sent == []            # nothing sent before session_init
    up.emit(json.dumps({"type": "session_init", "session_id": "late"}))
    await b.wait_for(lambda: "attached" in b.states())
    assert up.sent[1:] == [TMUX_CMD, b"ls\r"]
    b.leave()
    await finish(task)


async def test_shell_exit_reports_exited_and_closes_1000():
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    task = await start(make(b, cp, Connector(up)))
    await b.wait_for(lambda: "attached" in b.states())
    up.drop(1000, "shell exited")
    await finish(task)
    assert b.states() == ["connecting", "attached", "exited"]
    assert b.closed == (1000, "shell exited")


async def test_suspend_then_resume_reconnects_and_reattaches():
    b, cp = FakeBrowser(), FakeControl()
    up1, up2 = FakeUpstream(), FakeUpstream()
    task = await start(make(b, cp, Connector(up1, up2)))
    await b.wait_for(lambda: "attached" in b.states())
    # The sandbox is suspended (REST call or idle policy); its connections die.
    cp.st = "SUSPENDED"
    up1.drop(1006, "")
    await b.wait_for(lambda: "suspended" in b.states())
    assert cp.resumed == 0        # a suspend seen mid-session is not undone automatically
    await asyncio.sleep(0.05)
    assert "attached" not in b.states()[2:]
    # Resumed elsewhere (the UI's Resume button): the bridge notices and reattaches tmux.
    cp.st = "RUNNING"
    await b.wait_for(lambda: b.states().count("attached") == 2)
    assert up2.sent[1] == TMUX_CMD
    assert b.states() == ["connecting", "attached", "suspended", "connecting", "attached"]
    up2.emit(b"same screen")
    await b.wait_for(lambda: b.output().endswith(b"same screen"))
    b.leave()
    await finish(task)


async def test_watcher_notices_suspend_even_if_socket_hangs():
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    task = await start(make(b, cp, Connector(up, FakeUpstream()), watch_interval=5))
    await b.wait_for(lambda: "attached" in b.states())
    cp.st = "SUSPENDING"          # socket never closes by itself
    await b.wait_for(lambda: "suspended" in b.states())
    assert up.closed               # the bridge dropped it
    b.leave()
    await finish(task)


async def test_opening_on_suspended_sandbox_resumes_it():
    b, cp, up = FakeBrowser(), FakeControl("SUSPENDED"), FakeUpstream()
    task = await start(make(b, cp, Connector(up)))
    await b.wait_for(lambda: "attached" in b.states())
    assert cp.resumed == 1
    assert [s["detail"] for s in b.statuses()][:2] == ["checking sandbox state", "resuming sandbox"]
    b.leave()
    await finish(task)


async def test_unexpected_close_retries_with_backoff_then_gives_up():
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    # after the first upstream drops, every reconnect fails
    task = await start(make(b, cp, Connector(up)))
    await b.wait_for(lambda: "attached" in b.states())
    up.drop(1006, "")
    await finish(task, timeout=3)
    st = b.statuses()
    rec = [s for s in st if s["state"] == "reconnecting"]
    assert len(rec) >= 4
    assert [s["retry_in_s"] for s in rec[:4]] == [0.5, 1, 2, 4]
    assert all(s["gives_up_in_s"] <= 60 for s in rec)
    assert st[-1]["state"] == "exited" and "could not reconnect" in st[-1]["detail"]
    assert b.closed == (1011, "upstream unavailable")


async def test_reconnect_succeeds_after_a_failure():
    b, cp = FakeBrowser(), FakeControl()
    up1, up2 = FakeUpstream(), FakeUpstream()
    task = await start(make(b, cp, Connector(up1, OSError("boom"), up2)))
    await b.wait_for(lambda: "attached" in b.states())
    up1.drop(1011, "server error")
    await b.wait_for(lambda: b.states().count("attached") == 2)
    states = b.states()
    assert states[:2] == ["connecting", "attached"] and states.count("reconnecting") == 2
    b.leave()
    await finish(task)


async def test_terminated_sandbox_closes_4410():
    b, cp = FakeBrowser(), FakeControl("TERMINATED")
    task = await start(make(b, cp, Connector()))
    await finish(task)
    assert b.states() == ["connecting", "exited"]
    assert b.closed == (4410, "sandbox terminated")


async def test_terminated_while_attached():
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    task = await start(make(b, cp, Connector(up)))
    await b.wait_for(lambda: "attached" in b.states())
    cp.st = "TERMINATING"
    up.drop(1006, "")
    await finish(task)
    assert b.states()[-1] == "exited" and b.closed[0] == 4410


async def test_no_session_init_counts_as_failure():
    b, cp = FakeBrowser(), FakeControl()
    silent, good = FakeUpstream(init=False), FakeUpstream()
    task = await start(make(b, cp, Connector(silent, good), init_timeout=0.05))
    await b.wait_for(lambda: "attached" in b.states())
    assert "reconnecting" in b.states()
    assert silent.closed
    b.leave()
    await finish(task)


async def test_attach_and_detach_callbacks():
    seen = []
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    bridge = make(b, cp, Connector(up), on_attach=lambda: seen.append("attach"),
                  on_detach=lambda: seen.append("detach"), on_input=lambda: seen.append("input"))
    task = await start(bridge)
    await b.wait_for(lambda: "attached" in b.states())
    b.type(b"x")
    await b.wait_for(lambda: b"x" in up.sent)
    b.leave()
    await finish(task)
    assert seen == ["attach", "input", "detach"]


async def test_heartbeat_to_browser():
    b, cp, up = FakeBrowser(), FakeControl(), FakeUpstream()
    task = await start(make(b, cp, Connector(up), heartbeat_s=25))   # 25 ms at test scale
    await b.wait_for(lambda: sum(1 for k, d in b.out if k == "text" and json.loads(d)["type"] == "heartbeat") >= 2)
    b.leave()
    await finish(task)


async def test_wake_skips_the_poll_interval():
    b, cp = FakeBrowser(), FakeControl()
    up1, up2 = FakeUpstream(), FakeUpstream()
    # A 10-minute watch interval: only wake() can make the bridge notice the suspend in time.
    bridge = make(b, cp, Connector(up1, up2), watch_interval=600_000)
    task = await start(bridge)
    await b.wait_for(lambda: "attached" in b.states())
    cp.st = "SUSPENDED"
    bridge.wake()
    await b.wait_for(lambda: "suspended" in b.states(), timeout=1)
    b.leave()
    await finish(task)


async def test_control_plane_blips_do_not_end_the_session():
    class Flaky(FakeControl):
        def __init__(self):
            super().__init__()
            self.fail = 0

        async def state(self, fresh=False):
            if self.fail:
                self.fail -= 1
                raise RuntimeError("ThrottlingException")
            return await super().state(fresh)

    b, cp, up = FakeBrowser(), Flaky(), FakeUpstream()
    cp.fail = 2                      # first checks fail, then succeed
    task = await start(make(b, cp, Connector(up), watch_interval=1))
    await b.wait_for(lambda: "attached" in b.states())
    cp.fail = 3                      # the watcher hits errors while attached: the session stays up
    await asyncio.sleep(0.05)
    assert b.states() == ["connecting", "attached"] and not up.closed
    b.leave()
    await finish(task)
