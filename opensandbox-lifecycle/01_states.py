"""A sandbox's life from create to expiry: its states, a command, renew(), and the TTL running out.

The sandbox gets the shortest timeout (TTL) the server accepts, 60 seconds. A second thread lists
the sandboxes of this run four times a second and prints every state it sees, so the Pending
phase that create() waits through shows up too (the server answers the create call only once the
pod is Running). renew() then moves the expiry later, and the script waits until the controller
deletes the sandbox and the server no longer lists it.

Takes about two minutes. Exit 1 if a step does not do what it should.
"""

import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from opensandbox import SandboxSync
from opensandbox.models.sandboxes import SandboxFilter
from opensandbox.sync import SandboxManagerSync

from common import (
    SANDBOX_NAMESPACE,
    connect_server,
    connection,
    delete_all_sandboxes,
    heading,
    image,
    kubectl,
    own_metadata,
)

SCRIPT = "01_states"
TTL = timedelta(seconds=60)        # the server's minimum
RENEW = timedelta(seconds=90)      # renew() sets the expiry to now + this
GONE_LIMIT = 180                   # seconds after the expiry before we call it a failure
meta = own_metadata(SCRIPT)
t0 = time.monotonic()
lock = threading.Lock()


def say(text: str) -> None:
    with lock:
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] +{time.monotonic() - t0:5.1f}s  {text}",
              flush=True)


def hms(moment: datetime | None) -> str:
    return moment.astimezone(timezone.utc).strftime("%H:%M:%S") if moment else "never"


class Watcher(threading.Thread):
    """Lists this run's sandboxes and reports each change of state, and when the list is empty again."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.seen = False
        self.gone_at: datetime | None = None
        self.states: list[str] = []

    def run(self) -> None:
        last = None
        with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
            while not self.stop.is_set():
                try:
                    infos = manager.list_sandbox_infos(SandboxFilter(metadata={"run": meta["run"]})).sandbox_infos
                except Exception as error:  # a dropped port-forward, for example; try again
                    say(f"(list failed: {type(error).__name__})")
                    self.stop.wait(1)
                    continue
                if infos:
                    status = infos[0].status
                    state = f"{status.state}" + (f" ({status.reason})" if status.reason else "")
                    self.seen = True
                else:
                    state = "not listed"
                if state != last:
                    if infos:
                        self.states.append(str(infos[0].status.state))
                    say(f"state: {state}" if infos else ("not listed any more" if self.seen else "not listed yet"))
                    last = state
                if self.seen and not infos:
                    self.gone_at = datetime.now(timezone.utc)
                    return
                self.stop.wait(0.25)


def expire_time(sandbox_id: str) -> str:
    """spec.expireTime of the BatchSandbox: the controller deletes the sandbox at that time."""
    return kubectl("-n", SANDBOX_NAMESPACE, "get", "batchsandboxes.sandbox.opensandbox.io", sandbox_id,
                   "-o", "jsonpath={.spec.expireTime}", check=False).strip() or "-"


def main() -> int:
    connect_server()
    failures: list[str] = []
    print(f"image {image().split('/', 1)[1]}, timeout (TTL) {TTL.seconds} s, sandboxes tagged run={meta['run']}")
    watcher = Watcher()
    sb = None
    try:
        heading("create")
        watcher.start()
        say(f"Sandbox.create(timeout={TTL.seconds} s)")
        sb = SandboxSync.create(image(), timeout=TTL, ready_timeout=timedelta(seconds=120), metadata=meta,
                                connection_config=connection("proxy"))
        say(f"create() returned: {sb.id}")
        info = sb.get_info()
        first_expiry = info.expires_at
        say(f"created_at {hms(info.created_at)}, expires_at {hms(first_expiry)} "
            f"({(first_expiry - info.created_at).total_seconds():.0f} s), BatchSandbox expireTime {expire_time(sb.id)}")

        heading("a command")
        execution = sb.commands.run("echo hello from $(hostname) on $(uname -r)")
        out = "".join(part.text for part in execution.logs.stdout).strip()
        say(f"exit {execution.exit_code}: {out}")
        if execution.exit_code != 0 or "hello from" not in out:
            failures.append("the command failed")

        heading(f"renew({RENEW.seconds} s)")
        reply = sb.renew(RENEW)
        renewed = sb.get_info().expires_at
        say(f"renew() -> expires_at {hms(reply.expires_at)}; get_info() -> {hms(renewed)} "
            f"({(renewed - first_expiry).total_seconds():+.0f} s); BatchSandbox expireTime {expire_time(sb.id)}")
        if not renewed or renewed <= first_expiry:
            failures.append(f"renew did not move the expiry later ({hms(first_expiry)} -> {hms(renewed)})")

        heading("wait for the expiry")
        wait = (renewed - datetime.now(timezone.utc)).total_seconds()
        say(f"nothing touches the sandbox now; it expires in {wait:.0f} s")
        deadline = time.monotonic() + max(wait, 0) + GONE_LIMIT
        while watcher.is_alive() and time.monotonic() < deadline:
            watcher.join(1)
        if watcher.gone_at is None:
            failures.append(f"still listed {GONE_LIMIT} s after the expiry")
        else:
            late = (watcher.gone_at - renewed).total_seconds()
            say(f"gone from list_sandbox_infos {late:.0f} s after expires_at")
            try:
                sb.get_info()
                failures.append("get_info() still answers after the sandbox is gone")
            except Exception as error:
                say(f"get_info() now: {type(error).__name__}: {str(error)[:90]}")
            left = kubectl("-n", SANDBOX_NAMESPACE, "get", "batchsandboxes.sandbox.opensandbox.io,pods",
                           "-l", f"run={meta['run']}", "-o", "name", check=False).split()
            say(f"BatchSandbox and pods left in the cluster: {len(left)}")
    except Exception as error:  # SDK or kubectl trouble: one line, then clean up
        failures.append(f"{type(error).__name__}: {str(error)[:200]}")
    finally:
        watcher.stop.set()
        if sb is not None:
            sb.close()
        if watcher.gone_at is None:
            delete_all_sandboxes(metadata={"run": meta["run"]})

    heading("states seen")
    print("  " + " -> ".join(dict.fromkeys(watcher.states)) + (" -> gone" if watcher.gone_at else ""))
    if failures:
        print("\n" + "\n".join(f"  FAIL  {item}" for item in failures))
        return 1
    print("\nall steps passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
