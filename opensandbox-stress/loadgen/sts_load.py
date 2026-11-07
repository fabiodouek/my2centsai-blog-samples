"""STS GetCallerIdentity calls from inside a sandbox, for the SigV4 rung of 05_egress_ladder.py.

Runs in part 5's aws-tools image (boto3 installed). Two modes:

  signed    the sandbox's placeholder keys (the broker sets them); the egress sidecar adds the real
            short-lived credentials as carrier headers and the SigV4 addon re-signs each request.
            Expect HTTP 200. This is the rung.
  unsigned  no signature at all (botocore UNSIGNED), from a sandbox with no policy and no sidecar:
            AWS answers 403 at once. The time is the network and AWS front-end floor that the
            signed rung pays too.

boto3 keeps its connections open, as a real client does; retries are off, so every call is one
round trip. The response (it names the account and the role) is never printed: the script prints
one JSON line with latencies and counts of outcomes only.

Only answered calls are timed: in signed mode a 200 ("ok"), in unsigned mode any answer AWS gave
(its error code, e.g. MissingAuthenticationToken). A call that failed (killed by the addon, no
answer, a signed call AWS refused) is counted in "failed", never timed: a fast failure must not
pass for a fast call.

  python3 sts_load.py --mode signed --sequential 100 --concurrency 4 --per-worker 25
"""

import argparse
import json
import os
import threading
import time

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def client(mode: str, region: str):
    config = Config(retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=10, read_timeout=20)
    if mode == "unsigned":
        config = config.merge(Config(signature_version=UNSIGNED))
    return boto3.client("sts", region_name=region, endpoint_url=f"https://sts.{region}.amazonaws.com", config=config)


def call(sts) -> tuple[float, str]:
    """(ms, outcome): outcome is "ok", an AWS error code, or the exception's class name."""
    started = time.perf_counter()
    try:
        sts.get_caller_identity()
        outcome = "ok"
    except ClientError as error:
        outcome = error.response.get("Error", {}).get("Code", "ClientError")
    except BotoCoreError as error:
        outcome = type(error).__name__
    return 1000 * (time.perf_counter() - started), outcome


def answered(mode: str, outcome: str) -> bool:
    """Was this a real answer for the mode? signed: only "ok"; unsigned: any AWS error code."""
    if mode == "signed":
        return outcome == "ok"
    return outcome not in ("ok",) and not outcome.endswith(("Error", "Exception", "Timeout"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("signed", "unsigned"), required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--sequential", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--per-worker", type=int, default=25)
    args = parser.parse_args()
    outcomes: dict[str, int] = {}
    lock = threading.Lock()

    def count(outcome: str) -> None:
        with lock:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

    sts = client(args.mode, args.region)
    for _ in range(args.warmup):
        call(sts)
    seq, seq_failed = [], 0
    for _ in range(args.sequential):
        ms, outcome = call(sts)
        count(outcome)
        if answered(args.mode, outcome):
            seq.append(round(ms, 3))
        else:
            seq_failed += 1

    load: list[float] = []
    load_failed = [0]

    def worker() -> None:
        mine = client(args.mode, args.region)
        local, failed = [], 0
        for _ in range(args.per_worker):
            ms, outcome = call(mine)
            count(outcome)
            if answered(args.mode, outcome):
                local.append(round(ms, 3))
            else:
                failed += 1
        with lock:
            load.extend(local)
            load_failed[0] += failed

    started = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(args.concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started
    print(json.dumps({"mode": args.mode, "outcomes": outcomes,
                      "sequential": {"n": len(seq), "failed": seq_failed, "ms": seq},
                      "load": {"concurrency": args.concurrency, "n": len(load), "failed": load_failed[0],
                               "duration_s": round(wall, 3), "rps": round(len(load) / wall, 1) if wall else 0,
                               "ms": load}}), flush=True)


if __name__ == "__main__":
    main()
