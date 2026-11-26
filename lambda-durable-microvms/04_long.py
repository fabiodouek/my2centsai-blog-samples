"""Beyond the function's timeout: a 20-minute job from a function whose timeout is 120 seconds
(Lambda's maximum is 30 minutes; a MicroVM job can run up to 8 hours).

The function launches the MicroVM, its invocation ends, and no invocation runs while the
MicroVM works: the worker's heartbeats go to the durable callback, not to the function.
Prints, once a minute, the execution status, the MicroVM state and how many invocations
have run so far, then compares Lambda seconds with MicroVM seconds.

usage: python 04_long.py [seconds]   (default 1200, 20 minutes)
"""

import sys
import time

from common import (banner, clock, history, launched_microvms, lifetime, microvm, microvm_cost, result_of, save, say,
                    start, summarize, wait_done)


def main(seconds: int = 1200) -> None:
    banner(f"A {seconds // 60}-minute job from a function with a 2-minute timeout")
    arn = start("long", seconds=seconds)
    say(f"[{clock()}] started")
    last = [0.0]

    def watch(d):
        if time.time() - last[0] < 60:
            return
        last[0] = time.time()
        ev = history(arn)
        s = summarize(ev)
        vms = launched_microvms(ev)
        state = microvm(vms[0])["state"] if vms else "-"
        say(f"[{clock()}] execution {d['Status']}, MicroVM {state}, invocations so far: {len(s['invocations'])} "
            f"({s['lambda_seconds']:.1f}s)")

    d = wait_done(arn, poll=5, on_poll=watch)
    ev = history(arn)
    s = summarize(ev)
    time.sleep(2)
    lives = [lifetime(v) for v in launched_microvms(ev)]
    vm_s = sum(lv.get("seconds") or 0 for lv in lives)
    r = result_of(d)
    say(f"[{clock()}] execution {d['Status']} after {s['total_seconds'] / 60:.1f} min: {r}")
    say(f"function: {len(s['invocations'])} invocations, {s['lambda_seconds']:.2f}s in total")
    say(f"MicroVM:  {vm_s:.0f}s running, ${microvm_cost(vm_s):.4f}")
    save(f"04_long_{seconds}", {"execution": arn, "status": d["Status"], "result": r, "summary": s, "microvms": lives})


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
