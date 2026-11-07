"""The API ramp for 06_control_plane.py. Runs in a Job pod next to the server (manifests/api-load.yaml),
not on the laptop: through a kubectl port-forward the forward itself would be what is measured.

Standard library only. For each concurrency level it runs that many clients for --duration seconds,
each on its own keep-alive connection, calling the lifecycle server's API in turn:
  GET /v1/sandboxes?pageSize=20     the list a dashboard or a reaper polls
  GET /v1/sandboxes/<id>            the status poll an SDK makes while it waits (ids from --ids)
Clients are threads spread over up to 8 processes (one Python process drives one core at most).

Only answered calls (200) are timed; every other outcome (a refused or reset connection is status
0) is counted in "statuses" and "failed". The API key comes from the environment (OSB_API_KEY, from
the Secret opensandbox-server-auth in the Job's spec) and is never printed. Prints one JSON line per
level, then {"done": true}.

  python3 api_load.py --levels 1,4,16,64 --duration 20 --ids <id>,<id>,...
"""

import argparse
import http.client
import json
import multiprocessing
import os
import threading
import time
from urllib.parse import urlsplit

KEY_HEADER = "OPEN-SANDBOX-API-KEY"


def client(url: str, key: str, paths: list[str], until: float) -> dict:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=15)
    ms, statuses, i = [], {}, 0
    while time.time() < until:
        path = paths[i % len(paths)]
        i += 1
        started = time.perf_counter()
        try:
            conn.request("GET", path, headers={KEY_HEADER: key})
            reply = conn.getresponse()
            reply.read()
            status = reply.status
        except (OSError, http.client.HTTPException):
            status = 0
            conn.close()
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=15)
        if status == 200:       # a refused, reset or non-200 call is counted, never timed
            ms.append(round(1000 * (time.perf_counter() - started), 3))
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    conn.close()
    return {"ms": ms, "statuses": statuses}


def process(args: tuple) -> dict:
    """One process: `threads` clients side by side; their results merged."""
    url, key, paths, until, threads = args
    results: list[dict] = []
    lock = threading.Lock()

    def run() -> None:
        result = client(url, key, paths, until)
        with lock:
            results.append(result)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    merged: dict = {"ms": [], "statuses": {}}
    for result in results:
        merged["ms"] += result["ms"]
        for code, n in result["statuses"].items():
            merged["statuses"][code] = merged["statuses"].get(code, 0) + n
    return merged


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("OSB_URL", "http://opensandbox-server.opensandbox-system.svc.cluster.local"))
    parser.add_argument("--levels", default="1,4,16,64")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--pause", type=float, default=5.0, help="seconds of quiet between levels")
    parser.add_argument("--ids", default="")
    args = parser.parse_args()
    key = os.environ.get("OSB_API_KEY", "")
    if not key:
        raise SystemExit("OSB_API_KEY is not set")
    paths = ["/v1/sandboxes?pageSize=20"] + [f"/v1/sandboxes/{i}" for i in args.ids.split(",") if i]
    for level in [int(x) for x in args.levels.split(",") if x]:
        start = time.time()
        until = start + args.duration
        procs = min(level, 8)
        shares = [level // procs + (1 if i < level % procs else 0) for i in range(procs)]
        with multiprocessing.Pool(processes=procs) as pool:
            parts = pool.map(process, [(args.url, key, paths, until, n) for n in shares])
        end = time.time()
        ms = [m for p in parts for m in p["ms"]]
        statuses: dict[str, int] = {}
        for p in parts:
            for code, n in p["statuses"].items():
                statuses[code] = statuses.get(code, 0) + n
        ok = statuses.get("200", 0)
        print(json.dumps({"level": level, "start": round(start, 3), "end": round(end, 3),
                          "n": len(ms), "ok": ok, "failed": sum(statuses.values()) - ok,
                          "rps": round(ok / (end - start), 1), "statuses": statuses,
                          "ms": ms[:60000]}), flush=True)
        time.sleep(args.pause)
    print(json.dumps({"done": True}), flush=True)


if __name__ == "__main__":
    main()
