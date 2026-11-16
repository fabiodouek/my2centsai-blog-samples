"""Step 3: create, resume and suspend latency under concurrency.

For each concurrency level N, N tasks are created at once, then resumed and
suspended all at once for three rounds. Every call is timed from the client
(the `ax` CLI), so each number includes its round trip through the CLI's
port-forward, as a user of AX would see it."""

import time

from common import (delete_all, ax, banner, create, describe, parallel, save, Sampler, timed_call)

delete_all()  # start from an empty atespace

LEVELS = [1, 5, 10, 20, 40]
ROUNDS = 3
LIMITS = {"cpu": "100m", "memory": "128Mi"}


def create_one(name):
    t0 = time.perf_counter()
    create(name, **LIMITS)
    return True, (time.perf_counter() - t0) * 1000, ""


def op(verb):
    return lambda name: timed_call(ax, verb, "task", name, quiet=True, check=False)


results = {}
with Sampler("latency"):
    for n in LEVELS:
        banner(f"concurrency {n}")
        names = [f"st-l{n}-{i:02d}" for i in range(n)]
        r = {"create": [], "resume": [], "suspend": [], "errors": []}
        out = parallel(create_one, names, n)
        r["create"] += [ms for ok, ms, _ in out if ok]
        r["errors"] += [e for ok, _, e in out if not ok]
        for _ in range(ROUNDS):
            for verb in ("resume", "suspend"):
                out = parallel(op(verb), names, n)
                r[verb] += [ms for ok, ms, _ in out if ok]
                r["errors"] += [e for ok, _, e in out if not ok]
        for verb in ("create", "resume", "suspend"):
            r[verb + "_stats"] = describe(f"{verb:7}", r[verb])
        if r["errors"]:
            print(f"  errors: {len(r['errors'])} ({r['errors'][0]})")
        results[n] = r
        delete_all(f"st-l{n}-")

save("latency", results)
