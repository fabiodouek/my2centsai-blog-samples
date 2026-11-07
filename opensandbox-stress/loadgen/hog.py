"""A noisy neighbor for 07_noisy.py, run inside a sandbox in the background (standard library only).

  python3 hog.py cpu --workers 2 --seconds 900     busy loops in separate processes (not threads:
                                                   one Python process holds one core at most)
  python3 hog.py disk --mb 64 --files 8 --seconds 900
                                                   writes MiB-sized blocks to the root filesystem
                                                   and fsyncs each file, round and round over
                                                   --files files (so the disk never fills)

It stops by itself after --seconds (a safety net: 07_noisy.py deletes the hog's sandbox anyway).
"""

import argparse
import multiprocessing
import os
import time


def spin(until: float) -> None:
    x = 0
    while time.time() < until:
        for _ in range(100000):
            x += 1


def disk(until: float, megabytes: int, files: int, folder: str) -> None:
    block = os.urandom(1 << 20)
    i = 0
    while time.time() < until:
        path = os.path.join(folder, f"hog-{i % files}.bin")
        with open(path, "wb") as out:
            for _ in range(megabytes):
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        i += 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("cpu", "disk"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--mb", type=int, default=64)
    parser.add_argument("--files", type=int, default=8)
    parser.add_argument("--dir", default="/root")
    parser.add_argument("--seconds", type=float, default=900)
    args = parser.parse_args()
    until = time.time() + args.seconds
    if args.kind == "cpu":
        procs = [multiprocessing.Process(target=spin, args=(until,)) for _ in range(args.workers)]
    else:
        procs = [multiprocessing.Process(target=disk, args=(until, args.mb, args.files, args.dir))]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()


if __name__ == "__main__":
    main()
