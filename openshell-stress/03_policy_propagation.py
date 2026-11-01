"""Test 3: policy propagation. N sandboxes start with no network rules; a probe in each
(probes/flip.py) tries the echo server every 50 ms. Then one policy change allows it, and each
probe records the moment its first request gets through. After that every probe holds a long
streaming response open, a second change (an unrelated rule, so only a new policy generation)
goes out, and each probe records when its stream is cut.

Times are measured against the revision's creation time on the gateway (gateway Pod clock) and
the probe's own clock (its node's clock): both nodes sync to the Amazon Time Sync Service, so
the laptop's clock never enters the numbers. The gateway's own view (loaded_at - created_at of
each sandbox's revision) is recorded next to them.

  --delivery poll   supervisors poll for configuration every 10 s (the default)
  --delivery push   values/push-delivery.yaml: the gateway pushes over each supervisor session
                    (a Helm change, rolled back at the end)
  --scope sandbox   one `openshell policy update` per sandbox, all sent at once
  --scope global    one `openshell policy set --global` for every sandbox on the gateway (deleted
                    at the end; it replaces the policy of EVERY sandbox, so only on a cluster
                    that runs nothing else)

    uv run python 03_policy_propagation.py --count 20 --delivery poll      # 10 changes
    uv run python 03_policy_propagation.py --count 20 --delivery push
    uv run python 03_policy_propagation.py --count 3 --repeats 2 --stream-s 40   # smoke
"""

from __future__ import annotations

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from lib import (ECHO_HOST, PhaseWatcher, POLICIES, PROBES, VALUES, add_results_arg, best_effort,
                 create_rpc, delete_all_p6, gateway_values, handle_signals, helm_commands, osh,
                 other_sandboxes, p6_client, put_file, require_gateway, results_dir, revisions,
                 sampling, say, sh_in, short, summarize, wait_ready, write_csv, write_json)

DELIVERY = {"poll": [], "push": [VALUES / "push-delivery.yaml"]}
PY = "/usr/bin/python3.12"


def read_events(c, name: str, path: str) -> list[dict]:
    r = sh_in(c, name, f"cat {path} 2>/dev/null || true", timeout=30)
    return [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]


def poll_events(c, names: list[str], path: str, want: set[str], limit_s: float) -> dict[str, list[dict]]:
    """Read every probe's file every 2 s until each has one of `want`, or limit_s passes."""
    start, events = time.monotonic(), {}
    with ThreadPoolExecutor(max_workers=min(16, len(names))) as ex:
        while True:
            for n, ev in zip(names, ex.map(lambda n: read_events(c, n, path), names)):
                events[n] = ev
            done = [n for n in names if any(e["event"] in want for e in events[n])]
            if len(done) == len(names) or time.monotonic() - start > limit_s:
                return events
            time.sleep(2)


def change(scope: str, names: list[str], step: str) -> dict:
    """Send one policy change. Returns {name: (call_start, call_end, version)} for sandbox
    scope, or {"*": ...} with the global revision."""
    if scope == "global":
        f = POLICIES / ("global-echo.yaml" if step == "allow" else "global-echo-marker.yaml")
        t0 = time.time()
        r = osh("policy", "set", "--global", "--policy", str(f), "--yes", check=False)
        t1 = time.time()
        if r.returncode:
            raise RuntimeError(f"policy set --global: {(r.stderr or r.stdout).strip()[-400:]}")
        rev = json.loads(osh("policy", "list", "--global", "-o", "json").stdout)["revisions"][0]
        return {"*": (t0, t1, rev.get("version"), rev.get("created_at_ms"))}
    rule = ["--rule-name", "p6_echo", "--binary", PY, "--add-endpoint", f"{ECHO_HOST}:80"] if step == "allow" \
        else ["--rule-name", "p6_marker", "--binary", PY, "--add-endpoint", f"{ECHO_HOST}:81"]

    def one(n: str):
        t0 = time.time()
        r = osh("policy", "update", n, *rule, check=False)
        t1 = time.time()
        m = re.search(r"[Pp]olicy version (\d+)", r.stdout + r.stderr)
        if r.returncode or not m:
            raise RuntimeError(f"policy update {n}: {(r.stderr or r.stdout).strip()[-300:]}")
        return n, (t0, t1, int(m.group(1)), None)

    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        return dict(ex.map(one, names))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--delivery", choices=DELIVERY, default="poll")
    ap.add_argument("--scope", choices=("sandbox", "global"), default="sandbox")
    ap.add_argument("--repeats", type=int, default=10,
                    help="changes to time; in poll mode every sandbox loads a change at about the same moment, "
                         "so the spread of delays comes from repeats, not from more sandboxes")
    ap.add_argument("--stream-s", type=int, default=90, help="length of the streaming response each probe holds")
    ap.add_argument("--spread-s", type=float, default=0, help="spread the creates over this many seconds (0 = all at once)")
    ap.add_argument("--print-helm", action="store_true")
    add_results_arg(ap)
    args = ap.parse_args()
    files = DELIVERY[args.delivery]
    if args.print_helm:
        up, down = helm_commands(files)
        print(" ".join(up) if files else "(poll: no gateway change)")
        if files:
            print(" ".join(down))
        return

    handle_signals()
    require_gateway()
    out = results_dir(args)
    tag = f"policy-{args.delivery}-{args.scope}"
    probe = (PROBES / "flip.py").read_bytes()
    names = [f"p6-pp{k + 1:02d}" for k in range(args.count)]
    rows: list[dict] = []
    global_set = False
    with p6_client() as c:
        if n_left := best_effort("delete", delete_all_p6, c):
            print(f"  deleted {n_left} p6 sandbox(es)")
        if (others := other_sandboxes(c)) and args.scope == "global":
            raise SystemExit(f"--scope global would replace the policy of {len(others)} other sandbox(es): {', '.join(others[:5])}")

    with gateway_values(files, f"policy delivery {args.delivery}"):
        with p6_client() as c, sampling(out / f"{tag}-sampler.csv", metrics_csv=out / f"{tag}-gateway.csv"):
            try:
                say(f"creating {args.count} sandboxes (no network rules)")
                watch = PhaseWatcher(c, interval=1.0)
                watch.start()
                # --spread-s spaces the creates out. (In the smoke runs it made no difference: with
                # creates 3 s apart, every sandbox still loaded each change within 25 ms of the others.)
                gap = args.spread_s / args.count

                def make(i_n):
                    time.sleep(i_n[0] * gap)
                    return create_rpc(c, i_n[1])
                with ThreadPoolExecutor(max_workers=min(20, args.count)) as ex:
                    made = list(ex.map(make, enumerate(names)))
                wait_ready(watch, made, 300)
                watch.stop()
                if bad := [f"{r['name']}: {r['final_phase']} {r.get('create_error', '')}" for r in made if r["final_phase"] != "Ready"]:
                    raise RuntimeError("not Ready: " + "; ".join(bad))
                with ThreadPoolExecutor(max_workers=min(16, args.count)) as ex:
                    list(ex.map(lambda n: put_file(c, n, "/tmp/p6-flip.py", probe), names))

                for rep in range(1, args.repeats + 1):
                    path = f"/tmp/p6-flip-{rep}.jsonl"
                    say(f"repeat {rep}: probes start (echo denied)")

                    def start(n: str) -> None:
                        cfg = json.dumps({"name": n, "host": ECHO_HOST, "stream_s": args.stream_s, "limit_s": 180})
                        sh_in(c, n, f"nohup setsid python3 /tmp/p6-flip.py '{cfg}' {path} >/dev/null 2>&1 < /dev/null &")
                    with ThreadPoolExecutor(max_workers=min(16, args.count)) as ex:
                        list(ex.map(start, names))
                    time.sleep(3)

                    say(f"change 1: allow the echo server ({args.scope} scope)")
                    allow = change(args.scope, names, "allow")
                    global_set |= args.scope == "global"
                    ev1 = poll_events(c, names, path, {"allowed", "timeout"}, 120)
                    n_ok = sum(any(e["event"] == "allowed" for e in ev1[n]) for n in names)
                    print(f"  {n_ok}/{len(names)} probes got through")
                    poll_events(c, names, path, {"stream_open", "cut", "stream_end"}, 30)
                    time.sleep(2)

                    say("change 2: an unrelated rule (a new policy generation only)")
                    marker = change(args.scope, names, "marker")
                    ev2 = poll_events(c, names, path, {"cut", "stream_end"}, args.stream_s + 20)

                    revs = {n: {r["version"]: r for r in revisions(n)} for n in names} if args.scope == "sandbox" else {}
                    for n in names:
                        e = {x["event"]: x for x in ev2[n]}
                        a = allow.get(n) or allow["*"]
                        m = marker.get(n) or marker["*"]
                        ra = revs.get(n, {}).get(a[2], {})
                        rm = revs.get(n, {}).get(m[2], {})
                        created_a = (ra.get("created_at_ms") or a[3] or 0) / 1000
                        created_m = (rm.get("created_at_ms") or m[3] or 0) / 1000
                        row = {"delivery": args.delivery, "scope": args.scope, "repeat": rep, "name": n,
                               "allow_version": a[2], "allow_call_s": round(a[1] - a[0], 3),
                               "allow_created": created_a, "allow_loaded": (ra.get("loaded_at_ms") or 0) / 1000 or "",
                               "gw_allow_load_s": round((ra["loaded_at_ms"] - ra["created_at_ms"]) / 1000, 3) if ra.get("loaded_at_ms") else "",
                               "probe_allow_s": round(e["allowed"]["t"] - created_a, 3) if "allowed" in e and created_a else "",
                               "probe_tries": e.get("allowed", {}).get("tries", ""),
                               "marker_version": m[2], "marker_created": created_m,
                               "gw_marker_load_s": round((rm["loaded_at_ms"] - rm["created_at_ms"]) / 1000, 3) if rm.get("loaded_at_ms") else "",
                               "stream_cut": "cut" in e, "stream_end": "stream_end" in e,
                               "cut_s": round(e["cut"]["t"] - created_m, 3) if "cut" in e and created_m else "",
                               "cut_ticks": e.get("cut", {}).get("ticks", ""), "cut_error": e.get("cut", {}).get("error", "")}
                        rows.append(row)
                    got = [r for r in rows if r["repeat"] == rep]
                    pa = summarize([r["probe_allow_s"] or None for r in got])
                    ga = summarize([r["gw_allow_load_s"] or None for r in got])
                    cs = summarize([r["cut_s"] or None for r in got])
                    print(f"  allowed after (probe): p50 {pa['p50']:.2f} s, max {pa['max']:.2f} s;  "
                          f"gateway loaded_at - created_at: p50 {ga['p50']:.2f} s, max {ga['max']:.2f} s")
                    print(f"  streams cut: {sum(r['stream_cut'] for r in got)}/{len(got)}, after p50 {cs['p50']:.2f} s, "
                          f"max {cs['max']:.2f} s;  ran to the end: {sum(r['stream_end'] for r in got)}")

                    # Back to no network rules for the next repeat; stop any probe still running.
                    with ThreadPoolExecutor(max_workers=min(16, args.count)) as ex:
                        list(ex.map(lambda n: sh_in(c, n, "pkill -f '[p]6-flip.py'; true"), names))
                    if rep < args.repeats:
                        if args.scope == "global":
                            osh("policy", "delete", "--global", "--yes")
                            global_set = False
                        else:
                            with ThreadPoolExecutor(max_workers=args.count) as ex:
                                list(ex.map(lambda n: osh("policy", "update", n, "--remove-rule", "p6_echo",
                                                          "--remove-rule", "p6_marker", "--wait"), names))
                        time.sleep(12 if args.delivery == "poll" else 3)
            finally:
                write_csv(out / f"{tag}.csv", rows)
                if global_set:
                    r = osh("policy", "delete", "--global", "--yes", check=False)
                    print("global policy deleted" if r.returncode == 0 else f"WARNING: policy delete --global: {short(Exception(r.stderr))}")
                if n_left := best_effort("delete", delete_all_p6, c):
                    print(f"  deleted {n_left} p6 sandbox(es)")
    write_json(out / f"{tag}-summary.json", {
        "delivery": args.delivery, "scope": args.scope, "count": args.count, "repeats": args.repeats,
        "probe_allow_s": summarize([r["probe_allow_s"] or None for r in rows]),
        "gateway_load_s": summarize([r["gw_allow_load_s"] or None for r in rows]),
        "cut_s": summarize([r["cut_s"] or None for r in rows]),
        "streams_cut": sum(r["stream_cut"] for r in rows), "streams": len(rows)})
    print(f"results: {out}/{tag}.csv")


if __name__ == "__main__":
    main()
