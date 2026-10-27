"""Test 4: pause and resume over several cycles. Checks that files, a running process and its in-memory state survive,
then tries to reattach to that process's stdout stream after the last resume."""

import sys
import threading
import time

from e2b import Sandbox

from common import banner, tagged, timed

CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 3

sbx = Sandbox.create(timeout=600, metadata=tagged(test="04"))
print(f"sandbox: {sbx.sandbox_id}")

# A counter that only lives in process memory, echoed to stdout and mirrored to a file.
counter = sbx.commands.run(
    "python3 -u -c \"import time\n"
    "i = 0\n"
    "while True:\n"
    "    i += 1\n"
    "    print(i)\n"
    "    open('/tmp/counter', 'w').write(str(i))\n"
    "    time.sleep(1)\"",
    background=True,
    timeout=0,
)
pid = counter.pid
sbx.files.write("/home/user/state.txt", "written before the first pause\n")
time.sleep(3)

for cycle in range(1, CYCLES + 1):
    banner(f"Cycle {cycle}")
    sbx.files.write(f"/home/user/cycle-{cycle}.txt", f"cycle {cycle}\n")
    before = sbx.files.read("/tmp/counter")

    with timed("pause()"):
        sbx.pause()
    print(f"state: {Sandbox.get_info(sbx.sandbox_id).state}")

    time.sleep(10)  # paused: wall clock moves, the sandbox doesn't

    with timed("connect() / resume"):
        sbx.connect()
    after = sbx.files.read("/tmp/counter")
    alive = sbx.commands.run(f"kill -0 {pid} && echo alive || echo dead").stdout.strip()
    files = sorted(e.name for e in sbx.files.list("/home/user") if e.name.endswith(".txt"))
    print(f"counter before={before} after={after} (continues, not reset => memory restored)")
    print(f"pid {pid}: {alive}")
    print(f"files: {files}")

banner("Reattach to the counter's stdout after resume")
lines: list[str] = []
handle = sbx.commands.connect(pid, timeout=0)
t = threading.Thread(target=lambda: _safe_wait(handle, lines), daemon=True)


def _safe_wait(h, sink):
    try:
        h.wait(on_stdout=lambda s: sink.append(s.strip()))
    except Exception as e:  # killing the process ends wait() with a non-zero exit
        sink.append(f"<wait ended: {type(e).__name__}>")


t.start()
time.sleep(6)
received = [line for line in lines if line.isdigit()]
print(f"lines received in 6s: {len(received)} {received[-3:]}")
print("stream OK" if len(received) >= 3 else "stream stalled (see E2B#1587)")

sbx.commands.kill(pid)
sbx.kill()
