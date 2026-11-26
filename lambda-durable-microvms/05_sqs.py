"""SQS trigger: connect the queue to the function's alias, send a job, and follow the
execution it starts. Then send a job that runs longer than 15 minutes.

An SQS event source mapping invokes the function synchronously and waits for the whole
durable execution, so the message stays in flight until the execution ends. The mapping
refuses a durable function whose execution timeout is over 30 minutes; the script shows
that on the 2-hour pipeline function, then uses my2cents-ldm-queue (30 minutes). The queue's
visibility timeout is 30 minutes and its redrive policy moves a message to the DLQ after
one failed receive, so a failure never starts a second MicroVM.

usage: python 05_sqs.py [long_seconds]   (default 1200; 0 skips the long job)
"""

import json
import sys
import time
from datetime import datetime, timezone

from common import (ALIAS, FUNCTION, QUEUE_FUNCTION, banner, clock, history, lam, launched_microvms, lifetime, result_of, save, say,
                    sqs, summarize, tf, wait_done)


def try_pipeline_function() -> None:
    timeout = lam.get_function_configuration(FunctionName=FUNCTION, Qualifier=ALIAS)["DurableConfig"]["ExecutionTimeout"]
    try:
        m = lam.create_event_source_mapping(EventSourceArn=tf()["queue_arn"], FunctionName=f"{FUNCTION}:{ALIAS}",
                                            BatchSize=1, Enabled=False)
        lam.delete_event_source_mapping(UUID=m["UUID"])
        say(f"{FUNCTION} (execution timeout {timeout}s): mapping accepted")
    except lam.exceptions.InvalidParameterValueException as e:
        say(f"{FUNCTION} (execution timeout {timeout}s): {e.response['Error']['Message']}")


def mapping() -> dict:
    fn = f"{QUEUE_FUNCTION}:{ALIAS}"
    for m in lam.list_event_source_mappings(EventSourceArn=tf()["queue_arn"])["EventSourceMappings"]:
        if m["FunctionArn"].endswith(fn) and m["State"] != "Deleting":
            return m
    m = lam.create_event_source_mapping(EventSourceArn=tf()["queue_arn"], FunctionName=fn, BatchSize=1, Enabled=True)
    say(f"created event source mapping {m['UUID'][:8]} (batch size 1) -> {fn}")
    return m


def wait_enabled(uuid: str) -> None:
    while (st := lam.get_event_source_mapping(UUID=uuid)["State"]) != "Enabled":
        time.sleep(3)


def executions_since(t: datetime) -> list[dict]:
    r = lam.list_durable_executions_by_function(FunctionName=QUEUE_FUNCTION, StartedAfter=t)
    return r.get("DurableExecutions", [])


def send_and_follow(body: dict, label: str) -> dict:
    t = datetime.now(timezone.utc)
    sqs.send_message(QueueUrl=tf()["queue_url"], MessageBody=json.dumps(body))
    say(f"[{clock()}] sent {label}: {json.dumps(body)}")
    while not (found := executions_since(t)):
        time.sleep(1)
    arn = found[0]["DurableExecutionArn"]
    say(f"[{clock()}] the mapping started execution {found[0]['DurableExecutionName']} "
        f"({(datetime.now(timezone.utc) - t).total_seconds():.1f}s after the send)")
    # ApproximateNumberOfMessagesNotVisible lags by up to a minute, so sample it after that
    time.sleep(60)
    say(f"[{clock()}] queues while it runs: {queue_depths()}")
    last = [time.time()]

    def watch(d):
        if time.time() - last[0] >= 120:
            last[0] = time.time()
            say(f"[{clock()}] execution {d['Status']}")

    d = wait_done(arn, poll=5, on_poll=watch)
    s = summarize(history(arn))
    time.sleep(2)
    lives = [lifetime(v) for v in launched_microvms(history(arn))]
    say(f"[{clock()}] execution {d['Status']} after {s['total_seconds']:.1f}s, "
        f"{len(s['invocations'])} invocations, MicroVMs: {[(lv['state'], lv.get('seconds')) for lv in lives]}")
    r = result_of(d)
    say(f"  result: {json.dumps(r)[:300]}")
    return {"execution": arn, "status": d["Status"], "result": r, "summary": s, "microvms": lives}


def queue_depths() -> dict:
    out = {}
    for name in ("queue_url", "dlq_url"):
        a = sqs.get_queue_attributes(QueueUrl=tf()[name], AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        out[name.split("_")[0]] = {"visible": int(a["ApproximateNumberOfMessages"]),
                                   "in_flight": int(a["ApproximateNumberOfMessagesNotVisible"])}
    return out


def settled_depths() -> dict:
    """The mapping deletes the message a moment after the execution ends."""
    for _ in range(20):
        d = queue_depths()
        if d["queue"]["in_flight"] == 0:
            break
        time.sleep(2)
    return d


def main(long_seconds: int = 1200) -> None:
    banner("SQS -> durable function -> MicroVM")
    try_pipeline_function()
    m = mapping()
    wait_enabled(m["UUID"])
    short = send_and_follow({"demo": "task", "repo": "pallets/flask"}, "a scan job")
    say(f"queues after the short job: {settled_depths()}")
    out = {"short": short}
    if long_seconds:
        out["long"] = send_and_follow({"demo": "long", "seconds": long_seconds}, f"a {long_seconds // 60}-minute job")
        say(f"queues after the long job: {settled_depths()}")
    save(f"05_sqs_{long_seconds}", out)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
