"""Find the sandbox in CloudTrail: the session name is the sandbox ID.

  uv run python 05_cloudtrail.py                 # make a sandbox, call AWS, wait for the events
  uv run python 05_cloudtrail.py --session ID    # look up a sandbox ID from an earlier run (no sandbox)
  uv run python 05_cloudtrail.py --minutes 20    # how long to wait for events (default 15)

The broker assumes osb-sandbox with RoleSessionName = the sandbox ID, so every call the sandbox makes is
recorded under userIdentity.arn = arn:aws:sts::<account>:assumed-role/osb-sandbox/<sandbox-id>. This script:

  1. makes a sandbox and calls STS (GetCallerIdentity) and Bedrock (Converse) from it;
  2. looks the session name up in CloudTrail event history (free, the last 90 days of management events) with
     the `default` AWS profile, polling until the events appear (usually 2 to 10 minutes);
  3. looks up the AssumeRole event too: its requestParameters.roleSessionName is the sandbox ID, and its
     caller is the broker's role session.

S3 object operations (GetObject, PutObject) are DATA events: they are not in event history. To see them, create a
trail with S3 data events for the bucket (optional, billed per event) and look the same session name up there.

It prints no source IP address. The sandbox ID is written to logs/last_session.txt for console/capture.sh.
"""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import boto3

from common import (
    LOGS, MODEL_ID, Checks, account_id, cleanup, ensure_broker_forward, ensure_port_forward, env_sh, heading, new_sandbox,
    note, run, scrub,
)

SCRIPT = "05_cloudtrail"
parser = argparse.ArgumentParser()
parser.add_argument("--session", help="a sandbox ID from an earlier run: skip the sandbox, just look it up")
parser.add_argument("--minutes", type=float, default=15, help="how long to wait for CloudTrail (default 15)")
args = parser.parse_args()

checks = Checks()
started = datetime.now(timezone.utc) - (timedelta(days=2) if args.session else timedelta(minutes=5))


def rows(client, **lookup) -> list[dict]:
    events = []
    for page in client.get_paginator("lookup_events").paginate(StartTime=started, **lookup):
        for event in page["Events"]:
            events.append(json.loads(event["CloudTrailEvent"]))
    return events


def describe(event: dict) -> str:
    who = event.get("userIdentity", {})
    issuer = who.get("sessionContext", {}).get("sessionIssuer", {}).get("userName", "?")
    session = who.get("arn", "").rsplit("/", 1)[-1]
    return f"{event['eventTime']}  {event['eventSource'].split('.')[0]:<8} {event['eventName']:<18} role {issuer:<12} session {session}"


sandbox_id = args.session
try:
    if not sandbox_id:
        ensure_port_forward()
        ensure_broker_forward()
        heading("1. a sandbox calls STS and Bedrock")
        sb, view = new_sandbox(script=SCRIPT)
        sandbox_id = view["sandbox_id"]
        run(sb, "aws sts get-caller-identity --query Arn --output text")
        prompt = '[{"role":"user","content":[{"text":"Reply with exactly one word: ready"}]}]'
        run(sb, f"aws bedrock-runtime converse --model-id {MODEL_ID} --messages '{prompt}' --inference-config '{{\"maxTokens\":20}}' "
                "--query output.message.content[0].text --output text", label=f"aws bedrock-runtime converse --model-id {MODEL_ID} --messages ...")
        LOGS.mkdir(exist_ok=True)
        (LOGS / "last_session.txt").write_text(sandbox_id + "\n")
    else:
        heading("1. looking up an earlier sandbox")
    print(f"  session name = sandbox id = {sandbox_id}")

    heading("2. CloudTrail event history (the default AWS profile)")
    client = boto3.Session(profile_name="default", region_name=env_sh("AWS_REGION")).client("cloudtrail")
    deadline = time.monotonic() + args.minutes * 60
    found: dict[str, dict] = {}
    assume: dict | None = None
    while True:
        for event in rows(client, LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": sandbox_id}]):
            found.setdefault(event["eventName"], event)
        if assume is None:
            assume = next((e for e in rows(client, LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "AssumeRole"}])
                           if (e.get("requestParameters") or {}).get("roleSessionName") == sandbox_id), None)
        if ("GetCallerIdentity" in found and "Converse" in found and assume) or time.monotonic() >= deadline:
            break
        waited = int(args.minutes * 60 - (deadline - time.monotonic()))
        print(f"  waiting for CloudTrail ... {waited} s (have: {', '.join(sorted(found)) or 'nothing'}{', AssumeRole' if assume else ''})", flush=True)
        time.sleep(30)

    heading("3. the events")
    for name in ("GetCallerIdentity", "Converse"):
        if name in found:
            print("  " + scrub(describe(found[name])))
    if assume:
        print("  " + scrub(describe(assume)))
        params = assume.get("requestParameters", {})
        print(f"      AssumeRole: roleSessionName = {params.get('roleSessionName')}, durationSeconds = {params.get('durationSeconds')}, "
              f"caller = {scrub(assume.get('userIdentity', {}).get('arn', '')).split(':')[-1]}")
    expected_arn_tail = f"assumed-role/osb-sandbox/{sandbox_id}"
    gci = found.get("GetCallerIdentity")
    checks.check("STS GetCallerIdentity is in event history under the sandbox's session name", bool(gci) and gci["userIdentity"]["arn"].endswith(expected_arn_tail))
    checks.check("the AssumeRole event names the sandbox ID as roleSessionName", assume is not None)
    checks.check("the AssumeRole was made by the broker's role session",
                 assume is not None and "assumed-role/osb-broker/" in assume.get("userIdentity", {}).get("arn", ""))
    checks.check("Bedrock Converse is in event history under the same session name",
                 "Converse" in found and found["Converse"]["userIdentity"]["arn"].endswith(expected_arn_tail))
    if account_id():
        checks.check("the events are in this account", bool(gci) and gci.get("recipientAccountId") == account_id())
    note("S3 PutObject/GetObject are data events: not in event history. A trail with S3 data events for the bucket would record them under the same session name")
finally:
    if not args.session:
        cleanup()
checks.finish()
