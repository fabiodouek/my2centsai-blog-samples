"""In-VM agent for the Lambda MicroVM lifecycle tests.

Lambda MicroVMs have no exec or file API: you get an HTTPS endpoint that forwards to
ports inside the VM, and that's it. This agent is the smallest thing that turns a
MicroVM into a usable sandbox for the tests:

  :8080  app      GET /health, POST /exec, POST /exec/stream (SSE), PUT/GET /files,
                  GET /state, GET /events, GET /headers, POST /tcp/open, GET /tcp/check
  :8081  ws       WebSocket echo (to test the endpoint's WebSocket support)
  :9001  hooks    POST /aws/lambda-microvms/runtime/v1/{ready,validate,run,resume,suspend,terminate}

Stdlib only, apart from `websockets` for the echo server. Anyone holding an endpoint token
for port 8080 can run any command as root: that's the point of a sandbox, so guard the tokens.
"""

import asyncio
import http.client
import json
import os
import random
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import websockets

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"

# Values created at import time run during the image build, so they are captured in the
# snapshot and every MicroVM started from the image sees the same ones.
BUILD_TIME = time.time()
BUILD_UUID = str(uuid.uuid4())
BUILD_PID = os.getpid()

STATE = {"events": [], "run_payload": None, "run_uuid": None, "counter": 0}
TCP = {"conn": None, "host": None, "opened_at": None}


def log(msg, **kw):
    # stdout goes to CloudWatch when the MicroVM has an execution role with logs permissions
    print(json.dumps({"t": round(time.time(), 3), "msg": msg, **kw}), flush=True)


def counter():
    while True:
        STATE["counter"] += 1
        time.sleep(1)


class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def log_message(self, fmt, *args):
        pass


class Hooks(Base):
    def do_POST(self):
        raw = self.body()
        if not self.path.startswith(HOOK_PREFIX):
            return self.send_json(404, {"error": "not a hook"})
        hook = self.path[len(HOOK_PREFIX):]
        # The peer address tells Lambda's own hook calls apart from ones forwarded through the endpoint
        event = {"hook": hook, "t": time.time(), "bytes": len(raw), "peer": self.client_address[0]}
        if hook == "run":
            body = json.loads(raw or b"{}")
            payload = body.get("runHookPayload")
            STATE["run_payload"] = payload
            STATE["microvm_id"] = body.get("microvmId")
            # Fresh randomness per MicroVM: the documented fix for snapshot-shared values
            STATE["run_uuid"] = str(uuid.UUID(bytes=os.urandom(16), version=4))
            event["payload_len"] = len(payload or "")
        if hook == "validate" and os.environ.get("VALIDATE_CMD"):
            # Lambda records which parts of the snapshot this request touches and prefetches them
            # at run time, so exercise whatever a real session will need first.
            t0 = time.time()
            subprocess.run(["/bin/bash", "-c", os.environ["VALIDATE_CMD"]], capture_output=True, timeout=240)
            event["validate_cmd_s"] = round(time.time() - t0, 2)
        STATE["events"].append(event)
        log("hook", **event)
        return self.send_json(200, {"hook": hook})


class App(Base):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send_json(200, {"ok": True})
        if u.path == "/events":
            return self.send_json(200, {"events": STATE["events"]})
        if u.path == "/headers":
            return self.send_json(200, {"peer": self.client_address[0], "headers": dict(self.headers)})
        if u.path == "/state":
            return self.send_json(200, {
                "pid": os.getpid(),
                "build_pid": BUILD_PID,
                "build_time": BUILD_TIME,
                "now": time.time(),
                "uptime_s": float(open("/proc/uptime").read().split()[0]),
                "counter": STATE["counter"],
                "microvm_id": STATE.get("microvm_id"),
                "build_uuid": BUILD_UUID,
                "run_uuid": STATE["run_uuid"],
                # Generated per request: Python's `random` is seeded at import (in the snapshot),
                # uuid4 reads the kernel RNG.
                "random_random": random.random(),
                "uuid4_now": str(uuid.uuid4()),
                "boot_id": open("/proc/sys/kernel/random/boot_id").read().strip(),
                "run_payload_len": len(STATE["run_payload"] or ""),
            })
        if u.path == "/files":
            path = parse_qs(u.query)["path"][0]
            try:
                data = open(path, "rb").read()
            except FileNotFoundError:
                return self.send_json(404, {"error": "not found", "path": path})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if u.path == "/tcp/check":
            c = TCP["conn"]
            if c is None:
                return self.send_json(409, {"error": "no connection open"})
            try:
                c.request("HEAD", "/", headers={"Host": TCP["host"]})
                r = c.getresponse()
                r.read()
                return self.send_json(200, {"ok": True, "status": r.status, "age_s": round(time.time() - TCP["opened_at"], 1)})
            except Exception as e:
                return self.send_json(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return self.send_json(404, {"error": "not found"})

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path != "/files":
            return self.send_json(404, {"error": "not found"})
        path = parse_qs(u.query)["path"][0]
        data = self.body()
        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return self.send_json(200, {"path": path, "bytes": len(data)})

    def do_POST(self):
        raw = self.body()
        if self.path == "/exec":
            req = json.loads(raw or b"{}")
            if req.get("detach"):
                p = subprocess.Popen(["/bin/bash", "-c", req["cmd"]], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
                return self.send_json(200, {"pid": p.pid})
            t0 = time.time()
            try:
                p = subprocess.run(["/bin/bash", "-c", req["cmd"]], capture_output=True, text=True,
                                   timeout=req.get("timeout", 120), cwd=req.get("cwd", "/root"))
                out = {"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
            except subprocess.TimeoutExpired as e:
                out = {"rc": None, "stdout": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                       "stderr": "timeout"}
            out["seconds"] = round(time.time() - t0, 3)
            return self.send_json(200, out)
        if self.path == "/exec/stream":
            # Server-sent events: one event per output line, then an `exit` event.
            req = json.loads(raw or b"{}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            p = subprocess.Popen(["/bin/bash", "-c", req["cmd"]], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p.stdout:
                self.wfile.write(f"data: {json.dumps(line.rstrip())}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(f"event: exit\ndata: {p.wait()}\n\n".encode())
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/tcp/open":
            req = json.loads(raw or b"{}")
            host = req.get("host", "example.com")
            c = http.client.HTTPSConnection(host, 443, timeout=10)
            c.request("HEAD", "/", headers={"Host": host})
            r = c.getresponse()
            r.read()
            TCP.update(conn=c, host=host, opened_at=time.time())
            return self.send_json(200, {"ok": True, "status": r.status})
        return self.send_json(404, {"error": "not found"})


async def ws_echo(conn):
    async for msg in conn:
        await conn.send(f"echo: {msg}")


async def ws_main():
    async with websockets.serve(ws_echo, "0.0.0.0", 8081):
        await asyncio.Future()


def serve(port, handler):
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=counter, daemon=True).start()
    threading.Thread(target=serve, args=(9001, Hooks), daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(ws_main()), daemon=True).start()
    log("agent listening", app=8080, ws=8081, hooks=9001, build_uuid=BUILD_UUID)
    serve(8080, App)
