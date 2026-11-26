"""Task mode: one microvm() job (scan a repo) from an asynchronous invoke.

Prints the job's result, the durable operations the execution recorded and their
timings, how long the function actually ran (its invocations), and the MicroVM's
lifetime and final state.
"""

import sys
import time

from common import (banner, clock, history, launched_microvms, lifetime, microvm, microvm_cost, print_timeline,
                    result_of, save, say, start, summarize, wait_done, warm_up)


def main(repo: str = "pallets/click") -> None:
    banner(f"Task mode: scan {repo} in one MicroVM")
    warm_up()  # so the timings below don't include a cold start
    t0 = time.time()
    arn = start("task", repo=repo)
    say(f"[{clock()}] started execution {arn.split('/')[-2]}")
    seen = set()

    def watch(d):
        ids = launched_microvms(history(arn))
        for vm in ids:
            if vm not in seen:
                seen.add(vm)
                say(f"[{clock()}] launched {vm}: {microvm(vm)['state']}")

    d = wait_done(arn, poll=1, on_poll=watch)
    wall = time.time() - t0
    r = result_of(d)
    say(f"[{clock()}] execution {d['Status']} after {wall:.1f}s")
    say(f"  result: files={r.get('files')} lines={r.get('lines')} top={r.get('top_extensions')}")
    say(f"          secret-like strings={r.get('secret_like_strings')} clone={r.get('clone_seconds')}s "
        f"job={r.get('job_seconds')}s")

    events = history(arn)
    s = summarize(events)
    say("\noperations (start, duration, type, name):")
    print_timeline(s)
    say(f"\nbilled durable operations: {s['billed_operations']}")
    say(f"function invocations: {len(s['invocations'])}, "
        f"total {s['lambda_seconds']:.2f}s of {s['total_seconds']:.1f}s")
    for i in s["invocations"]:
        say(f"  at {i['start']:6.2f}s ran {i['seconds']:.3f}s")

    vms = launched_microvms(events)
    time.sleep(2)
    lives = [lifetime(v) for v in vms]
    for lv in lives:
        say(f"MicroVM {lv['id']}: {lv['state']}, lived {lv.get('seconds')}s "
            f"(${microvm_cost(lv.get('seconds') or 0):.5f} at the 2 GB / 1 vCPU rate)")
    save("02_task", {"execution": arn, "status": d["Status"], "wall_seconds": round(wall, 2), "result": r,
                     "summary": s, "microvms": lives})


if __name__ == "__main__":
    main(*sys.argv[1:])
