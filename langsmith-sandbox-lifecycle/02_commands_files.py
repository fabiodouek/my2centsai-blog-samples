"""Test 2: commands (blocking, streaming, handles, stdin), the command timeout, the files API,
download and service URLs, and what the default egress allow-list lets through."""

import time

import httpx
from langsmith.sandbox import CommandTimeoutError

from common import banner, create, rest, timed

sb = create("02")
print(f"sandbox: {sb.name}")

try:
    banner("Commands")
    r = sb.run("echo out; echo err 1>&2; exit 3")
    print(f"exit={r.exit_code} stdout={r.stdout!r} stderr={r.stderr!r} (no exception on non-zero exit)")
    sb.run("for i in 1 2 3; do echo tick $i; sleep 1; done", on_stdout=lambda s: print("  stream>", s, end=""))

    with timed("run('sleep 10', timeout=2)"):
        try:
            r = sb.run("sleep 10; echo done", timeout=2)
            print(f"no exception: exit={r.exit_code} stdout={r.stdout!r} stderr={r.stderr!r}")
        except CommandTimeoutError as e:
            print(f"CommandTimeoutError: {e}")
    with timed("run('sleep 75') with the default timeout"):
        try:
            r = sb.run("sleep 75; echo done")
            print(f"no exception: exit={r.exit_code} stdout={r.stdout!r} stderr={r.stderr!r}")
        except CommandTimeoutError as e:
            print(f"CommandTimeoutError: {e}")

    h = sb.run("cat", wait=False)
    try:
        h.send_input("hello\n")
        print("send_input with default close_input: accepted")
    except Exception as e:
        print(f"send_input with default close_input: {type(e).__name__}: {e}")
    h2 = sb.run("cat", wait=False, close_input=False)
    h2.send_input("hello over stdin\n")
    h2.close_input()
    print(f"cat with close_input=False echoed: {h2.result.stdout!r}")

    banner("Files")
    sb.write("/root/notes/hello.txt", "hello from my laptop\n")
    print(f"read : {sb.read('/root/notes/hello.txt')!r}")
    print(f"ls   : {[(f.path, f.size_bytes) for f in sb.ls('/root/notes')]}")
    print(f"glob : {[f.path for f in sb.glob('*.txt', '/root/notes').matches]}")
    print(f"grep : {[(m.path, m.line, m.text) for m in sb.grep('hello', '/root/notes').matches]}")
    print(f"stat : {sb.stat('/root/notes/hello.txt')}")

    banner("Download URL (no LangSmith credentials needed)")
    dl = sb.generate_download_url("/root/notes/hello.txt", expires_in_seconds=300)
    print(f"url host: {httpx.URL(dl.download_url).host} expires_at={dl.expires_at}")
    resp = httpx.get(dl.download_url, timeout=30, follow_redirects=True)
    print(f"GET without auth: HTTP {resp.status_code} {resp.text.strip()!r}")
    forever = sb.generate_download_url("/root/notes/hello.txt")
    print(f"no expires_in_seconds -> expires_at={forever.expires_at}")

    banner("Service URL")
    server = sb.run("python3 -m http.server 8000 --directory /root/notes", wait=False, timeout=0)
    time.sleep(2)
    svc = sb.service(8000)
    print(f"service_url host: {httpx.URL(svc.service_url).host}")
    print(f"svc.get('/hello.txt'): {svc.get('/hello.txt').status_code} {svc.get('/hello.txt').text.strip()!r}")
    bare = httpx.get(svc.service_url.rstrip("/") + "/hello.txt", timeout=30)
    print(f"same URL without the token: HTTP {bare.status_code}")
    print(f"browser_url carries the token in the query: {'token' in svc.browser_url}")
    server.kill()

    banner("Egress")
    allow = rest("GET", f"/boxes/{sb.name}")["proxy_config"]["access_control"]["allow_list"]
    print(f"{len(allow)} allow-listed patterns, e.g. {allow[:3]} ... {allow[-3:]}")
    for host in ["https://pypi.org/simple/", "https://registry.npmjs.org/", "https://github.com",
                 "https://api.anthropic.com", "https://api.openai.com", "https://huggingface.co",
                 "https://registry-1.docker.io/v2/", "https://example.com", "https://httpbin.org/get",
                 "https://sts.amazonaws.com", "https://bedrock-runtime.us-east-1.amazonaws.com"]:
        out = sb.run(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 8 {host} || echo ' blocked'").stdout
        print(f"  {host:50s} {out.strip()}")
    print(sb.run("curl -sS --max-time 8 https://example.com 2>&1 | head -c 200; echo").stdout.strip())

    banner("Installing packages")
    for cmd in ["set -o pipefail; pip3 install -q requests 2>&1 | tail -1",
                "set -o pipefail; uv pip install --system -q requests 2>&1 | tail -1",
                "python3 -m venv /root/v && /root/v/bin/pip install -q requests && echo venv-ok"]:
        with timed(cmd.removeprefix('set -o pipefail; ').split(' 2>')[0]):
            r = sb.run(cmd, timeout=180)
        print(f"  exit={r.exit_code} {(r.stdout + r.stderr).strip()[-160:]!r}")
finally:
    sb.delete()
