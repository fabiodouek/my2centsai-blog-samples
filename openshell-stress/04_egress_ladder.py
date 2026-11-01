"""Test 4: the egress overhead ladder. One sandbox, one destination, the same benchmark
(probes/bench.py) under policies that ask the supervisor to do more and more:

  direct         a plain Pod on the same node, no OpenShell (the baseline)
  l4-tls-skip    the supervisor relays bytes without looking at them (tls: skip)
  l4             no request rules, but HTTP requests are checked for host and path
  rest-audit     protocol: rest, access: full, enforcement audit (the default)
  rest-enforce   protocol: rest, enforced allow rules for GET and POST /bench/**
  credential     the p6-echo provider (profile echo-demo): REST enforce plus a bearer token
                 substituted into every request by the supervisor (dummy value)

The destination is the in-cluster echo server (plain HTTP on port 80, from part 4). With
--https the ladder also runs against an AWS endpoint in the Region (s3.us-east-1.amazonaws.com,
an unauthenticated GET /, through the NAT gateway): direct, tls: skip, TLS terminated by the
supervisor, and REST enforce.

The workload Pod is pinned to the echo server's node; --supervisor decides where the supervisor
Pod must land (the script recreates the sandbox until it does): results go to ladder.csv for
"same" and ladder-other.csv for "other". The echo server writes each response in two pieces, so
keep-alive requests to it wait out a delayed ACK (about 40 ms, in the direct baseline too): read
the new-connection numbers for echo, and keep-alive only for the HTTPS rungs.

Each rung: --fresh requests on new connections, --keepalive requests on one connection,
--throughput-s seconds of --threads parallel clients, --upload POSTs of 1 MiB (echo only).

    uv run python 04_egress_ladder.py --https                      # supervisor on the workload's node
    uv run python 04_egress_ladder.py --https --supervisor other   # every request crosses nodes twice
    uv run python 04_egress_ladder.py --fresh 50 --keepalive 50 --throughput-s 3 --upload 5 --https   # smoke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time

from lib import (ECHO_HOST, HERE, POLICIES, PROBES, add_results_arg, best_effort, create_cli,
                 delete_sandboxes, handle_signals, kubectl, load_policy, node_selector, osh,
                 p6_client, pods_of, require_gateway, results_dir, run, say, set_network_policies,
                 short, summarize, workload_image, write_csv, write_json)

NAME, POD, PROVIDER = "p6-ladder", "p6-direct", "p6-echo"
DUMMY = "p6-dummy"   # the provider's credential value: a dummy, shown back by the echo server
S3_HOST = "s3.us-east-1.amazonaws.com"
ECHO_RUNGS = [("direct", None), ("l4-tls-skip", "echo-l4-tls-skip.yaml"), ("l4", "echo-l4.yaml"),
              ("rest-audit", "echo-rest-audit.yaml"), ("rest-enforce", "echo-rest-enforce.yaml"),
              ("credential", "base.yaml")]
HTTPS_RUNGS = [("https-direct", None), ("https-tls-skip", "https-tls-skip.yaml"),
               ("https-terminate", "https-terminate.yaml"), ("https-rest-enforce", "https-rest-enforce.yaml")]


def start_direct_pod(node: str) -> None:
    manifest = (HERE / "manifests" / "direct-client.yaml").read_text()
    manifest = manifest.replace("${NODE}", node).replace("${IMAGE}", workload_image())
    kubectl("delete", "pod", "-n", "default", POD, "--ignore-not-found", "--wait=true", timeout=120)
    kubectl("apply", "-f", "-", input=manifest)
    kubectl("wait", "-n", "default", f"pod/{POD}", "--for=condition=Ready", "--timeout=180s", timeout=200)


def bench_direct(cfg: dict) -> dict:
    r = run(["kubectl", "--context", "osh-eks", "exec", "-i", "-n", "default", POD, "--", "python3", "-",
             json.dumps(cfg)], input=(PROBES / "bench.py").read_text(), timeout=900)
    return json.loads(r.stdout.strip().splitlines()[-1])


def bench_sandbox(c, cfg: dict) -> dict:
    r = c.exec(NAME, ["python3", "-", json.dumps(cfg)], workspace="default", no_login_shell=True,
               timeout_seconds=900, stdin=(PROBES / "bench.py").read_bytes())
    if r.exit_code != 0 or not r.stdout.strip():
        raise RuntimeError(f"bench exit {r.exit_code}: {r.stderr.strip()[-400:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def ensure_provider() -> bool:
    """p6-echo from the echo-demo profile part 4 imported; the value comes from the environment,
    never the command line. False if the profile isn't on the gateway."""
    profiles = osh("provider", "list-profiles", check=False).stdout
    if "echo-demo" not in profiles:
        print("  profile echo-demo not on the gateway (part 4 imports it): skipping the credential rung")
        return False
    osh("provider", "delete", PROVIDER, check=False)
    osh("provider", "create", "--name", PROVIDER, "--type", "echo-demo", "--credential", "ECHO_TOKEN",
        env={**os.environ, "ECHO_TOKEN": DUMMY})
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fresh", type=int, default=500)
    ap.add_argument("--keepalive", type=int, default=500)
    ap.add_argument("--throughput-s", type=float, default=20)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--upload", type=int, default=30)
    ap.add_argument("--https", action="store_true", help="also run the HTTPS rungs against an AWS endpoint")
    ap.add_argument("--node", help="node for the sandbox's workload Pod (default: the echo server's node)")
    ap.add_argument("--rungs", help="comma-separated subset of rung names")
    ap.add_argument("--supervisor", choices=("same", "other", "any"), default="same",
                    help="the supervisor Pod on the workload's node, the other node, or wherever it lands")
    add_results_arg(ap)
    args = ap.parse_args()
    handle_signals()
    require_gateway()
    out = results_dir(args)
    scratch = out / "scratch"
    scratch.mkdir(exist_ok=True)

    echo_pods = json.loads(kubectl("-n", "echo", "get", "pods", "-l", "app=echo", "-o", "json", check=False) or '{"items": []}')["items"]
    if not echo_pods:
        raise SystemExit("the echo server isn't running: run ../openshell-egress/00_echo.sh, and import its profile with "
                         "openshell -g osh-eks profile import -f ../openshell-egress/profiles/echo-demo.yaml")
    echo_pod = echo_pods[0]
    node = args.node or echo_pod["spec"]["nodeName"]
    suffix = "" if args.supervisor == "same" else f"-{args.supervisor}"
    rungs = ECHO_RUNGS + (HTTPS_RUNGS if args.https else [])
    if args.rungs:
        rungs = [r for r in rungs if r[0] in args.rungs.split(",")]
    summary_rows, raw_rows = [], []
    have_provider = attached = False
    with p6_client() as c:
        try:
            delete_sandboxes(c, [NAME])  # a leftover, or a Sandbox CR an earlier run orphaned
            say(f"sandbox {NAME}, workload pinned to {node}")
            # Created with no network rules: in 0.1.6 a provider attach is checked against the
            # policy a sandbox was created with, and an L4-only rule for the echo server there
            # makes the later credential attach fail (FAILED_PRECONDITION, "credentialed endpoint
            # ... uses L4-only"), even after the rule is gone. Each rung sets its own rules.
            # The workload Pod can be pinned, the supervisor Pod can't: recreate the sandbox until
            # the supervisor lands where --supervisor asks (it decides whether every request
            # crosses nodes twice).
            for attempt in range(1, 11):
                secs = create_cli(NAME, "--policy", str(POLICIES / "base.yaml"),
                                  "--driver-config-json", node_selector(node))
                where = pods_of(c.get(NAME, workspace="default").id)
                same = where.get("supervisor") == node
                if args.supervisor == "any" or same == (args.supervisor == "same"):
                    break
                print(f"  supervisor on {where.get('supervisor')}, want {args.supervisor} node: recreating ({attempt})")
                delete_sandboxes(c, [NAME])
            else:
                raise RuntimeError(f"supervisor never landed on the {args.supervisor} node in 10 tries")
            path = (f"workload {where.get('workload', '?')}, supervisor {where.get('supervisor', '?')}, "
                    f"echo {echo_pod['spec']['nodeName']}")
            print(f"  Ready in {secs:.1f} s; {path}")
            say(f"baseline Pod {POD} (no OpenShell) on {node}")
            start_direct_pod(node)
            if any(r[0] == "credential" for r in rungs):
                have_provider = ensure_provider()

            for rung, policy in rungs:
                if rung == "credential" and not have_provider:
                    continue
                https = rung.startswith("https")
                cfg = {"rung": rung, "host": S3_HOST if https else ECHO_HOST, "port": 443 if https else 80,
                       "scheme": "https" if https else "http", "path": "/" if https else "/bench/get",
                       "fresh": args.fresh, "keepalive": args.keepalive, "throughput_s": args.throughput_s,
                       "threads": args.threads, "upload": 0 if https else args.upload}
                say(f"rung {rung}")
                if policy:
                    set_network_policies(NAME, load_policy(POLICIES / policy).get("network_policies") or {}, scratch)
                if rung == "credential":
                    osh("sandbox", "provider", "attach", NAME, PROVIDER, "--wait", "--timeout", "60")
                    attached = True
                    cfg["auth_env"] = "ECHO_TOKEN"
                t = time.monotonic()
                try:
                    res = bench_direct(cfg) if rung.endswith("direct") else bench_sandbox(c, cfg)
                except Exception as e:
                    print(f"  failed: {short(e, 400)}")
                    summary_rows.append({"rung": rung, "error": short(e, 400)})
                    continue
                finally:
                    if attached:
                        osh("sandbox", "provider", "detach", NAME, PROVIDER, "--wait", check=False)
                        attached = False
                rec = {"rung": rung, "scheme": cfg["scheme"], "path": "direct" if rung.endswith("direct") else path,
                       "took_s": round(time.monotonic() - t, 1), "errors": json.dumps(res.get("errors", {}))}
                if "auth" in res:
                    want = hashlib.sha256(f"Bearer {DUMMY}".encode()).hexdigest()[:12]
                    rec["auth_substituted"] = res["auth"].get("sha256_12") == want
                    rec["auth_header_len"] = res["auth"].get("length")
                for mode in ("fresh", "keepalive", "upload"):
                    if mode in res:
                        s = summarize(res[mode]["lat_ms"])
                        rec.update({f"{mode}_n": s["n"], f"{mode}_p50_ms": round(s["p50"], 2),
                                    f"{mode}_p95_ms": round(s["p95"], 2), f"{mode}_p99_ms": round(s["p99"], 2),
                                    f"{mode}_codes": json.dumps(res[mode]["codes"])})
                        raw_rows += [{"rung": rung, "mode": mode, "i": i, "ms": v} for i, v in enumerate(res[mode]["lat_ms"])]
                if "upload" in res:
                    rec["upload_mib_s"] = res["upload"]["mib_per_s"]
                if "throughput" in res:
                    rec.update({"rps": res["throughput"]["rps"], "rps_failed": res["throughput"]["failed"],
                                "rps_threads": res["throughput"]["threads"]})
                summary_rows.append(rec)
                print(f"  new connection p50 {rec.get('fresh_p50_ms', float('nan')):.2f} ms p95 {rec.get('fresh_p95_ms', float('nan')):.2f}   "
                      f"keep-alive p50 {rec.get('keepalive_p50_ms', float('nan')):.2f} ms   {rec.get('rps', 0):.0f} req/s"
                      + (f"   upload {rec['upload_mib_s']} MiB/s" if 'upload_mib_s' in rec else "")
                      + (f"   token substituted: {rec['auth_substituted']}" if 'auth_substituted' in rec else "")
                      + (f"   errors {rec['errors']}" if rec['errors'] != '{}' else ""), flush=True)
        finally:
            write_csv(out / f"ladder{suffix}.csv", summary_rows)
            write_csv(out / f"ladder{suffix}-raw.csv", raw_rows)
            best_effort("baseline Pod", kubectl, "delete", "pod", "-n", "default", POD, "--ignore-not-found",
                        "--wait=false", check=False)
            if attached:
                best_effort("provider detach", osh, "sandbox", "provider", "detach", NAME, PROVIDER, check=False)
            best_effort("sandbox", delete_sandboxes, c, [NAME])
            if have_provider:
                best_effort("provider", osh, "provider", "delete", PROVIDER, check=False)
    write_json(out / f"ladder{suffix}-summary.json", {"node": node, "supervisor": args.supervisor, "rungs": summary_rows})
    print(f"results: {out}/ladder{suffix}.csv, ladder{suffix}-raw.csv")


if __name__ == "__main__":
    main()
