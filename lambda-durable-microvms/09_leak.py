"""The leak: the same scan from a function whose role lacks lambda:TerminateMicrovm.

The job succeeds and the execution succeeds, so nothing fails. The terminate step's error
is only logged. The MicroVM keeps running until its maximum duration, which the operation
sets to its timeout plus 5 minutes (here a 1-minute timeout, so 6 minutes).

The script finds the log line, then watches the MicroVM until the platform ends it.
"""

import json
import time

from common import (LEAKY_FUNCTION, banner, clock, function_logs, history, launched_microvms, microvm, microvm_cost,
                    result_of, save, say, start, ts, wait_done, warm_up)


def main() -> None:
    banner("A missing permission: lambda:TerminateMicrovm")
    warm_up(LEAKY_FUNCTION)
    t0 = time.time()
    arn = start("leak", function=LEAKY_FUNCTION)
    d = wait_done(arn, poll=1)
    r = result_of(d)
    say(f"[{clock()}] execution {d['Status']}: scanned {r.get('repo')}, {r.get('files')} files")
    vm = launched_microvms(history(arn))[0]
    m = microvm(vm)
    say(f"[{clock()}] {vm}: {m['state']}, maximumDurationInSeconds={m['maximumDurationInSeconds']}")
    time.sleep(10)
    lines = [l for l in function_logs(LEAKY_FUNCTION, t0 - 5) if "terminate failed" in l]
    for line in lines[:1]:
        try:
            entry = json.loads(line)
            say(f"function log, {entry['level']}: {entry['message'].split('. ')[0]}.")
        except ValueError:
            say(f"function log: {line[:240]}")
    while (m := microvm(vm))["state"] not in ("TERMINATED",):
        say(f"[{clock()}] {m['state']}, running for {time.time() - ts(m['startedAt']):.0f}s")
        time.sleep(60)
    life = ts(m["terminatedAt"]) - ts(m["startedAt"])
    say(f"[{clock()}] {m['state']} after {life:.0f}s ({m.get('stateReason')}): "
        f"${microvm_cost(life):.4f} of compute for a job that needed about 5 seconds (${microvm_cost(5):.5f})")
    save("09_leak", {"execution": arn, "status": d["Status"], "result": r, "microvm": vm, "lifetime_seconds": life,
                     "state_reason": m.get("stateReason"), "log_lines": lines[:3]})


if __name__ == "__main__":
    main()
