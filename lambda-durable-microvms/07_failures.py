"""Failure semantics.

  fail    the job raises in the MicroVM: the durable function gets MicrovmJobFailedError
  crash   the job kills the worker process 5 seconds in: no result, no more heartbeats,
          so the operation fails with MicrovmTimeoutError at the 30-second heartbeat timeout
  retry   a job that fails on attempt 0: the extension does not run a failed job again,
          so the handler loops, and each attempt gets a fresh MicroVM

After each, the script checks that the operation terminated every MicroVM it launched.
"""

import threading
import time

from common import (banner, clock, history, launched_microvms, lifetime, result_of, save, say, start, summarize,
                    wait_done, warm_up)


def run(demo: str, out: dict) -> None:
    t0 = time.time()
    arn = start(demo)
    d = wait_done(arn, poll=1)
    ev = history(arn)
    s = summarize(ev)
    time.sleep(3)
    lives = [lifetime(v) for v in launched_microvms(ev)]
    out[demo] = {"execution": arn, "status": d["Status"], "result": result_of(d), "summary": s, "microvms": lives,
                 "wall_seconds": round(time.time() - t0, 1)}


def show(demo: str, o: dict) -> None:
    say(f"\n{demo}: execution {o['status']} in {o['summary']['total_seconds']:.1f}s")
    r = o["result"]
    if isinstance(r, list):
        for a in r:
            say(f"  attempt {a['attempt']}: {'ok on ' + a['microvm_id'][:16] if a['ok'] else 'failed: ' + a['error']}")
    else:
        for k, v in r.items():
            say(f"  {k}: {str(v)[:160]}")
    callbacks = [op for op in o["summary"]["operations"] if op["type"] == "Callback"]
    for c in callbacks:
        say(f"  {c['name']}: {c.get('outcome')} after {c.get('seconds')}s")
    for lv in o["microvms"]:
        say(f"  {lv['id'][:24]} {lv['state']} after {lv.get('seconds')}s ({lv.get('stateReason')})")


def main() -> None:
    banner("Failures: a failing job, a crashed worker, and retrying")
    warm_up()
    out: dict = {}
    threads = [threading.Thread(target=run, args=(d, out)) for d in ("fail", "crash")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    run("retry", out)
    for demo in ("fail", "crash", "retry"):
        show(demo, out[demo])
    save("07_failures", out)


if __name__ == "__main__":
    main()
