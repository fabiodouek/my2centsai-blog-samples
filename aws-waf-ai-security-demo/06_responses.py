"""Response inspection: what AWS WAF finds on the way out, and where you can see it.

Response-phase labels are not in the WAF logs. The only place they show up is
CloudWatch label metrics (AllowedResponses / BlockedResponses / CountedResponses),
so this script sends the four response cases and then reads those metrics.

  python 06_responses.py            send the cases, wait, read the metrics
  python 06_responses.py --read     only read the metrics (last 3 hours)
"""
import sys
import time
from datetime import datetime, timedelta, timezone

from cases import CASES
from common import BOLD, GREEN, NS, PREFIX, RED, RESET, client, head, load_state, say, send

RESPONSE_CASES = ["mcp-poison", "a2a-poison", "mcp-pii", "llm-secret"]
METRICS = ["BlockedResponses", "CountedResponses", "AllowedResponses"]


def send_cases(rounds=3):
    s = load_state()
    head(f"Four requests that look harmless, {rounds} times each. The problem is in the response.")
    say(f"  {BOLD}{'case':<13}{'what the backend returns':<44}{'REST API':<12}{'ALB':<10}{RESET}")
    for cid, _, what, build, _ in [c for c in CASES if c[0] in RESPONSE_CASES]:
        cells = ""
        for base in (s["api_url"], s["alb_url"]):
            codes = [send(base, build())[0] for _ in range(rounds)]
            txt = "/".join(str(c) for c in codes)
            cells += f"{RED if 403 in codes else GREEN}{txt:<12}{RESET}"
        say(f"  {cid:<13}{what:<44}{cells}")


def read_metrics(hours=3):
    cw = client("cloudwatch")
    end = datetime.now(timezone.utc)
    rows, found = {}, set()
    for page in cw.get_paginator("list_metrics").paginate(Namespace="AWS/WAFV2"):
        for m in page["Metrics"]:
            d = {x["Name"]: x["Value"] for x in m["Dimensions"]}
            if m["MetricName"] not in METRICS or "LabelNamespace" not in d or not d.get("WebACL", "").startswith(PREFIX):
                continue
            label = (d["LabelNamespace"] + ":" + d["LabelName"]).replace(NS, "")
            if label.startswith("guardrails:sensitive_information:"):
                found.add(label.split(":")[2])  # the kind of data, e.g. aws_access_key
            if not label.startswith("response:"):
                continue  # keep the per-direction labels, skip the per-category ones
            pts = cw.get_metric_statistics(Namespace="AWS/WAFV2", MetricName=m["MetricName"], Dimensions=m["Dimensions"],
                                           StartTime=end - timedelta(hours=hours), EndTime=end, Period=3600 * hours, Statistics=["Sum"])["Datapoints"]
            total = int(sum(p["Sum"] for p in pts))
            if total:
                rows[(d["WebACL"], label, m["MetricName"])] = total
    head(f"CloudWatch label metrics for responses, last {hours} hours")
    say(f"  {BOLD}{'web ACL':<20}{'response label':<58}{'metric':<18}{'count'}{RESET}")
    for (acl, label, metric), total in sorted(rows.items()):
        colour = RED if metric == "BlockedResponses" else ""
        say(f"  {acl:<20}{label:<58}{colour}{metric:<18}{total}{RESET}")
    if found:
        say(f"\n  sensitive information found in responses: {', '.join(sorted(found))}")
    if not rows:
        say("  (no response label metrics yet: they take a few minutes to arrive)")
    return rows


if __name__ == "__main__":
    if "--read" not in sys.argv:
        send_cases()
        say("\n  waiting 4 minutes for CloudWatch ...")
        time.sleep(240)
    read_metrics()
