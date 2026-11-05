"""Test 3: commands, files and the endpoint. Auth tokens and port scopes, token expiry, whether the
hooks port is reachable from outside, exit codes and long commands, SSE streaming, the endpoint's
bandwidth, WebSockets, HTTP/2, the shell (SHELL_INGRESS), and NO_INGRESS."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time

import httpx
import websockets

from common import (HTTP_INGRESS, NO_INGRESS, SHELL_INGRESS, VM, banner, err, mvm, redact, run, timed)

SLOW = "--slow" in sys.argv  # token-expiry check adds ~2 minutes

vm = run("sandbox", "03-endpoint", ingress=(HTTP_INGRESS, SHELL_INGRESS))
vm.wait("RUNNING")
vm.ready()
url = f"https://{vm.endpoint}"
h = httpx.Client(timeout=60)

banner("Tokens and ports")
print("no token:                ", h.get(f"{url}/health").status_code)
print("garbage token:           ", h.get(f"{url}/health", headers={"X-aws-proxy-auth": "nope"}).status_code)
t8080 = vm.token(8080)
print("8080 token, default port:", h.get(f"{url}/health", headers={"X-aws-proxy-auth": t8080}).status_code)
print("8080 token, port 8081:   ", h.get(f"{url}/health", headers={"X-aws-proxy-auth": t8080, "X-aws-proxy-port": "8081"}).status_code)
other = run("sandbox", "03-other")
other.wait("RUNNING")
print("token for VM A on VM B:  ", h.get(f"https://{other.endpoint}/health", headers={"X-aws-proxy-auth": t8080}).status_code)
other.terminate()
rng = mvm.create_microvm_auth_token(microvmIdentifier=vm.id, expirationInMinutes=5,
                                    allowedPorts=[{"range": {"startPort": 8000, "endPort": 8100}}])["authToken"]["X-aws-proxy-auth"]
print("range 8000-8100, 8080:   ", h.get(f"{url}/health", headers={"X-aws-proxy-auth": rng}).status_code)
print("range 8000-8100, 9001:   ", h.post(f"{url}/aws/lambda-microvms/runtime/v1/run", headers={"X-aws-proxy-auth": rng, "X-aws-proxy-port": "9001"}).status_code)

banner("Can a token holder call the lifecycle hooks?")
t9001 = vm.token(9001)
r = h.post(f"{url}/aws/lambda-microvms/runtime/v1/run", headers={"X-aws-proxy-auth": t9001, "X-aws-proxy-port": "9001"},
           json={"microvmId": "forged", "runHookPayload": "from outside"})
print("POST /run on the hooks port through the endpoint:", r.status_code, r.text[:80])
print("hook events the agent saw (hook, payload bytes, peer address):")
for e in vm.events():
    print("  ", e["hook"], e.get("payload_len"), e.get("peer"))
hd = vm.request("GET", "/headers").json()
print("a normal request as the app sees it: peer", hd["peer"], "| X-aws-proxy-* headers left:",
      [k for k in hd["headers"] if k.lower().startswith("x-aws-proxy")] or "none")

if SLOW:
    banner("Token expiry (1-minute token)")
    short = mvm.create_microvm_auth_token(microvmIdentifier=vm.id, expirationInMinutes=1,
                                          allowedPorts=[{"port": 8080}])["authToken"]["X-aws-proxy-auth"]
    start = time.time()
    for at in (0, 65, 95, 125, 185):
        time.sleep(max(0, start + at - time.time()))
        print(f"  t+{at:3d}s:", h.get(f"{url}/health", headers={"X-aws-proxy-auth": short}).status_code)

banner("Commands")
for cmd in ("echo hello", "exit 3", "echo to-stderr >&2; false", "cd /tmp && pwd"):
    r = vm.exec(cmd)
    print(f"{cmd!r:32} rc={r['rc']} out={r['stdout'].strip()!r} err={r['stderr'].strip()!r}")
with timed("a 100 s command through the endpoint"):
    try:
        r = vm.exec("sleep 100; echo done", timeout=150)
        print("  ->", r["rc"], r["stdout"].strip())
    except Exception as e:
        print("  ->", err(e))

banner("Streaming (SSE): do lines arrive as they're printed?")
start = time.perf_counter()
for kind, val in vm.stream("for i in 1 2 3 4 5; do echo line $i; sleep 1; done"):
    print(f"  +{time.perf_counter() - start:4.1f}s {kind} {val}")

banner("Files and the endpoint's bandwidth")
for mb in (1, 20, 100):
    data = os.urandom(mb * 1024 * 1024)
    with timed(f"PUT {mb} MiB", res := {}):
        vm.put_file(f"/tmp/blob{mb}", data)
    up = res[f"PUT {mb} MiB"]
    with timed(f"GET {mb} MiB", res):
        back = vm.get_file(f"/tmp/blob{mb}")
    down = res[f"GET {mb} MiB"]
    same = hashlib.sha256(back).hexdigest() == hashlib.sha256(data).hexdigest()
    print(f"  {mb} MiB: up {mb / up:.2f} MiB/s, down {mb / down:.2f} MiB/s, sha256 match {same}")

banner("HTTP/2")
out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_version}", "--http2",
                      "-H", f"X-aws-proxy-auth: {t8080}", f"{url}/health"], capture_output=True, text=True).stdout
print("negotiated HTTP version (client to endpoint):", out)

banner("WebSocket echo on port 8081 (auth and port as subprotocols)")


async def ws_echo():
    tok = vm.token(8081)
    protos = ["lambda-microvms", f"lambda-microvms.authentication.{tok}", "lambda-microvms.port.8081"]
    async with websockets.connect(f"wss://{vm.endpoint}/", subprotocols=protos, open_timeout=20) as ws:
        for msg in ("ping", "hello from my laptop"):
            t = time.perf_counter()
            await ws.send(msg)
            print(f"  {await ws.recv()!r} ({(time.perf_counter() - t) * 1000:.0f} ms)")

try:
    asyncio.run(ws_echo())
except Exception as e:
    print("  websocket:", err(e))

banner("Shell (SHELL_INGRESS)")
st = mvm.create_microvm_shell_auth_token(microvmIdentifier=vm.id, expirationInMinutes=10)
st = {k: v for k, v in st.items() if k != "ResponseMetadata"}
print("token response keys:", list(st), list(st.get("authToken", {})) if isinstance(st.get("authToken"), dict) else "")
shell_tok = st["authToken"]["X-aws-proxy-auth"] if isinstance(st.get("authToken"), dict) else st.get("authToken")


async def shell(commands: str, seconds: float = 6):
    out = []
    async with websockets.connect(f"wss://{vm.endpoint}/shell", additional_headers={"X-aws-proxy-auth": shell_tok},
                                  open_timeout=20, max_size=None) as ws:
        first = await asyncio.wait_for(ws.recv(), 10)
        print("  first frame:", first[:120] if isinstance(first, str) else f"{len(first)} bytes")
        await ws.send(json.dumps({"type": "resize", "rows": 40, "cols": 160}))
        await ws.send(commands.encode())
        end = time.time() + seconds
        try:
            while time.time() < end:
                try:
                    m = await asyncio.wait_for(ws.recv(), max(0.1, end - time.time()))
                    out.append(m.decode(errors="replace") if isinstance(m, bytes) else m)
                except asyncio.TimeoutError:
                    break
        except websockets.ConnectionClosed as e:
            out.append(f"\n[closed: {e}]")
    return "".join(out)

try:
    text = asyncio.run(shell("id; hostname; . /etc/os-release; echo $PRETTY_NAME; "
                             "command -v ctr || echo 'no ctr: this is the app container'; ps -eo pid,user,comm | head -8; exit\n"))
    print(redact(text)[-2500:])
except Exception as e:
    print("  shell:", err(e))

vm.terminate()

banner("NO_INGRESS")
quiet = run("sandbox", "03-noingress", ingress=(NO_INGRESS,))
quiet.wait("RUNNING")
print("describe ingress:", quiet.describe().get("ingressNetworkConnectors"))
try:
    tok = quiet.token(8080)
    r = h.get(f"https://{quiet.endpoint}/health", headers={"X-aws-proxy-auth": tok})
    print(f"HTTP through the endpoint with NO_INGRESS: {r.status_code} {r.text[:40]!r} (server: {r.headers.get('server')})")
except Exception as e:
    print("token or request:", err(e))
try:
    print("shell token:", list(mvm.create_microvm_shell_auth_token(microvmIdentifier=quiet.id, expirationInMinutes=5)))
except Exception as e:
    print("shell token:", err(e))
quiet.terminate()
