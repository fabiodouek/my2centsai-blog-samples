"""Is a hog running? Run by 07_noisy.py inside a hog's sandbox (Python 3, standard library only).

  python3 hog_probe.py --window 2

Prints one JSON line:
  procs           hog.py's Python processes (its main process and its workers) still alive at the
                  end of the window, from /proc (the image has no ps or pgrep)
  cpu_m           what those processes used over the window, in millicores: utime + stime from
                  /proc/<pid>/stat, read at both ends. This is the sandbox kernel's own count, which
                  inside a Kata VM is the guest's, so it works where the kubelet reports 0 for the pod
  window_s        the window, measured
  newest_write_s  seconds since a disk hog last wrote one of its files (null: no file)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time


def hog_pids(proc: str = "/proc") -> list[int]:
    """Python processes running hog.py; not a shell wrapper whose command line names it. hog.py's
    workers are found because they share its command line: multiprocessing forks them (the default
    start method on Linux up to Python 3.13; the sandbox image is python:3.12). Under "forkserver"
    or "spawn" the workers' command lines differ, every CPU hog would read 1 of 3 processes, and
    the check would fail closed (never pass a hog it cannot see)."""
    pids = []
    for path in glob.glob(os.path.join(proc, "[0-9]*", "cmdline")):
        try:
            with open(path, "rb") as source:
                argv = source.read().split(b"\0")
        except OSError:
            continue
        if argv and b"python" in os.path.basename(argv[0]) and any(a.endswith(b"hog.py") for a in argv[1:]):
            pids.append(int(os.path.basename(os.path.dirname(path))))
    return sorted(pids)


def parse_stat(stat: str) -> tuple[str, int]:
    """(state, utime + stime in clock ticks) of a /proc/<pid>/stat line: fields 3, 14 and 15. Field
    2, the command name in parentheses, may itself hold spaces and parentheses: split after the last ')'."""
    rest = stat[stat.rindex(")") + 1:].split()   # rest[0] is field 3
    return rest[0], int(rest[11]) + int(rest[12])


def read_ticks(pids: list[int], proc: str = "/proc") -> dict[int, int]:
    """{pid: utime + stime} of the processes still alive: one that exited, or a zombie (Z) waiting
    to be reaped, is left out."""
    out = {}
    for pid in pids:
        try:
            with open(os.path.join(proc, str(pid), "stat")) as source:
                state, ticks = parse_stat(source.read())
        except (OSError, ValueError, IndexError):
            continue
        if state not in ("Z", "X", "x"):
            out[pid] = ticks
    return out


def cpu_milli(before: dict[int, int], after: dict[int, int], seconds: float, hertz: int) -> float | None:
    """Millicores used between the two reads by the processes present at both (None: none were)."""
    both = before.keys() & after.keys()
    if not both or seconds <= 0:
        return None
    return round(1000 * sum(after[p] - before[p] for p in both) / hertz / seconds, 1)


def newest_write_s(folder: str, now: float) -> float | None:
    ages = []
    for path in glob.glob(os.path.join(folder, "hog-*.bin")):
        try:
            ages.append(now - os.path.getmtime(path))
        except OSError:
            continue
    return round(max(0.0, min(ages)), 1) if ages else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=float, default=2.0, help="seconds between the two CPU reads")
    parser.add_argument("--dir", default="/root", help="where a disk hog writes (hog.py --dir)")
    parser.add_argument("--proc", default="/proc")
    args = parser.parse_args()
    pids = hog_pids(args.proc)
    started = time.monotonic()
    before = read_ticks(pids, args.proc)
    time.sleep(args.window)
    after = read_ticks(pids, args.proc)
    seconds = time.monotonic() - started
    print(json.dumps({"procs": len(after), "cpu_m": cpu_milli(before, after, seconds, os.sysconf("SC_CLK_TCK")),
                      "window_s": round(seconds, 2), "newest_write_s": newest_write_s(args.dir, time.time())}))


if __name__ == "__main__":
    main()
