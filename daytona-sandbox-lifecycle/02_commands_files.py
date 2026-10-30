"""Test 2: run commands (blocking, session, background), work with files, preview URLs, and the exec timeout."""

import time

import requests
from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaProcessExecutionTimeoutError, SessionExecuteRequest

from common import SAFETY_TTL, banner, labels, timed

daytona = Daytona()
sbx = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="02"), ttl_minutes=SAFETY_TTL))
print(f"sandbox: {sbx.id}")
home = sbx.get_user_home_dir()

banner("Blocking command")
r = sbx.process.exec("python3 --version && whoami && pwd")
print(f"exit={r.exit_code}\n{r.result}")

banner("stdout, stderr and exit code")
r = sbx.process.exec("echo out; echo err 1>&2; exit 3")
print(f"exit={r.exit_code} result={r.result!r}")

banner("Session command, streamed logs")
sbx.process.create_session("ticks")
cmd = sbx.process.execute_session_command(
    "ticks", SessionExecuteRequest(command="for i in 1 2 3; do echo tick $i; sleep 1; done", run_async=True)
)
time.sleep(3.5)
print(sbx.process.get_session_command_logs("ticks", cmd.cmd_id).stdout.strip())

banner("Files: upload, download, list")
sbx.fs.create_folder(f"{home}/notes", "755")
sbx.fs.upload_file(b"hello from my laptop\n", f"{home}/notes/hello.txt")
sbx.fs.upload_file(b"a", f"{home}/notes/a.txt")
sbx.fs.upload_file(b"b", f"{home}/notes/b.txt")
print(sbx.fs.download_file(f"{home}/notes/hello.txt").decode().strip())
for f in sbx.fs.list_files(f"{home}/notes"):
    print(f"  {'dir ' if f.is_dir else 'file'} {f.name} ({f.size} B)")

banner("Background server + preview URL")
sbx.process.create_session("srv")
sbx.process.execute_session_command(
    "srv", SessionExecuteRequest(command=f"cd {home}/notes && python3 -m http.server 3000", run_async=True)
)
time.sleep(1.5)
link = sbx.get_preview_link(3000)
print(f"preview URL: {link.url}")
resp = requests.get(f"{link.url}/hello.txt", timeout=15)
print(f"no token     -> HTTP {resp.status_code}")
resp = requests.get(f"{link.url}/hello.txt", timeout=15, headers={"x-daytona-preview-token": link.token})
print(f"token header -> HTTP {resp.status_code} {resp.text.strip()[:60]!r}")
resp = requests.get(f"{link.url}/hello.txt", timeout=15, headers={
    "x-daytona-preview-token": link.token, "X-Daytona-Skip-Preview-Warning": "true"})
print(f"token + skip -> HTTP {resp.status_code} {resp.text.strip()[:60]!r}")
signed = sbx.create_signed_preview_url(3000, expires_in_seconds=60)
resp = requests.get(f"{signed.url}/hello.txt", timeout=15, headers={"X-Daytona-Skip-Preview-Warning": "true"})
print(f"signed URL   -> HTTP {resp.status_code} {resp.text.strip()[:60]!r}")

banner("Exec timeout")
with timed("exec('sleep 75') with no timeout"):
    r = sbx.process.exec("sleep 75")  # docs say 10s default; it runs to completion
    print(f"  exit={r.exit_code}")
with timed("exec('sleep 10', timeout=3)"):
    try:
        sbx.process.exec("sleep 10", timeout=3)
    except DaytonaProcessExecutionTimeoutError as e:
        print(f"  {type(e).__name__}: {e}")

sbx.delete()
