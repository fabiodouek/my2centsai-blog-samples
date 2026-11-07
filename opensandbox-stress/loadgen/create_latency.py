"""One round of sandbox creates, run inside the cluster by 03_latency.py (manifests/latency-client.yaml).

N creates start at once (a barrier), each through the SDK against the lifecycle server's Service,
with the server proxy on. A create is timed from the call until SandboxSync.create() returns, which
is when the sandbox answered its health check. Then every sandbox made is killed (--keep leaves
them for the caller). Prints one JSON line:

  {"n", "ok", "ms": [one per successful create], "failed": count, "errors": {type: count},
   "error_samples": [...], "wall_ms", "killed", "kill_failed", "client_cpu_m", "client_cpu_peak_m"}

client_cpu_m is this process's own CPU over the round (process time / wall time) and
client_cpu_peak_m its busiest second: near 1000m, the one Python process was the limit, not the server.

Failed creates are never in "ms": they are counted. The API key comes from OSB_API_KEY (the
server's Secret) and is never printed.

  python3 create_latency.py --n 40 --run <id> --image <ref> [--pool osb-stress-pool] [--cpu 250m --memory 512Mi]
"""

import argparse
import json
import os
import threading
import time
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.sync import SandboxManagerSync


def connection() -> ConnectionConfigSync:
    """A new config per sandbox (closing a sandbox closes the transport its config owns)."""
    return ConnectionConfigSync(domain=os.environ.get("OSB_SERVER_DOMAIN", "opensandbox-server.opensandbox-system.svc:80"),
                                api_key=os.environ["OSB_API_KEY"], use_server_proxy=True,
                                request_timeout=timedelta(seconds=120))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--step", default="cold")
    parser.add_argument("--image", required=True)
    parser.add_argument("--pool", default="")
    parser.add_argument("--cpu", default="250m")
    parser.add_argument("--memory", default="512Mi")
    parser.add_argument("--keep", action="store_true",
                        help="leave the sandboxes running (the caller deletes several rounds' at once)")
    args = parser.parse_args()
    meta = {"example": "opensandbox-stress", "script": "03_latency", "run": args.run, "step": args.step}
    extensions = {"poolRef": args.pool} if args.pool else None
    barrier = threading.Barrier(args.n)
    lock = threading.Lock()
    ms: list[float] = []
    ids: list[str] = []
    errors: dict[str, int] = {}
    samples: list[str] = []
    marks: list[float] = []

    def one() -> None:
        try:
            barrier.wait(timeout=60)
        except threading.BrokenBarrierError:
            pass
        started = time.perf_counter()
        with lock:
            marks.append(started)
        try:
            sb = SandboxSync.create(args.image, metadata=meta, resource={"cpu": args.cpu, "memory": args.memory},
                                    extensions=extensions, timeout=timedelta(minutes=20),
                                    ready_timeout=timedelta(seconds=180), connection_config=connection())
            took = 1000 * (time.perf_counter() - started)
            with lock:
                ms.append(round(took, 1))
                ids.append(sb.id)
                marks.append(time.perf_counter())
            sb.close()
        except Exception as error:  # noqa: BLE001 - counted, never a sample
            name = type(error).__name__
            with lock:
                errors[name] = errors.get(name, 0) + 1
                if len(samples) < 3:
                    samples.append(f"{name}: {str(error)[:160]}")

    # This process's own CPU: it is one Python process, so near 1000m (one core) the client, not the
    # server, is what limits the round, and the caller flags it. The round average dilutes a busy
    # start, so the busiest second is kept as well (sampled every second by a thread).
    cpu_before, wall_before = time.process_time(), time.perf_counter()
    peak = [0.0]
    stop = threading.Event()

    def watch_cpu() -> None:
        last_cpu, last_wall = time.process_time(), time.perf_counter()
        while not stop.wait(1.0):
            cpu_now, wall_now = time.process_time(), time.perf_counter()
            if wall_now > last_wall:
                peak[0] = max(peak[0], 1000 * (cpu_now - last_cpu) / (wall_now - last_wall))
            last_cpu, last_wall = cpu_now, wall_now

    watcher = threading.Thread(target=watch_cpu, daemon=True)
    watcher.start()
    threads = [threading.Thread(target=one) for _ in range(args.n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()
    watcher.join(timeout=2)
    cpu_used, wall_used = time.process_time() - cpu_before, time.perf_counter() - wall_before
    client_cpu_m = round(1000 * cpu_used / wall_used, 1) if wall_used > 0 else None
    client_cpu_peak_m = round(peak[0], 1) if wall_used >= 1.0 else client_cpu_m
    wall = round(1000 * (max(marks) - min(marks)), 1) if marks else None

    killed, kill_failed = 0, 0
    with SandboxManagerSync.create(connection_config=connection()) as manager:
        for sandbox_id in ([] if args.keep else ids):
            try:
                manager.kill_sandbox(sandbox_id)
                killed += 1
            except Exception:  # noqa: BLE001 - the laptop's delete_run sweeps the rest
                kill_failed += 1
    print(json.dumps({"n": args.n, "ok": len(ms), "ms": ms, "failed": sum(errors.values()), "errors": errors,
                      "error_samples": samples, "wall_ms": wall, "killed": killed, "kill_failed": kill_failed,
                      "client_cpu_m": client_cpu_m, "client_cpu_peak_m": client_cpu_peak_m}),
          flush=True)


if __name__ == "__main__":
    main()
