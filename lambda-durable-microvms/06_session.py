"""Session mode: one MicroVM, two jobs, a durable wait in between.

Job 1 (/build) clones pallets/click and pip-installs it, and remembers the commit in the
worker's memory. A durable wait follows. Job 2 (/test) runs click's test suite from the
files job 1 left on disk, and reports what it finds in memory.

Two sessions run side by side:
  suspend=True   suspend_after on job 1: the function suspends the MicroVM when the build
                 result arrives; the next request resumes it (idle policy autoResumeEnabled)
  suspend=False  the MicroVM keeps running, and billing, through the wait

The script samples each MicroVM's state every 5 seconds during the wait and prints the
running seconds of each.

usage: python 06_session.py [wait_seconds]   (default 120)
"""

import sys
import threading
import time

from common import (banner, clock, history, launched_microvms, microvm, microvm_cost, result_of, save, say, start,
                    summarize, ts, wait_done, warm_up)


def follow(label: str, suspend: bool, wait_s: int, out: dict) -> None:
    arn = start("session", name=f"session-{label}-{int(time.time())}", suspend=suspend, wait_seconds=wait_s)
    samples, vm = [], None
    while True:
        d = microvm(vm) if vm else None
        if vm is None:
            ids = launched_microvms(history(arn))
            vm = ids[0] if ids else None
        if d:
            samples.append((round(time.time(), 1), d["state"]))
            if len(samples) == 1 or samples[-1][1] != samples[-2][1]:
                say(f"[{clock()}] {label:<11} MicroVM {d['state']}")
        st = microvm(vm)["state"] if vm else None
        if st == "TERMINATED":
            break
        time.sleep(5)
    d = wait_done(arn)
    ev = history(arn)
    s = summarize(ev)
    m = microvm(vm)
    life = ts(m["terminatedAt"]) - ts(m["startedAt"])
    # Running time = lifetime minus the time sampled as SUSPENDED (5-second resolution)
    suspended = sum(b[0] - a[0] for a, b in zip(samples, samples[1:]) if a[1] == "SUSPENDED")
    out[label] = {"execution": arn, "status": d["Status"], "result": result_of(d), "summary": s,
                  "microvm": vm, "lifetime_seconds": round(life, 1), "suspended_seconds": round(suspended, 1),
                  "running_seconds": round(life - suspended, 1), "samples": samples}


def main(wait_s: int = 120) -> None:
    banner(f"Session mode: build, wait {wait_s}s, test, in one MicroVM")
    warm_up()
    out: dict = {}
    threads = [threading.Thread(target=follow, args=(label, suspend, wait_s, out))
               for label, suspend in (("suspend", True), ("no-suspend", False))]
    for t in threads:
        t.start()
        time.sleep(1)
    for t in threads:
        t.join()
    for label, o in out.items():
        r = o["result"]
        say(f"\n{label}: execution {o['status']} in {o['summary']['total_seconds']:.0f}s, "
            f"{len(o['summary']['invocations'])} invocations ({o['summary']['lambda_seconds']:.1f}s of Lambda)")
        if "build" in r:
            b, t = r["build"], r["test"]
            say(f"  build: {b['repo']}@{b['commit']} in {b['job_seconds']}s on {b['microvm_id'][:16]} pid {b['pid']}")
            say(f"  test:  {t['pytest_summary']} in {t['job_seconds']}s on {t['microvm_id'][:16]} pid {t['pid']}")
            say(f"         files from the build on disk: {t['repo_on_disk']}, "
                f"memory: commit={t['memory'].get('commit')} build_pid={t['memory'].get('build_pid')}")
        else:
            say(f"  {r}")
        say(f"  MicroVM lived {o['lifetime_seconds']}s, suspended ~{o['suspended_seconds']:.0f}s, "
            f"running ~{o['running_seconds']:.0f}s: ${microvm_cost(o['running_seconds']):.5f} of compute")
    save(f"06_session_{wait_s}", out)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
