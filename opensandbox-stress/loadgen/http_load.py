"""HTTP load against the echo server, run inside a sandbox by 05_egress_ladder.py (Python 3, standard
library only: the python:3.12-slim image has no curl).

Every request is a new connection, the way a curl per request is: an A lookup through the
sandbox's resolver (the egress sidecar's DNS proxy when there is a policy), a TCP connect (through
nftables, and through the transparent proxy when the credential proxy is on), one GET, and the
close. Each request is timed in full, the lookup also on its own.

--resolve-once looks the name up once at the start (timed, and logged) and then opens every
connection to that cached address: a connect, the GET and the close, with no lookup. That isolates
the connection path (nftables, the transparent proxy) from the resolver. If the address stops
being reachable partway (the egress sidecar's nftables may let an allowed address expire), those
requests fail: they are counted with their kind and the seconds since the lookup, never timed.
--max-consecutive-failures ends the warm-up or the one-at-a-time phase early after that many failures
in a row (the load phase is bounded by --duration).

  python3 http_load.py --host echo.opensandbox.svc.cluster.local \
      --warmup 50 --sequential 500 --concurrency 16 --duration 20 [--expect-header X-Stress-Key]

The load phase spreads its N clients over --processes processes (threads within each), so the
generator is not one Python process holding one core: a GIL-bound client would cap the requests per
second of the fast rungs and compress the differences. The caller still checks the sandbox's CPU and
flags a client-bound run.

Prints one JSON line: the sequential latencies (one request at a time), the load phase (N workers
for D seconds: requests per second and latencies), status counts, and with --expect-header whether
the echo server saw that header on the first request (the vault rung: the sidecar injects it; its
value is never printed). Failed requests (no answer, or not 200) are counted, never timed, with
why (timeout, refused, reset, http <status>, ...) and when: seconds since the lookup with
--resolve-once, since the start otherwise; last_ok_s is when the last request succeeded, on the same
clock. No address is printed.
"""

import argparse
import errno
import json
import multiprocessing
import socket
import threading
import time

MAX_SAMPLES = 60000
MAX_FAILURE_TIMES = 2000


def request_bytes(host: str, path: str) -> bytes:
    return (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: osb-stress-ladder\r\nAccept: */*\r\n"
            "Connection: close\r\n\r\n").encode()


def one(host: str, port: int, payload: bytes, timeout: float, address=None) -> tuple[float, float, int, bytes, str]:
    """(total ms, lookup ms, status, body, error) for one request on a new connection. status 0 or
    not 200: failed, and error says why. With `address` (--resolve-once) there is no lookup (0 ms)."""
    started = time.perf_counter()
    try:
        if address is None:
            address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        looked_up = time.perf_counter()
        with socket.create_connection(address, timeout=timeout) as conn:
            conn.sendall(payload)
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)
        ended = time.perf_counter()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
        error = "" if status == 200 else (f"http {status}" if status else "no HTTP answer")
        return 1000 * (ended - started), 1000 * (looked_up - started), status, body, error
    except socket.gaierror:
        error = "lookup"
    except socket.timeout:
        error = "timeout"
    except ConnectionRefusedError:
        error = "refused"
    except ConnectionResetError:
        error = "reset"
    except OSError as caught:
        error = errno.errorcode.get(caught.errno or 0, type(caught).__name__)    # EPIPE, EHOSTUNREACH, ...
    except (ValueError, IndexError) as caught:
        error = type(caught).__name__
    return 1000 * (time.perf_counter() - started), 0.0, 0, b"", error


def new_failures() -> dict:
    """Failed requests: how many, of which kind, when; and when the last request succeeded (last_ok_s),
    all in seconds since the same moment (the lookup with --resolve-once)."""
    return {"count": 0, "kinds": {}, "times_s": [], "last_ok_s": None}


def note_ok(failures: dict, since: float) -> None:
    failures["last_ok_s"] = round(time.time() - since, 3)


def note_failure(failures: dict, error: str, since: float) -> None:
    """Count a failed request: its kind, and when (seconds since `since`, a time.time())."""
    failures["count"] += 1
    failures["kinds"][error] = failures["kinds"].get(error, 0) + 1
    if len(failures["times_s"]) < MAX_FAILURE_TIMES:
        failures["times_s"].append(round(time.time() - since, 3))


def merge_failures(parts: list[dict]) -> dict:
    out = new_failures()
    for part in parts:
        out["count"] += part["count"]
        for kind, n in part["kinds"].items():
            out["kinds"][kind] = out["kinds"].get(kind, 0) + n
        out["times_s"] += part["times_s"]
        if part.get("last_ok_s") is not None:
            out["last_ok_s"] = max(out["last_ok_s"] or 0.0, part["last_ok_s"])
    out["times_s"] = sorted(out["times_s"])[:MAX_FAILURE_TIMES]
    return out


def load_process(job: tuple) -> dict:
    """One process of the load phase: `threads` clients until the deadline (time.time())."""
    host, port, payload, timeout, threads, deadline, address, since = job
    ms: list[float] = []
    errors = [0]
    statuses: dict[str, int] = {}
    failures = new_failures()
    lock = threading.Lock()

    def worker() -> None:
        mine, failed, seen, why = [], 0, {}, new_failures()
        while time.time() < deadline:
            total, _, status, _, error = one(host, port, payload, timeout, address)
            if status == 200:
                mine.append(round(total, 3))
                note_ok(why, since)
            else:
                failed += 1
                note_failure(why, error, since)
            seen[str(status)] = seen.get(str(status), 0) + 1
        with lock:
            ms.extend(mine)
            errors[0] += failed
            for code, count in seen.items():
                statuses[code] = statuses.get(code, 0) + count
            merged = merge_failures([failures, why])
            failures.update(merged)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    return {"ms": ms, "errors": errors[0], "statuses": statuses, "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--path", default="/ladder")
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--sequential", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--expect-header", default="")
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--resolve-once", action="store_true",
                        help="look the host up once at the start, then connect to that address every time")
    parser.add_argument("--max-consecutive-failures", type=int, default=0,
                        help="end the warm-up or the one-at-a-time phase after this many failures in a row (0: never)")
    args = parser.parse_args()
    payload = request_bytes(args.host, args.path)
    statuses: dict[str, int] = {}
    failures = new_failures()
    stopped: list[str] = []

    def count(status: int) -> None:
        statuses[str(status)] = statuses.get(str(status), 0) + 1

    since = time.time()
    address = None
    resolve_once_ms = None
    if args.resolve_once:
        started = time.perf_counter()
        address = socket.getaddrinfo(args.host, args.port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        since = time.time()
        resolve_once_ms = round(1000 * (time.perf_counter() - started), 3)

    def attempt(phase: str, n: int, keep: list | None) -> None:
        """n requests one at a time; failures counted (and noted), successes timed into keep."""
        in_a_row = 0
        for _ in range(n):
            total, lookup, status, _, error = one(args.host, args.port, payload, args.timeout, address)
            count(status)
            if status == 200:
                in_a_row = 0
                note_ok(failures, since)
                if keep is not None:
                    keep.append((round(total, 3), round(lookup, 3)))
                continue
            note_failure(failures, error, since)
            in_a_row += 1
            if args.max_consecutive_failures and in_a_row >= args.max_consecutive_failures:
                stopped.append(f"{phase} after {in_a_row} failures in a row")
                return

    first = one(args.host, args.port, payload, args.timeout, address)
    count(first[2])
    if first[2] == 200:
        note_ok(failures, since)
    else:
        note_failure(failures, first[4], since)
    header_seen = None
    if args.expect_header:
        try:
            seen = json.loads(first[3].decode() or "{}").get("headers", {})
            header_seen = any(k.lower() == args.expect_header.lower() and v for k, v in seen.items())
        except ValueError:
            header_seen = False
    attempt("warm-up", args.warmup, None)
    timed: list[tuple[float, float]] = []
    before = failures["count"]
    attempt("one at a time", args.sequential, timed)
    seq_errors = failures["count"] - before
    seq_ms = [t for t, _ in timed]
    seq_dns = [] if args.resolve_once else [d for _, d in timed]

    procs = max(1, min(args.processes, args.concurrency))
    shares = [args.concurrency // procs + (1 if i < args.concurrency % procs else 0) for i in range(procs)]
    started = time.perf_counter()
    load_start_unix = time.time()
    deadline = load_start_unix + args.duration
    with multiprocessing.Pool(processes=procs) as pool:
        parts = pool.map(load_process, [(args.host, args.port, payload, args.timeout, n, deadline, address, since)
                                        for n in shares])
    wall = time.perf_counter() - started
    load_end_unix = time.time()
    load_ms = [m for part in parts for m in part["ms"]]
    load_errors = [sum(part["errors"] for part in parts)]
    for part in parts:
        for code, count in part["statuses"].items():
            statuses[code] = statuses.get(code, 0) + count
    failures = merge_failures([failures] + [part["failures"] for part in parts])
    failures["since"] = "the lookup" if args.resolve_once else "the start"
    failures["first_s"] = failures["times_s"][0] if failures["times_s"] else None

    print(json.dumps({
        "host": args.host, "header_seen": header_seen, "statuses": statuses, "resolve_once": args.resolve_once,
        "resolve_once_ms": resolve_once_ms, "timeout_s": args.timeout,
        "failures": failures, "stopped_early": stopped,
        "sequential": {"n": len(seq_ms), "errors": seq_errors, "ms": seq_ms[:MAX_SAMPLES], "dns_ms": seq_dns[:MAX_SAMPLES]},
        "load": {"concurrency": args.concurrency, "processes": procs, "duration_s": round(wall, 3), "n": len(load_ms),
                 "start_unix": round(load_start_unix, 3), "end_unix": round(load_end_unix, 3),
                 "errors": load_errors[0], "rps": round(len(load_ms) / wall, 1) if wall else 0,
                 "ms": load_ms[:MAX_SAMPLES]},
    }), flush=True)


if __name__ == "__main__":
    main()
