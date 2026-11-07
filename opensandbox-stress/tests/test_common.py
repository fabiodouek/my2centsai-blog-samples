"""common.run_child(): a server change started by a script survives the script being interrupted.
The parent runs in a subprocess (common.py installs signal handlers and loads the sibling
directories' modules on import). Needs ../opensandbox-eks/env.sh and the venv; no cluster."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not (ROOT.parent / "opensandbox-eks" / "env.sh").is_file(),
                                reason="../opensandbox-eks/env.sh is not here")


def parent_script(marker: Path) -> str:
    child = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('done')"
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        import common
        print("STARTED", flush=True)
        try:
            done = common.run_child([sys.executable, "-c", {child!r}], timeout=60)
        except KeyboardInterrupt:
            print("INTERRUPTED-AFTER-CHILD", flush=True)
            sys.exit(3)
        print("CHILD", done.returncode, flush=True)
    """)


@pytest.mark.parametrize("sig, code", [(signal.SIGINT, 3), (signal.SIGTERM, 128 + signal.SIGTERM),
                                       (signal.SIGHUP, 128 + signal.SIGHUP)])
def test_the_child_finishes_when_the_parent_is_interrupted(tmp_path, sig, code):
    marker = tmp_path / "child-finished"
    env = {**os.environ, "OSB_STRESS_LOGS": str(tmp_path / "logs")}
    proc = subprocess.Popen([sys.executable, "-c", parent_script(marker)], cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
    assert proc.stdout.readline().strip() == "STARTED"
    import time
    time.sleep(0.8)                         # the child is running now
    os.killpg(proc.pid, sig)                # the parent's process group only: the child has its own session
    out, err = proc.communicate(timeout=60)
    assert marker.read_text() == "done", (out, err)
    # After a hang-up the script's output goes to logs/hangup.log (the terminal is gone).
    said = (tmp_path / "logs" / "hangup.log").read_text() if sig == signal.SIGHUP else out
    assert "interrupted: waiting for" in said
    assert proc.returncode == code, (out, err)


def test_a_child_that_finishes_normally(tmp_path):
    marker = tmp_path / "child-finished"
    done = subprocess.run([sys.executable, "-c", parent_script(marker)], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=60, env={**os.environ, "OSB_STRESS_LOGS": str(tmp_path / "logs")})
    assert "CHILD 0" in done.stdout and marker.exists()


def test_a_closed_terminal_still_runs_the_restore(tmp_path):
    """A real terminal (a pty) closes under the script, then SIGHUP arrives twice, the second while
    the restore runs: server_session()'s finally must still run to the end (its first print would
    otherwise fail with EIO, and the second SIGHUP would abort it), the script must exit 129, and
    the clean-up's output must be in logs/hangup.log."""
    import pty
    import select
    import time

    logs = tmp_path / "logs"
    marker = tmp_path / "restored"
    script = textwrap.dedent(f"""
        import sys, time, pathlib
        sys.path.insert(0, {str(ROOT)!r})
        import common

        def fake_restore(force=False):
            print("restoring the baseline", flush=True)
            time.sleep(1.5)                  # the second SIGHUP arrives while this runs
            print("restore finished", flush=True)
            pathlib.Path({str(marker)!r}).write_text("done")
            return []

        common.restore_baseline = fake_restore
        with common.server_session():
            print("STARTED", flush=True)
            time.sleep(60)
    """)
    master, slave = pty.openpty()
    env = {**os.environ, "OSB_STRESS_LOGS": str(logs)}
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT), stdin=slave, stdout=slave, stderr=slave,
                            env=env, start_new_session=True)
    os.close(slave)
    seen = b""
    deadline = time.monotonic() + 60
    while b"STARTED" not in seen and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 1)
        if ready:
            try:
                seen += os.read(master, 4096)
            except OSError:
                break
    assert b"STARTED" in seen, seen
    os.close(master)                         # the terminal window closes
    time.sleep(0.3)
    # A closed terminal delivers SIGHUP twice (the kernel to the foreground group, the shell to its
    # jobs), the second while the clean-up runs: it must not abort the restore.
    os.killpg(proc.pid, signal.SIGHUP)
    time.sleep(0.5)
    os.killpg(proc.pid, signal.SIGHUP)
    assert proc.wait(timeout=60) == 128 + signal.SIGHUP
    assert marker.read_text() == "done"
    log = (logs / "hangup.log").read_text()
    assert "the terminal hung up" in log and "restoring the baseline" in log and "server back to the baseline" in log
    assert "restore finished" in log and log.count("the terminal hung up") == 1 and "FAIL" not in log


def test_a_background_command_needs_its_own_timeout(tmp_path):
    """execd kills a background command's process group when its timeout runs out, so run_in()
    refuses a background command without an explicit timeout, and passes the one it is given."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        import common

        class Commands:
            def run(self, command, opts):
                print("OPTS", int(opts.timeout.total_seconds()), opts.background, flush=True)
                raise SystemExit(0)

        class Sandbox:
            commands = Commands()

        try:
            common.run_in(Sandbox(), "sleep 1000", background=True)
        except ValueError as error:
            print("REFUSED", "timeout" in str(error), flush=True)
        common.run_in(Sandbox(), "sleep 1000", timeout=960, background=True)
    """)
    done = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
                          env={**os.environ, "OSB_STRESS_LOGS": str(tmp_path / "logs")})
    assert "REFUSED True" in done.stdout and "OPTS 960 True" in done.stdout, done.stdout + done.stderr
