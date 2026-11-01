"""Victim probe for 06_noisy_neighbors.py. Runs in the background in one sandbox and, every
`interval` seconds, appends a JSON line (sandbox clock) to argv[2] with:

  cpu_ms        time for a fixed amount of pure-Python work (about 20 ms on an idle m7i core)
  fsync_ws_ms   write 4 KiB to /sandbox (this sandbox's own EBS volume) and fsync it
  fsync_tmp_ms  the same in /tmp (the container's writable layer, on the node's root volume)
  net_ms        one GET to the echo server on a new connection, through the supervisor
"""
import http.client
import json
import os
import sys
import time

cfg = json.loads(sys.argv[1])
OUT = sys.argv[2]
INTERVAL, DURATION = float(cfg.get("interval", 1)), float(cfg.get("duration", 600))
HOST, PORT = cfg.get("host", "echo.echo.svc.cluster.local"), int(cfg.get("port", 80))
WORK = int(cfg.get("work", 300_000))
BLOCK = os.urandom(4096)


def cpu():
    t = time.perf_counter()
    s = 0
    for i in range(WORK):
        s += i * i
    return (time.perf_counter() - t) * 1000


def fsync(path):
    t = time.perf_counter()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, BLOCK)
        os.fsync(fd)
    finally:
        os.close(fd)
    return (time.perf_counter() - t) * 1000


def net():
    t = time.perf_counter()
    c = http.client.HTTPConnection(HOST, PORT, timeout=5)
    c.request("GET", "/p6/noisy")
    r = c.getresponse()
    r.read()
    c.close()
    return (time.perf_counter() - t) * 1000 if r.status == 200 else None


end = time.time() + DURATION
with open(OUT, "a") as f:
    while time.time() < end:
        tick = time.time()
        row = {"t": tick}
        for key, fn in (("cpu_ms", cpu), ("fsync_ws_ms", lambda: fsync("/sandbox/.p6-probe")),
                        ("fsync_tmp_ms", lambda: fsync("/tmp/.p6-probe")), ("net_ms", net)):
            try:
                v = fn()
                row[key] = round(v, 3) if v is not None else None
            except Exception as e:
                row[key] = None
                row[key + "_err"] = type(e).__name__
        f.write(json.dumps(row) + "\n")
        f.flush()
        time.sleep(max(0.0, INTERVAL - (time.time() - tick)))
