"""Policy propagation probe for 03_policy_propagation.py. Runs in the background inside each
sandbox and appends JSON lines (sandbox clock, epoch seconds) to the file in argv[2]:

  {"event": "start"}                    probing begins; the echo server is still denied
  {"event": "allowed", "t": ...}        first 200 from the echo server (policy loaded)
  {"event": "stream_open", "t": ...}    a long GET /slow stream is open under that policy
  {"event": "cut", "t": ..., ...}       the stream ended early (a newer policy generation)
  {"event": "stream_end", "t": ...}     the stream ran to the end (nothing cut it)
  {"event": "timeout", ...}             never allowed within the time limit

The probe tries every 50 ms with a new connection and a 0.5 s timeout. A denied connect fails
at once with EACCES (OpenShell's seccomp broker), so a try costs a few milliseconds.
"""
import http.client
import json
import sys
import time

cfg = json.loads(sys.argv[1])
OUT = sys.argv[2]
HOST, PORT = cfg.get("host", "echo.echo.svc.cluster.local"), int(cfg.get("port", 80))
LIMIT = float(cfg.get("limit_s", 300))
STREAM_S = int(cfg.get("stream_s", 120))
NAME = cfg.get("name", "")


def emit(event, **kw):
    with open(OUT, "a") as f:
        f.write(json.dumps({"event": event, "t": time.time(), "name": NAME, **kw}) + "\n")


emit("start")
start, tries, last_err = time.time(), 0, ""
while True:
    tries += 1
    try:
        c = http.client.HTTPConnection(HOST, PORT, timeout=0.5)
        c.request("GET", f"/p6/flip/{NAME}")
        r = c.getresponse()
        r.read()
        c.close()
        if r.status == 200:
            emit("allowed", tries=tries)
            break
        last_err = f"HTTP {r.status}"
    except Exception as e:
        last_err = type(e).__name__
    if time.time() - start > LIMIT:
        emit("timeout", tries=tries, last_error=last_err)
        sys.exit(0)
    time.sleep(0.05)

# Hold a long-lived response open: the next policy generation should close it.
ticks, err = 0, ""
try:
    c = http.client.HTTPConnection(HOST, PORT, timeout=STREAM_S + 30)
    c.request("GET", f"/slow?seconds={STREAM_S}")
    r = c.getresponse()
    emit("stream_open", status=r.status)
    while True:
        line = r.fp.readline()  # chunk-size line, then "tick N", then CRLF
        if not line:
            break
        if line.startswith(b"tick"):
            ticks += 1
        if line.strip() == b"0":
            emit("stream_end", ticks=ticks)
            sys.exit(0)
except Exception as e:
    err = f"{type(e).__name__}: {e}"[:200]
emit("cut", ticks=ticks, error=err)
