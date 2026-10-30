"""Test 4: stop and start a container sandbox three times, what survives, what a stopped sandbox does
when you touch it, archive and restore, and what pause() does on a container."""

import time

import requests
from daytona import CreateSandboxFromSnapshotParams, Daytona, SessionExecuteRequest

from common import SAFETY_TTL, banner, labels, timed

daytona = Daytona()
sbx = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="04"), ttl_minutes=SAFETY_TTL))
home = sbx.get_user_home_dir()
print(f"sandbox: {sbx.id}")


def start_counter() -> None:
    sbx.process.create_session("counter")
    sbx.process.execute_session_command("counter", SessionExecuteRequest(
        command="python3 -c \"import time\nn=0\nwhile True:\n n+=1; open('/tmp/n','w').write(str(n)); time.sleep(1)\"",
        run_async=True))
    sbx.process.create_session("srv")
    sbx.process.execute_session_command("srv", SessionExecuteRequest(
        command=f"cd {home} && python3 -m http.server 3000", run_async=True))


sbx.fs.upload_file(b"written before the first stop\n", f"{home}/state.txt")
start_counter()
time.sleep(3)

for cycle in range(1, 4):
    banner(f"Cycle {cycle}")
    before = sbx.process.exec("cat /tmp/n").result.strip()
    token_before = sbx.get_preview_link(3000).token
    with timed("stop()"):
        sbx.stop()
    with timed("start()"):
        sbx.start()
    print(f"file   : {sbx.process.exec(f'cat {home}/state.txt').result.strip()}")
    n1 = sbx.process.exec("cat /tmp/n").result.strip()
    time.sleep(2)
    n2 = sbx.process.exec("cat /tmp/n").result.strip()
    print(f"counter: {before} at stop, {n1} -> {n2} two seconds after start "
          f"({'still counting' if n1 != n2 else 'process gone, /tmp kept'})")
    print(f"preview token changed: {sbx.get_preview_link(3000).token != token_before}")
    start_counter()
    time.sleep(2)

banner("Touching a stopped sandbox")
link = sbx.get_preview_link(3000)
sbx.stop()
try:
    r = sbx.process.exec("echo hi")
    print(f"exec on stopped: exit={r.exit_code} {r.result.strip()!r}")
except Exception as e:
    print(f"exec on stopped: {type(e).__name__}: {str(e)[:120]}")
resp = requests.get(link.url, timeout=20, headers={"x-daytona-preview-token": link.token})
print(f"preview on stopped: HTTP {resp.status_code} {resp.text.strip()[:80]!r}")
time.sleep(5)
print(f"state after both: {daytona.get(sbx.id).state.value}")

banner("Archive and restore")
with timed("archive()"):
    sbx.archive()
    while daytona.get(sbx.id).state.value != "archived":
        time.sleep(1)
print(f"state: {daytona.get(sbx.id).state.value}")
with timed("start() from archived"):
    sbx.start(timeout=300)
print(f"file after restore: {sbx.process.exec(f'cat {home}/state.txt').result.strip()}")

banner("pause() on a container")
try:
    sbx.pause()
    print("paused?!")
except Exception as e:
    print(f"{type(e).__name__}: {str(e)[:160]}")

sbx.delete()
