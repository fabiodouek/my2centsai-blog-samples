"""The durable function: one handler, one demo per event["demo"].

Every MicroVM call is a durable operation from the MicroVM extension. The
function launches the MicroVM, hands it the job, and its invocation ends while
the MicroVM works. The worker in the MicroVM completes a durable callback with
the result, Lambda invokes the function again, it replays up to that point, and
terminates the MicroVM.

The event can also arrive from SQS (05_sqs.py): the message body is the event.
"""

import json
import os
import time
from typing import Any

import boto3
from aws_durable_execution_sdk_python.config import CompletionConfig, Duration, MapConfig
from aws_durable_execution_sdk_python.context import DurableContext
from aws_durable_execution_sdk_python.execution import durable_execution
from aws_durable_execution_sdk_python_extensions.compute.microvm import (
    MicrovmConfig,
    MicrovmError,
    MicrovmInvokeOptions,
    MicrovmJobFailedError,
    MicrovmSession,
    MicrovmSessionConfig,
    MicrovmTimeoutError,
    default_microvm_retry_strategy,
    microvm,
    microvm_session,
)
from aws_durable_execution_sdk_python.retries import RetryDecision

IMAGE = os.environ["MICROVM_IMAGE_ARN"]
ROLE = os.environ["MICROVM_EXECUTION_ROLE_ARN"]
BUCKET = os.environ.get("BUCKET", "")


def quota_aware_retry(error: Exception, attempt: int) -> RetryDecision:
    """The default strategy fails a launch at once on ServiceQuotaExceededException.
    Retry that one every 5 seconds instead: MicroVM memory frees up as others terminate."""
    code = getattr(error, "response", {}).get("Error", {}).get("Code")
    if code == "ServiceQuotaExceededException" and attempt < 24:
        return RetryDecision.retry(Duration.from_seconds(5))
    return default_microvm_retry_strategy(error, attempt)


def config(minutes: float = 10, heartbeat_seconds: int = 60, quota_retry: bool = False) -> MicrovmConfig:
    return MicrovmConfig(
        image_identifier=IMAGE,
        execution_role_arn=ROLE,
        retry_strategy=quota_aware_retry if quota_retry else None,
        # The MicroVM lives at most timeout + 5 minutes
        timeout=Duration.from_seconds(int(minutes * 60)),
        # Fails the job if the worker goes quiet, instead of waiting for the timeout
        heartbeat_timeout=Duration.from_seconds(heartbeat_seconds),
    )


def scan_one(ctx: DurableContext, repo: str, name: str, quota_retry: bool = False) -> dict:
    return microvm(ctx, name, job_input={"kind": "scan", "repo": repo}, config=config(quota_retry=quota_retry))


# ---------- demos ----------

def task(event: dict, ctx: DurableContext) -> Any:
    return scan_one(ctx, event.get("repo", "pallets/click"), "scan")


def fanout(event: dict, ctx: DurableContext) -> Any:
    repos = event["repos"]
    # The map's default completion fails fast: the first failed item ends the map, and
    # items still waiting on their MicroVMs are never resumed (nor their MicroVMs terminated).
    # all_completed() lets every item finish, so every MicroVM is terminated.
    map_config = MapConfig(max_concurrency=event.get("max_concurrency", 4))
    if event.get("all_completed"):
        map_config = MapConfig(max_concurrency=event.get("max_concurrency", 4),
                               completion_config=CompletionConfig.all_completed())
    result = ctx.map(
        repos,
        lambda item_ctx, repo, i, _all: scan_one(item_ctx, repo, f"scan-{i}", bool(event.get("quota_retry"))),
        name="scan-all",
        config=map_config,
    )
    return {
        "completion_reason": result.completion_reason.value,
        "results": result.get_results(),
        "errors": [e.message if hasattr(e, "message") else str(e) for e in result.get_errors() if e],
        "statuses": [item.status.value for item in result.all],
    }


def serial(event: dict, ctx: DurableContext) -> Any:
    return [scan_one(ctx, repo, f"scan-{i}") for i, repo in enumerate(event["repos"])]


def long(event: dict, ctx: DurableContext) -> Any:
    seconds = int(event["seconds"])
    return microvm(ctx, "long", job_input={"kind": "sleep", "seconds": seconds},
                   config=config(minutes=seconds / 60 + 10, heartbeat_seconds=120))


def session(event: dict, ctx: DurableContext) -> Any:
    repo = event.get("repo", "pallets/click")
    suspend = bool(event.get("suspend"))
    wait_s = int(event.get("wait_seconds", 120))

    def pipeline(vm: MicrovmSession, sctx: DurableContext) -> Any:
        built = vm.invoke(sctx, "build", job_input={"repo": repo}, options=MicrovmInvokeOptions(
            path="/build", timeout=Duration.from_minutes(10), heartbeat_timeout=Duration.from_seconds(60),
            # The function calls SuspendMicrovm when this job's result arrives
            suspend_after=suspend))
        # A durable wait: no Lambda invocation runs while it lasts
        sctx.wait(Duration.from_seconds(wait_s), name="cool-down")
        tested = vm.invoke(sctx, "test", job_input={"repo": repo}, options=MicrovmInvokeOptions(
            path="/test", timeout=Duration.from_minutes(10),
            # Covers the resume, the worker's refusal window and the request retries
            heartbeat_timeout=Duration.from_seconds(120)))
        return {"microvm_id": vm.microvm_id, "build": built, "test": tested}

    cfg = MicrovmSessionConfig(
        image_identifier=IMAGE, execution_role_arn=ROLE, timeout=Duration.from_minutes(30),
        # Needed to suspend: the service resumes the MicroVM on the next request
        idle_policy={"autoResumeEnabled": True, "maxIdleDurationSeconds": 7200,
                     "suspendedDurationSeconds": 1800} if suspend else None,
    )
    return microvm_session(ctx, "pipeline", config=cfg, handler=pipeline)


def fail(event: dict, ctx: DurableContext) -> Any:
    try:
        return microvm(ctx, "tests", job_input={"kind": "fail", "repo": "pallets/click"}, config=config())
    except MicrovmJobFailedError as e:
        return {"caught": type(e).__name__, "message": str(e)[:300]}


def crash(event: dict, ctx: DurableContext) -> Any:
    t0 = ctx.step(lambda _: time.time(), name="t0")
    try:
        return microvm(ctx, "crash", job_input={"kind": "crash", "after": 5}, config=config(heartbeat_seconds=30))
    except MicrovmError as e:
        t1 = ctx.step(lambda _: time.time(), name="t1")
        return {"caught": type(e).__name__, "message": str(e)[:300], "seconds_to_fail": round(t1 - t0, 1)}


def retry(event: dict, ctx: DurableContext) -> Any:
    """A failed job is not retried for you: retry it yourself, each attempt in a fresh MicroVM."""
    attempts = []
    for attempt in range(3):
        try:
            r = microvm(ctx, f"flaky-{attempt}", job_input={"kind": "flaky", "attempt": attempt}, config=config())
            attempts.append({"attempt": attempt, "ok": True, "microvm_id": r["microvm_id"]})
            break
        except MicrovmJobFailedError as e:
            attempts.append({"attempt": attempt, "ok": False, "error": str(e)[:120]})
    return attempts


def replay(event: dict, ctx: DurableContext) -> Any:
    """Crash the invocation once, after the MicroVM job finished, and let the durable function replay."""
    result = scan_one(ctx, event.get("repo", "pallets/click"), "scan")
    marker = f"replay/{event['run_id']}"
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=BUCKET, Key=marker)
        crashed_before = True
    except s3.exceptions.ClientError:
        crashed_before = False
    if not crashed_before:
        # A side effect outside a step, on purpose: it makes only the first pass crash
        s3.put_object(Bucket=BUCKET, Key=marker, Body=b"crashed once")
        print("replay demo: crashing this invocation after the MicroVM job finished")
        os._exit(1)
    return {"scan": result, "crashed_before": crashed_before}


def leak(event: dict, ctx: DurableContext) -> Any:
    # A 60-second timeout: the MicroVM may live 6 minutes in total
    return microvm(ctx, "scan", job_input={"kind": "scan", "repo": "pallets/click"},
                   config=config(minutes=1, heartbeat_seconds=60))


def ping(event: dict, ctx: DurableContext) -> Any:
    """No MicroVM: warms the function up before a timed run."""
    return {"pong": True}


DEMOS = {f.__name__: f for f in (ping, task, fanout, serial, long, session, fail, crash, retry, replay, leak)}


@durable_execution
def handler(event: dict, context: DurableContext) -> Any:
    if "Records" in event:  # SQS event source mapping: one message per batch
        event = json.loads(event["Records"][0]["body"])
    return DEMOS[event["demo"]](event, context)
