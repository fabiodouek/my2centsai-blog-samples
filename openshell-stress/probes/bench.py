"""HTTP benchmark that runs inside a sandbox (or the plain baseline Pod) for 04_egress_ladder.py.
Standard library only. One JSON argument, one JSON line out:

  fresh       N requests, each on a new TCP (or TLS) connection: DNS + connect + request
  keepalive   N requests on one connection
  throughput  C threads, each sending requests on new connections for D seconds
  upload      K POSTs of a 1 MiB random body, each on a new connection

With "auth_env", every request carries Authorization: Bearer $<auth_env> (in a sandbox the
variable holds a credential placeholder that the supervisor replaces). The echo server sends
back the headers it received; only a SHA-256 prefix of that header leaves this script.
"""
import hashlib
import http.client
import json
import os
import ssl
import sys
import threading
import time

cfg = json.loads(sys.argv[1])
HOST, PORT, SCHEME = cfg["host"], int(cfg["port"]), cfg.get("scheme", "http")
PATH = cfg.get("path", "/bench/get")
TIMEOUT = float(cfg.get("timeout", 10))
HEADERS = {}
if cfg.get("auth_env"):
    HEADERS["Authorization"] = "Bearer " + os.environ.get(cfg["auth_env"], "")
CTX = ssl.create_default_context() if SCHEME == "https" else None  # honours SSL_CERT_FILE


def connect():
    if SCHEME == "https":
        return http.client.HTTPSConnection(HOST, PORT, timeout=TIMEOUT, context=CTX)
    return http.client.HTTPConnection(HOST, PORT, timeout=TIMEOUT)


def request(conn, method="GET", path=PATH, body=None):
    hdrs = dict(HEADERS)
    if body is not None:
        hdrs["Content-Type"] = "application/octet-stream"
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, data


errors = {}


def err(e):
    k = type(e).__name__
    errors[k] = errors.get(k, 0) + 1


def fresh(n):
    out, codes = [], {}
    for _ in range(n):
        t = time.perf_counter()
        try:
            c = connect()
            status, _ = request(c)
            c.close()
            out.append((time.perf_counter() - t) * 1000)
            codes[status] = codes.get(status, 0) + 1
        except Exception as e:
            err(e)
    return out, codes


def keepalive(n):
    out, codes = [], {}
    c = connect()
    try:
        request(c)  # warm-up: connect outside the timed loop
    except Exception as e:
        err(e)
        return out, codes
    for _ in range(n):
        t = time.perf_counter()
        try:
            status, _ = request(c)
            out.append((time.perf_counter() - t) * 1000)
            codes[status] = codes.get(status, 0) + 1
        except Exception as e:
            err(e)
            c = connect()
    c.close()
    return out, codes


def throughput(threads, seconds):
    done, fail = [0] * threads, [0] * threads
    stop = time.perf_counter() + seconds

    def worker(i):
        while time.perf_counter() < stop:
            try:
                c = connect()
                request(c)
                c.close()
                done[i] += 1
            except Exception as e:
                fail[i] += 1
                err(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    took = time.perf_counter() - t0
    return {"threads": threads, "seconds": round(took, 2), "requests": sum(done), "failed": sum(fail),
            "rps": round(sum(done) / took, 1)}


def upload(k, size=1 << 20):
    body = os.urandom(size)
    out, codes, sent = [], {}, 0
    for _ in range(k):
        t = time.perf_counter()
        try:
            c = connect()
            status, _ = request(c, "POST", "/bench/upload", body)
            c.close()
            out.append((time.perf_counter() - t) * 1000)
            codes[status] = codes.get(status, 0) + 1
            sent += size if status < 400 else 0
        except Exception as e:
            err(e)
    secs = sum(out) / 1000
    return out, codes, round(sent / 2 ** 20 / secs, 1) if secs else 0.0


def auth_check():
    """sha256 prefix of the Authorization header the echo server received."""
    try:
        c = connect()
        status, data = request(c)
        c.close()
        got = json.loads(data).get("headers", {})
        value = next((v for k, v in got.items() if k.lower() == "authorization"), "")
        return {"status": status, "sha256_12": hashlib.sha256(value.encode()).hexdigest()[:12] if value else "",
                "length": len(value)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}


result = {"rung": cfg.get("rung", ""), "host": HOST, "port": PORT, "scheme": SCHEME}
if cfg.get("auth_env"):
    result["auth"] = auth_check()
if cfg.get("fresh"):
    lat, codes = fresh(int(cfg["fresh"]))
    result["fresh"] = {"lat_ms": [round(x, 3) for x in lat], "codes": codes}
if cfg.get("keepalive"):
    lat, codes = keepalive(int(cfg["keepalive"]))
    result["keepalive"] = {"lat_ms": [round(x, 3) for x in lat], "codes": codes}
if cfg.get("throughput_s"):
    result["throughput"] = throughput(int(cfg.get("threads", 8)), float(cfg["throughput_s"]))
if cfg.get("upload"):
    lat, codes, mbps = upload(int(cfg["upload"]))
    result["upload"] = {"lat_ms": [round(x, 3) for x in lat], "codes": codes, "mib_per_s": mbps}
result["errors"] = errors
print(json.dumps(result))
