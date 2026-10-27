"""Test 5: auto-pause on timeout, then wake the sandbox with an SDK call and with an HTTP request to its public URL."""

import time
import urllib.request

from e2b import Sandbox

from common import banner, tagged, timed

sbx = Sandbox.create(
    timeout=20,
    metadata=tagged(test="05"),
    lifecycle={"on_timeout": "pause", "auto_resume": True},
)
print(f"sandbox: {sbx.sandbox_id}")
print(f"lifecycle: {sbx.get_info().lifecycle}")

sbx.files.write("/home/user/hello.txt", "hello from a paused sandbox\n")
sbx.commands.run("cd /home/user && python3 -m http.server 3000", background=True, timeout=0)
url = f"https://{sbx.get_host(3000)}/hello.txt"
print(f"preview URL: {url}")


def wait_for_pause(limit: int = 120) -> None:
    start = time.time()
    while time.time() - start < limit:
        state = Sandbox.get_info(sbx.sandbox_id).state
        if str(state).lower().endswith("paused"):
            print(f"paused after ~{time.time() - start:.0f}s of polling")
            return
        time.sleep(5)
    raise RuntimeError("sandbox never paused")


banner("Wait for timeout -> auto-pause")
wait_for_pause()

banner("Wake with files.read()")
with timed("files.read on paused sandbox"):
    content = sbx.files.read("/home/user/hello.txt")
print(content.strip())
info = sbx.get_info()
print(f"state after read: {info.state}, new end_at: {info.end_at}")

banner("Pause again, wake with HTTP")
sbx.pause()
print(f"state: {Sandbox.get_info(sbx.sandbox_id).state}")
with timed("GET preview URL on paused sandbox"):
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            print(f"HTTP {resp.status}: {resp.read().decode().strip()}")
    except Exception as e:
        print(f"request failed: {e}")
print(f"state after HTTP: {Sandbox.get_info(sbx.sandbox_id).state}")

sbx.kill()
