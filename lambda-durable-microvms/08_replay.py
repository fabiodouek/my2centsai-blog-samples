"""Replay safety: the handler crashes (os._exit) right after the MicroVM job has finished.

The crash is a side effect on purpose: the handler writes a marker object to S3 and exits
only when the marker isn't there yet, so only the first pass crashes. What the script
checks: did Lambda run the handler again, did the replay launch a second MicroVM, and did
the job run a second time?
"""

import json
import time

from common import (banner, clock, history, launched_microvms, lifetime, list_microvms, print_timeline, result_of, save,
                    say, start, summarize, wait_done, warm_up)


def main() -> None:
    banner("Replay: crash the function after the job, see what runs again")
    warm_up()
    before = {i["microvmId"] for i in list_microvms()}
    run_id = f"r{int(time.time())}"
    arn = start("replay", run_id=run_id)
    say(f"[{clock()}] started (run_id {run_id})")
    d = wait_done(arn, poll=1, timeout=900)
    ev = history(arn)
    s = summarize(ev)
    say(f"[{clock()}] execution {d['Status']} after {s['total_seconds']:.1f}s")
    say("\ninvocations:")
    for i in s["invocations"]:
        say(f"  at {i['start']:6.2f}s ran {i['seconds']:.3f}s{'  <- error (crashed)' if i['error'] else ''}")
    for e in ev:
        if e["EventType"] == "InvocationCompleted" and e["InvocationCompletedDetails"].get("Error"):
            err = e["InvocationCompletedDetails"]["Error"].get("Payload", "")
            say(f"  invocation error: {json.dumps(err)[:200] if not isinstance(err, str) else err[:200]}")
    say("\noperations:")
    print_timeline(s)
    launches = [o for o in s["operations"] if (o["name"] or "").endswith(".launch")]
    time.sleep(3)
    new = [i["microvmId"] for i in list_microvms() if i["microvmId"] not in before]
    say(f"\nlaunch steps recorded: {len(launches)}; MicroVMs created by this run: {len(new)}")
    for v in new:
        say(f"  {lifetime(v)}")
    r = result_of(d)
    say(f"result: crashed_before={r.get('crashed_before') if isinstance(r, dict) else r}, "
        f"scan ran on {r['scan']['microvm_id'][:24] if isinstance(r, dict) and 'scan' in r else '-'}")
    save("08_replay", {"execution": arn, "status": d["Status"], "result": r, "summary": s, "new_microvms": new})


if __name__ == "__main__":
    main()
