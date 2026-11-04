"""Browser <-> MicroVM shell bridge for /ws/terminal/{id}.

Browsers can't set headers on a WebSocket, and the MicroVM shell wants `X-aws-proxy-auth`, so the backend
holds the upstream connection: wss://<endpoint>/shell with a freshly minted shell token. The first upstream
frame is text JSON {"type":"session_init",...}; after that the client sends resize as text JSON and
keystrokes as binary frames; output comes back as binary (or text). Closing the shell sends `1000 shell
exited`. Open connections die on suspend/resume, so the bridge reconnects (and reattaches tmux).

Browser protocol (after the auth message, handled by the route):
  browser -> backend: binary = keystrokes; text JSON {"type":"resize","rows","cols"}; {"type":"input","data"};
                      {"type":"ping"} (answered with {"type":"pong"})
  backend -> browser: binary = terminal output; text JSON {"type":"status","state",...} where state is one of
                      connecting | attached | reconnecting | suspended | exited; {"type":"heartbeat"} every 25 s
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Protocol

log = logging.getLogger("terminal")

TMUX_CMD = (b' if command -v tmux >/dev/null 2>&1; then tmux new -A -s main; '
            b'else echo "(tmux not installed in this image: plain shell)"; fi\n')
DEFAULT_SIZE = (40, 120)
RECONNECT_WINDOW_S = 60.0
BACKOFF = (0.5, 1, 2, 4, 8, 15)
FAST_POLL = 0.5          # control-plane poll while suspended, for the first FAST_WINDOW_S seconds
FAST_WINDOW_S = 120.0
MAX_BUFFER = 64 * 1024
SUSPENDED = ("SUSPENDING", "SUSPENDED")
GONE = ("TERMINATING", "TERMINATED")


class UpstreamClosed(Exception):
    def __init__(self, code: int | None, reason: str = ""):
        super().__init__(f"{code} {reason}".strip())
        self.code, self.reason = code, reason or ""


class Browser(Protocol):
    async def recv(self) -> str | bytes | None: ...  # None = browser went away
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class Upstream(Protocol):
    async def recv(self) -> str | bytes: ...  # raises UpstreamClosed
    async def send(self, data: str | bytes) -> None: ...
    async def close(self) -> None: ...


class ControlPlane(Protocol):
    async def state(self, fresh: bool = False) -> str: ...
    async def resume(self) -> None: ...
    async def shell_token(self) -> str: ...


Connector = Callable[[str], Awaitable[Upstream]]  # token -> connected upstream


def parse_resize(msg: dict) -> tuple[int, int] | None:
    try:
        rows, cols = int(msg.get("rows")), int(msg.get("cols"))
    except (TypeError, ValueError):
        return None
    if not (1 <= rows <= 1000 and 1 <= cols <= 1000):
        return None
    return rows, cols


class TerminalBridge:
    def __init__(self, browser: Browser, cp: ControlPlane, connect: Connector, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 watch_interval: float = 3.0, suspended_poll: float = 2.0, init_timeout: float = 10.0,
                 first_resize_wait: float = 1.5, on_attach: Callable[[], None] | None = None,
                 on_detach: Callable[[], None] | None = None, on_input: Callable[[], None] | None = None,
                 heartbeat_s: float = 25.0):
        self.browser, self.cp, self.connect = browser, cp, connect
        self.sleep, self.clock = sleep, clock
        self.watch_interval, self.suspended_poll = watch_interval, suspended_poll
        self.init_timeout, self.first_resize_wait = init_timeout, first_resize_wait
        self.on_attach, self.on_detach, self.on_input = on_attach, on_detach, on_input
        self.heartbeat_s = heartbeat_s
        self.size: tuple[int, int] | None = None
        self._got_size = asyncio.Event()
        self.up: Upstream | None = None
        self._buffer: list[bytes] = []
        self._buffered = 0
        self._lock = asyncio.Lock()          # serialises the attach sequence with browser input
        self.browser_gone = asyncio.Event()
        self._wake = asyncio.Event()         # set by wake(): re-check the control plane now
        self.statuses: list[dict] = []       # what we told the browser (handy for tests and logs)
        self.closed_with: tuple[int, str] | None = None
        self._last_close: UpstreamClosed | None = None
        self._last_error: str | None = None

    def wake(self) -> None:
        """The backend just suspended/resumed/terminated this sandbox: don't wait for the next poll."""
        self._wake.set()

    async def _nap(self, seconds: float) -> None:
        sleeper = asyncio.ensure_future(self.sleep(seconds))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (sleeper, waker):
                t.cancel()
            self._wake.clear()

    # ---- browser side ----
    async def status(self, state: str, detail: str = "", **extra) -> None:
        msg = {"type": "status", "state": state, "detail": detail, **extra}
        self.statuses.append(msg)
        try:
            await self.browser.send_text(json.dumps(msg))
        except Exception:
            self.browser_gone.set()

    async def _close_browser(self, code: int, reason: str) -> None:
        self.closed_with = (code, reason)
        try:
            await self.browser.close(code, reason)
        except Exception:
            pass

    async def _to_upstream(self, data: str | bytes) -> None:
        async with self._lock:
            if self.up is not None:
                try:
                    await self.up.send(data)
                    return
                except UpstreamClosed:
                    pass  # the upstream reader will notice and reconnect
            if isinstance(data, bytes) and self._buffered + len(data) <= MAX_BUFFER:
                self._buffer.append(data)
                self._buffered += len(data)

    async def _read_browser(self) -> None:
        try:
            while True:
                m = await self.browser.recv()
                if m is None:
                    break
                if isinstance(m, bytes):
                    if self.on_input:
                        self.on_input()
                    await self._to_upstream(m)
                    continue
                try:
                    msg = json.loads(m)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                t = msg.get("type")
                if t == "resize":
                    size = parse_resize(msg)
                    if size:
                        self.size = size
                        self._got_size.set()
                        async with self._lock:
                            if self.up is not None:
                                try:
                                    await self.up.send(json.dumps({"type": "resize", "rows": size[0], "cols": size[1]}))
                                except UpstreamClosed:
                                    pass
                elif t == "input" and isinstance(msg.get("data"), str):
                    if self.on_input:
                        self.on_input()
                    await self._to_upstream(msg["data"].encode())
                elif t == "ping":
                    try:
                        await self.browser.send_text(json.dumps({"type": "pong"}))
                    except Exception:
                        break
        except Exception as e:
            log.debug("browser reader ended: %s", e)
        finally:
            self.browser_gone.set()

    # ---- session ----
    async def _heartbeat(self) -> None:
        """CloudFront drops a WebSocket after 60 s without traffic; an idle terminal sends nothing, so send a
        small text frame every 25 s (the browser ignores it). The upstream shell has protocol pings."""
        while self.heartbeat_s > 0:
            await self.sleep(self.heartbeat_s)
            try:
                await self.browser.send_text(json.dumps({"type": "heartbeat"}))
            except Exception:
                self.browser_gone.set()
                return

    async def run(self) -> None:
        reader = asyncio.create_task(self._read_browser())
        beat = asyncio.create_task(self._heartbeat())
        try:
            await self._race(self._session())
        finally:
            reader.cancel()
            beat.cancel()
            await self._drop_upstream()

    async def _race(self, coro) -> None:
        """Run `coro` until it finishes or the browser goes away."""
        main = asyncio.create_task(coro)
        gone = asyncio.create_task(self.browser_gone.wait())
        done, _ = await asyncio.wait({main, gone}, return_when=asyncio.FIRST_COMPLETED)
        for t in (main, gone):
            if t not in done:
                t.cancel()
        for t in (main, gone):
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("terminal session failed")
                await self.status("exited", "internal error")
                await self._close_browser(1011, "internal error")

    async def _drop_upstream(self) -> None:
        async with self._lock:
            up, self.up = self.up, None
        if up is not None:
            try:
                await up.close()
            except Exception:
                pass

    async def _state(self) -> str:
        """get_microvm with a few retries: a throttled or failed control-plane call shouldn't end a session."""
        for i in range(5):
            try:
                return await self.cp.state(fresh=True)
            except Exception as e:
                if i == 4:
                    raise
                log.info("get_microvm failed (%s); retrying", type(e).__name__)
                await self._nap(min(2 ** i, 8))
        raise RuntimeError("unreachable")

    async def _wait_state(self, pred, poll: float, timeout: float | None = None, fast_for: float = 0.0) -> str:
        """Poll the control plane until pred(state). For the first `fast_for` seconds poll every
        FAST_POLL so a resume reattaches within about a second; wake() cuts any wait short."""
        start = self.clock()
        while True:
            st = await self._state()
            if pred(st) or (timeout is not None and self.clock() - start > timeout):
                return st
            await self._nap(FAST_POLL if self.clock() - start < fast_for else poll)

    async def _session(self) -> None:
        may_resume = True       # opening a terminal on a suspended sandbox resumes it; a suspend seen later doesn't
        fail_since: float | None = None
        attempt = 0
        await self.status("connecting", "checking sandbox state")
        while True:
            st = await self._state()
            if st in GONE:
                await self.status("exited", "sandbox terminated", vm_state=st)
                return await self._close_browser(4410, "sandbox terminated")
            if st in SUSPENDED:
                if may_resume:
                    if st == "SUSPENDING":
                        await self._wait_state(lambda s: s != "SUSPENDING", 0.5, timeout=30)
                    await self.status("connecting", "resuming sandbox")
                    try:
                        await self.cp.resume()
                    except Exception as e:
                        log.info("resume on connect: %s", e)
                    await self._wait_state(lambda s: s != "SUSPENDED", 0.25, timeout=30)
                    may_resume = False
                else:
                    await self.status("suspended", "sandbox is suspended; resume it to reconnect", vm_state=st)
                    await self._wait_state(lambda s: s not in SUSPENDED, self.suspended_poll, fast_for=FAST_WINDOW_S)
                    await self.status("connecting", "sandbox resumed")
                    fail_since, attempt = None, 0
                continue
            if st == "PENDING":
                await self._wait_state(lambda s: s != "PENDING", 0.25, timeout=60)
                continue

            # RUNNING: open the shell.
            outcome = await self._attach_once()
            if outcome == "attached-then-closed":
                fail_since, attempt = None, 0
                may_resume = False
                reason = self._last_close
                st = await self._state()
                if st in GONE or st in SUSPENDED:
                    continue  # the top of the loop reports it
                if reason and reason.code == 1000:
                    await self.status("exited", reason.reason or "shell exited")
                    return await self._close_browser(1000, "shell exited")
            # Connect failure or unexpected close while RUNNING: back off, up to 60 s.
            now = self.clock()
            fail_since = fail_since or now
            remaining = RECONNECT_WINDOW_S - (now - fail_since)
            if remaining <= 0:
                await self.status("exited", "could not reconnect to the sandbox shell")
                return await self._close_browser(1011, "upstream unavailable")
            delay = min(BACKOFF[min(attempt, len(BACKOFF) - 1)], max(0.5, remaining))
            attempt += 1
            await self.status("reconnecting", self._last_error or "shell connection closed",
                              attempt=attempt, retry_in_s=delay, gives_up_in_s=round(remaining))
            await self.sleep(delay)

    async def _attach_once(self) -> str:
        """Connect, attach and pump until the upstream closes. Returns 'failed' or 'attached-then-closed'."""
        self._last_close, self._last_error = None, None
        try:
            token = await self.cp.shell_token()
            up = await self.connect(token)
        except Exception as e:
            self._last_error = f"connect failed: {type(e).__name__}"
            return "failed"
        try:
            first = await asyncio.wait_for(up.recv(), self.init_timeout)
        except (asyncio.TimeoutError, UpstreamClosed, Exception) as e:
            self._last_error = f"no session_init: {type(e).__name__}"
            try:
                await up.close()
            except Exception:
                pass
            return "failed"
        session_id, early = None, None
        if isinstance(first, str):
            try:
                j = json.loads(first)
                if isinstance(j, dict) and j.get("type") == "session_init":
                    session_id = j.get("session_id")
                else:
                    early = first.encode()
            except ValueError:
                early = first.encode()
        else:
            early = first

        if self.size is None:
            try:
                await asyncio.wait_for(self._got_size.wait(), self.first_resize_wait)
            except asyncio.TimeoutError:
                pass
        rows, cols = self.size or DEFAULT_SIZE
        try:
            async with self._lock:
                await up.send(json.dumps({"type": "resize", "rows": rows, "cols": cols}))
                await up.send(TMUX_CMD)
                for chunk in self._buffer:
                    await up.send(chunk)
                self._buffer, self._buffered = [], 0
                self.up = up
        except UpstreamClosed as e:
            self._last_error = f"closed during attach: {e}"
            return "failed"
        if early:
            await self.browser.send_bytes(early)
        await self.status("attached", "", session_id=session_id)
        if self.on_attach:
            self.on_attach()
        try:
            await self._pump(up)
        finally:
            await self._drop_upstream()
            if self.on_detach:
                self.on_detach()
        return "attached-then-closed"

    async def _pump(self, up: Upstream) -> None:
        reader = asyncio.create_task(self._pump_output(up))
        watcher = asyncio.create_task(self._watch_state())
        done, pending = await asyncio.wait({reader, watcher}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _pump_output(self, up: Upstream) -> None:
        try:
            while True:
                m = await up.recv()
                if isinstance(m, bytes):
                    await self.browser.send_bytes(m)
                else:
                    if m.startswith("{"):
                        try:
                            j = json.loads(m)
                            if isinstance(j, dict) and j.get("type") in ("session_init", "pong", "ping"):
                                continue
                        except ValueError:
                            pass
                    await self.browser.send_bytes(m.encode())
        except UpstreamClosed as e:
            self._last_close = e
        except Exception as e:
            self._last_close = UpstreamClosed(None, f"{type(e).__name__}")
            if self.browser_gone.is_set():
                return

    async def _watch_state(self) -> None:
        """Control-plane check while attached (doesn't count as endpoint traffic): a suspend or terminate
        can leave the socket hanging until keepalive gives up, so notice it here and drop the upstream."""
        while True:
            await self._nap(self.watch_interval)
            try:
                st = await self.cp.state(fresh=True)
            except Exception as e:
                log.info("watch: get_microvm failed (%s)", type(e).__name__)
                continue
            if st in SUSPENDED or st in GONE:
                self._last_close = UpstreamClosed(None, f"sandbox {st.lower()}")
                return
