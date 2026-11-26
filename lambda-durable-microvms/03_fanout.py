"""Fan-out: the same four scans with context.map (one MicroVM per repo, at most four at a
time) and then one after another, to compare wall time, Lambda time and MicroVM time.

Then five repos at once, one more than the account's MicroVM memory quota allows (the
account used for the post has 8 GB, four 2 GB MicroVMs). With the map's default fail-fast
completion, the fifth launch fails, the map ends early, and the four MicroVMs that did
launch keep running: nothing is left to terminate them. The script shows that and
terminates them itself. Then it runs the same five with a retry strategy that waits out
ServiceQuotaExceededException and an all_completed() map, so every item finishes and every
MicroVM is terminated.
Pass --skip-quota to run only the first comparison.
"""

import sys

import time

from common import (banner, clock, history, launched_microvms, lifetime, microvm, microvm_cost, mvm, result_of, save,
                    say, start, summarize, wait_done, warm_up)

REPOS = ["pallets/click", "pallets/flask", "psf/requests", "encode/httpx"]


def run(demo: str, repos=REPOS, max_concurrency=4, terminate_leftovers=False, **extra) -> dict:
    t0 = time.time()
    arn = start(demo, repos=repos, max_concurrency=max_concurrency, **extra)
    say(f"[{clock()}] {demo}: started")
    d = wait_done(arn, poll=1)
    wall = time.time() - t0
    events = history(arn)
    s = summarize(events)
    launches = sorted((o["start"], o["name"]) for o in s["operations"] if (o["name"] or "").endswith(".launch"))
    time.sleep(2)
    lives = [lifetime(v) for v in launched_microvms(events)]
    vm_seconds = sum(lv.get("seconds") or 0 for lv in lives)
    r = result_of(d)
    say(f"[{clock()}] {demo}: {d['Status']} in {s['total_seconds']:.1f}s, "
        f"{len(s['invocations'])} invocations ({s['lambda_seconds']:.2f}s of Lambda), "
        + (f"{len(lives)} MicroVMs ({sum(1 for lv in lives if 'seconds' not in lv)} still running), "
           if any("seconds" not in lv for lv in lives) else
           f"{len(lives)} MicroVMs ({vm_seconds:.0f}s, ${microvm_cost(vm_seconds):.5f}), ") +
        f"{s['billed_operations']} operations")
    say("  launches at: " + ", ".join(f"{t:.2f}s" for t, _ in launches))
    if isinstance(r, dict) and "completion_reason" in r:
        say(f"  map completion: {r['completion_reason']}, items: {r['statuses']}")
        for e in r["errors"]:
            say(f"  error: {e[:112]}")
    leftovers = [lv for lv in lives if lv["state"] in ("RUNNING", "PENDING")]
    if leftovers:
        say(f"  {len(leftovers)} MicroVMs still RUNNING after the execution ended "
            f"(max duration {microvm(leftovers[0]['id'])['maximumDurationInSeconds']}s)")
        if terminate_leftovers:
            for lv in leftovers:
                mvm.terminate_microvm(microvmIdentifier=lv["id"])
            while any(microvm(lv["id"])["state"] != "TERMINATED" for lv in leftovers):
                time.sleep(2)  # their memory counts against the quota until TERMINATED
            say(f"  terminated them from this script")
    items = r["results"] if isinstance(r, dict) and "results" in r else r
    for item in items if isinstance(items, list) else []:
        say(f"  {item['repo']:<14} files={item['files']:<4} lines={item['lines']:<6} job={item['job_seconds']}s "
            f"on {item['microvm_id'][:16]}")
    return {"demo": demo, "leftovers": leftovers, "execution": arn, "status": d["Status"], "wall_seconds": round(wall, 2), "result": r,
            "summary": s, "microvms": lives, "microvm_seconds": vm_seconds, "launch_times": launches}


def main() -> None:
    banner("Fan-out: four repos, four MicroVMs, one durable function")
    warm_up()
    parallel = run("fanout")
    serial = run("serial")
    speedup = serial["summary"]["total_seconds"] / parallel["summary"]["total_seconds"]
    say(f"\nmap: {parallel['summary']['total_seconds']:.1f}s   serial: {serial['summary']['total_seconds']:.1f}s"
        f"   ({speedup:.1f}x faster with the same MicroVM seconds: "
        f"{parallel['microvm_seconds']:.0f}s vs {serial['microvm_seconds']:.0f}s)")
    save("03_fanout", {"parallel": parallel, "serial": serial})
    if "--skip-quota" in sys.argv:
        return
    banner("Five repos, quota for four")
    five = REPOS + ["pallets/jinja"]
    over = run("fanout", repos=five, max_concurrency=5, terminate_leftovers=True)
    fixed = run("fanout", repos=five, max_concurrency=5, quota_retry=True, all_completed=True)
    save("03_quota", {"over_quota": over, "quota_retry": fixed})


if __name__ == "__main__":
    main()
