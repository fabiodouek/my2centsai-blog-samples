"""Shared helpers: Terraform outputs, clients, invoking the durable function, reading its
execution history, watching MicroVMs, saving results, and redacting account-specific values."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from functools import cache
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent
RESULTS = HERE / "results"
PREFIX = "my2cents-ldm"
REGION = os.environ.get("AWS_REGION", "us-east-1")
IMAGE_NAME = f"{PREFIX}-worker"
FUNCTION = f"{PREFIX}-pipeline"
LEAKY_FUNCTION = f"{PREFIX}-leaky"
QUEUE_FUNCTION = f"{PREFIX}-queue"  # same code, a 30-minute execution timeout for SQS
ALIAS = "live"
BASE_IMAGE = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INTERNET_EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"

# Published us-east-1 rates (Graviton/ARM), from https://aws.amazon.com/lambda/pricing/
RATE_VCPU_S = 0.0000276944
RATE_GB_S = 0.0000036667
RATE_DURABLE_OP = 8.00 / 1_000_000
RATE_LAMBDA_ARM_GB_S = 0.0000133334
MICROVM_VCPU, MICROVM_GB = 1, 2  # the default size, which the image uses

_cfg = Config(region_name=REGION, retries={"max_attempts": 4, "mode": "standard"})
lam = boto3.client("lambda", config=_cfg)
mvm = boto3.client("lambda-microvms", config=_cfg)
logs = boto3.client("logs", config=_cfg)
s3 = boto3.client("s3", config=_cfg)
sqs = boto3.client("sqs", config=_cfg)


# ---------- Terraform outputs and redaction ----------

@cache
def tf() -> dict:
    out = subprocess.run(["terraform", f"-chdir={HERE / 'infra'}", "output", "-json"],
                         capture_output=True, text=True, check=True).stdout
    return {k: v["value"] for k, v in json.loads(out).items()}


@cache
def account_id() -> str:
    return boto3.client("sts", config=_cfg).get_caller_identity()["Account"]


def redact(text) -> str:
    """Hide the account ID and bucket name in anything printed (REDACT=0 shows them)."""
    s = text if isinstance(text, str) else json.dumps(text, indent=2, default=str)
    if os.environ.get("REDACT", "1") == "0":
        return s
    s = s.replace(account_id(), "<acct>")
    try:
        s = s.replace(tf()["bucket"], "<bucket>")
    except Exception:
        pass
    return s


def say(*parts) -> None:
    print(redact(" ".join(str(p) for p in parts)), flush=True)


def banner(title: str) -> None:
    say(f"\n=== {title} " + "=" * max(0, 64 - len(title)))


def image_arn() -> str:
    return f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{IMAGE_NAME}"


def save(name: str, data) -> Path:
    RESULTS.mkdir(exist_ok=True)
    p = RESULTS / f"{name}.json"
    p.write_text(redact(data))
    return p


def ts(t) -> float:
    return t.timestamp() if isinstance(t, datetime) else float(t)


def clock(t=None) -> str:
    t = datetime.now(timezone.utc) if t is None else t
    return t.astimezone(timezone.utc).strftime("%H:%M:%S")


# ---------- invoking and watching executions ----------

def start(demo: str, function: str = FUNCTION, name: str | None = None, **event) -> str:
    """Start a durable execution asynchronously (InvocationType=Event); return its ARN."""
    name = name or f"{demo}-{int(time.time())}"
    r = lam.invoke(FunctionName=function, Qualifier=ALIAS, InvocationType="Event",
                   DurableExecutionName=name, Payload=json.dumps({"demo": demo, **event}).encode())
    return r["DurableExecutionArn"]


def warm_up(function: str = FUNCTION) -> None:
    """Run the no-MicroVM 'ping' demo so the timed run doesn't include a cold start."""
    wait_done(start("ping", function=function), poll=0.5)


def execution(arn: str) -> dict:
    return lam.get_durable_execution(DurableExecutionArn=arn)


TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "STOPPED"}


def wait_done(arn: str, poll: float = 2, timeout: float = 7200, on_poll=None) -> dict:
    t0 = time.time()
    while True:
        d = execution(arn)
        if d["Status"] in TERMINAL:
            return d
        if on_poll:
            on_poll(d)
        if time.time() - t0 > timeout:
            raise TimeoutError(f"execution still {d['Status']} after {timeout}s")
        time.sleep(poll)


def result_of(d: dict):
    if d.get("Result"):
        return json.loads(d["Result"])
    return {"error": d.get("Error")}


def history(arn: str) -> list[dict]:
    events, marker = [], None
    while True:
        kw = {"DurableExecutionArn": arn, "IncludeExecutionData": True, "MaxItems": 1000}
        if marker:
            kw["Marker"] = marker
        r = lam.get_durable_execution_history(**kw)
        events += r["Events"]
        marker = r.get("NextMarker")
        if not marker:
            return events


def payload(ev: dict, key: str):
    """The JSON payload of a Result or Error inside an event's details, if any."""
    for v in ev.values():
        if isinstance(v, dict) and isinstance(v.get(key), dict) and "Payload" in v[key]:
            try:
                return json.loads(v[key]["Payload"])
            except (TypeError, ValueError):
                return v[key]["Payload"]
    return None


def summarize(events: list[dict]) -> dict:
    """Operations, invocations and per-operation timings from an execution history.

    Billed operations follow AWS's metering table: the execution start, each step attempt,
    each wait, each callback and each child context (map items included) count once;
    completing a callback through the API counts zero."""
    t0 = ts(events[0]["EventTimestamp"])
    ops: dict[str, dict] = {}
    invocations = []
    step_attempts = 0
    for ev in events:
        et, t = ev["EventType"], ts(ev["EventTimestamp"]) - t0
        if et == "InvocationCompleted":
            d = ev["InvocationCompletedDetails"]
            invocations.append({"start": round(ts(d["StartTimestamp"]) - t0, 2),
                                "seconds": round(ts(d["EndTimestamp"]) - ts(d["StartTimestamp"]), 3),
                                "error": bool(d.get("Error"))})
            continue
        if "Id" not in ev or et.startswith("Execution"):
            continue
        kind = et.replace("Started", "").replace("Succeeded", "").replace("Failed", "").replace(
            "TimedOut", "").replace("Cancelled", "")
        o = ops.setdefault(ev["Id"], {"name": ev.get("Name"), "type": kind, "sub_type": ev.get("SubType"),
                                      "parent": ev.get("ParentId")})
        if et.endswith("Started"):
            o.setdefault("start", round(t, 2))
            if et == "StepStarted":
                step_attempts += 1
        else:
            o["end"], o["outcome"] = round(t, 2), et.replace(kind, "")
            if et == "StepFailed":
                step_attempts += 0  # a failed attempt is followed by a new StepStarted, counted there
    for o in ops.values():
        if "start" in o and "end" in o:
            o["seconds"] = round(o["end"] - o["start"], 2)
    billed = 1 + sum(1 for o in ops.values() if o["type"] in ("Wait", "Callback", "Context")) + step_attempts
    lambda_seconds = sum(i["seconds"] for i in invocations)
    return {
        "operations": list(ops.values()),
        "billed_operations": billed,
        "invocations": invocations,
        "lambda_seconds": round(lambda_seconds, 3),
        "total_seconds": round(ts(events[-1]["EventTimestamp"]) - t0, 2),
    }


def print_timeline(summary: dict, indent: str = "  ") -> None:
    for o in summary["operations"]:
        if o["type"] == "Context" and o.get("sub_type") in (None, "RunInChildContext") and o["name"] is None:
            continue
        secs = f"{o.get('seconds', 0):7.2f}s" if "seconds" in o else "    ...  "
        say(f"{indent}{o.get('start', 0):7.2f}s  {secs}  {o['type']:<8} {o['name'] or ''}  {o.get('outcome', '')}")


def launched_microvms(events: list[dict]) -> list[str]:
    """MicroVM IDs from the launch steps' checkpointed results."""
    ids = []
    for ev in events:
        if ev["EventType"] == "StepSucceeded" and (ev.get("Name") or "").endswith(".launch"):
            r = payload(ev, "Result")
            if isinstance(r, dict) and r.get("microvmId"):
                ids.append(r["microvmId"])
    return ids


# ---------- MicroVMs ----------

def microvm(vm_id: str) -> dict:
    return mvm.get_microvm(microvmIdentifier=vm_id)


def lifetime(vm_id: str) -> dict:
    d = microvm(vm_id)
    end = d.get("terminatedAt")
    out = {"id": vm_id, "state": d["state"], "stateReason": d.get("stateReason")}
    if end:
        out["seconds"] = round(ts(end) - ts(d["startedAt"]), 1)
    return out


def list_microvms(states: tuple[str, ...] | None = None) -> list[dict]:
    items, token = [], None
    while True:
        kw = {"imageIdentifier": image_arn()}
        if token:
            kw["nextToken"] = token
        try:
            r = mvm.list_microvms(**kw)
        except mvm.exceptions.ResourceNotFoundException:
            return []
        items += r.get("items", [])
        token = r.get("nextToken")
        if not token:
            break
    return [i for i in items if not states or i.get("state") in states]


def microvm_cost(seconds: float) -> float:
    return seconds * (MICROVM_VCPU * RATE_VCPU_S + MICROVM_GB * RATE_GB_S)


def function_logs(function: str, since: float, pattern: str = "") -> list[str]:
    try:
        r = logs.filter_log_events(logGroupName=f"/aws/lambda/{function}", startTime=int(since * 1000),
                                   filterPattern=pattern, limit=200)
    except logs.exceptions.ResourceNotFoundException:
        return []
    return [e["message"].strip() for e in r.get("events", [])]
