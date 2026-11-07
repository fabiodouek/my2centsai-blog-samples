"""The victim's benchmarks for 07_noisy.py, run inside a sandbox (Python 3, standard library only).

Each iteration times three things:
  cpu_ms    a fixed amount of work: sha256 over a 1 MiB buffer, --cpu-rounds times
  io_ms     write --io-mb MiB to the root filesystem and fsync it (the file is then removed)
  fsync_ms  --fsyncs small appends (4 KiB), each followed by fsync: a latency, one per append

  python3 victim.py --iterations 5

Prints one JSON line {cpu_ms: [...], io_ms: [...], fsync_ms: [...]}.
"""

import argparse
import hashlib
import json
import os
import time


def cpu(rounds: int, buffer: bytes) -> float:
    started = time.perf_counter()
    digest = hashlib.sha256()
    for _ in range(rounds):
        digest.update(buffer)
    digest.hexdigest()
    return 1000 * (time.perf_counter() - started)


def io(path: str, megabytes: int, block: bytes) -> float:
    started = time.perf_counter()
    with open(path, "wb") as out:
        for _ in range(megabytes):
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    elapsed = 1000 * (time.perf_counter() - started)
    os.remove(path)
    return elapsed


def fsyncs(path: str, count: int) -> list[float]:
    small = os.urandom(4096)
    times = []
    with open(path, "ab") as out:
        for _ in range(count):
            started = time.perf_counter()
            out.write(small)
            out.flush()
            os.fsync(out.fileno())
            times.append(1000 * (time.perf_counter() - started))
    os.remove(path)
    return times


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--cpu-rounds", type=int, default=300)
    parser.add_argument("--io-mb", type=int, default=64)
    parser.add_argument("--fsyncs", type=int, default=100)
    parser.add_argument("--dir", default="/root")
    args = parser.parse_args()
    buffer = os.urandom(1 << 20)
    result = {"cpu_ms": [], "io_ms": [], "fsync_ms": []}
    for i in range(args.iterations):
        result["cpu_ms"].append(round(cpu(args.cpu_rounds, buffer), 2))
        result["io_ms"].append(round(io(os.path.join(args.dir, f"victim-{i}.bin"), args.io_mb, buffer), 2))
        result["fsync_ms"] += [round(t, 3) for t in fsyncs(os.path.join(args.dir, f"victim-{i}.log"), args.fsyncs)]
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
