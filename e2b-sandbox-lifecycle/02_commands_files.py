"""Test 2: run commands (blocking, streaming, background), work with files, and hit the 60s command timeout."""

import time

from e2b import Sandbox, TimeoutException

from common import banner, tagged, timed

sbx = Sandbox.create(timeout=300, metadata=tagged(test="02"))
print(f"sandbox: {sbx.sandbox_id}")

banner("Blocking command")
r = sbx.commands.run("python3 --version && whoami && pwd")
print(f"exit={r.exit_code}\n{r.stdout}")

banner("Streaming stdout")
sbx.commands.run(
    "for i in 1 2 3; do echo tick $i; sleep 1; done",
    on_stdout=lambda line: print(f"  stream> {line.strip()}"),
)

banner("Files: write, read, list")
sbx.files.write("/home/user/notes/hello.txt", "hello from my laptop\n")
sbx.files.write_files([
    {"path": "/home/user/notes/a.txt", "data": "a"},
    {"path": "/home/user/notes/b.txt", "data": "b"},
])
print(sbx.files.read("/home/user/notes/hello.txt").strip())
for entry in sbx.files.list("/home/user/notes"):
    print(f"  {entry.type.value:5} {entry.name}")

banner("Background process with timeout=0")
server = sbx.commands.run(
    "cd /home/user/notes && python3 -m http.server 3000",
    background=True,
    timeout=0,  # without this the default 60s command timeout kills it
)
time.sleep(1)
print(f"server pid: {server.pid}")
for p in sbx.commands.list():
    print(f"  running: pid={p.pid} cmd={p.cmd} {' '.join(p.args)}")
print(f"preview URL: https://{sbx.get_host(3000)}/hello.txt")
print(sbx.commands.run("curl -s localhost:3000/hello.txt").stdout.strip())

banner("Command timeout")
with timed("sleep 10 with timeout=3"):
    try:
        sbx.commands.run("sleep 10", timeout=3)
    except TimeoutException as e:
        print(f"  TimeoutException: {e}")

sbx.kill()
