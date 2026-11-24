"""Send the request matrix through AWS WAF and show what the AI Security rule group did with each request.

  python 05_attacks.py                 both front doors
  python 05_attacks.py --door alb      one front door (apigw or alb)
  python 05_attacks.py --show-block    also print the full response of the first blocked request

Status codes come from the client. Labels and the terminating rule come from the WAF logs
(request phase only: AWS WAF does not log response-phase labels).
"""
import argparse
import json
import time
from pathlib import Path

from cases import CASES
from common import (BOLD, DIM, GREEN, RED, RESET, YELLOW, head, labels_of, load_state, new_tag, say, send, verdict,
                    waf_log_records)

KEEP = ("traffic:", "protocol_compliance_check:", "request:guardrails:", "guardrails:not_assessed", "guardrails:call_failed")


def short(labels):
    """The labels that matter, shortened: the recognized protocol first, then checks and findings."""
    out = []
    for l in labels:
        if l.startswith(KEEP):
            out.append(l.replace("traffic:recognized_", "").replace("traffic:", "").replace("protocol_compliance_check:", "")
                       .replace("request:guardrails:", "").replace("guardrails:", "").replace(":severity:", "=")
                       .replace(":confidence:", "="))
    out.sort(key=lambda x: x not in ("mcp", "a2a", "llm", "not_recognized"))
    return " ".join(out) or "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--door", choices=["apigw", "alb", "both"], default="both")
    ap.add_argument("--show-block", action="store_true")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--out", help="name for results/<name>.json")
    a = ap.parse_args()
    s = load_state()
    doors = {"apigw": s["api_url"], "alb": s["alb_url"]}
    if a.door != "both":
        doors = {a.door: doors[a.door]}
    cases = [c for c in CASES if not a.only or c[0] in a.only.split(",")]

    start = int(time.time() * 1000) - 5000
    rows, first_block = [], None
    for cid, proto, what, build, expect in cases:
        for door, base in doors.items():
            tag = new_tag()
            code, body, secs = send(base, build(), tag)
            rows.append({"case": cid, "proto": proto, "what": what, "expect": expect, "door": door, "tag": tag,
                         "status": code, "ms": round(secs * 1000)})
            if code == 403 and first_block is None:
                first_block = (cid, door, body)
    head(f"Sent {len(rows)} requests (mode={s.get('mode')}, guardrails={s.get('guardrails')}). Reading the WAF logs ...")
    recs = waf_log_records(s["log_group"], [r["tag"] for r in rows], wait=240, start_ms=start)
    for r in rows:
        rec = recs.get(r["tag"])
        r["labels"] = labels_of(rec) if rec else None
        r["action"], r["rule"] = verdict(rec) if rec else ("no log", "")

    for door in doors:
        head(f"Front door: {'API Gateway REST API' if door == 'apigw' else 'Application Load Balancer'}")
        say(f"  {BOLD}{'case':<14}{'HTTP':<6}{'WAF log':<9}{'labels':<44}{'blocking rule'}{RESET}")
        for r in [r for r in rows if r["door"] == door]:
            colour = RED if r["status"] == 403 else GREEN
            labels = short(r["labels"]) if r["labels"] is not None else "(not in the log)"
            say(f"  {r['case']:<14}{colour}{r['status']:<6}{RESET}{r['action']:<9}{labels:<44}{YELLOW}{r['rule']}{RESET}")
        blocked = sum(r["status"] == 403 for r in rows if r["door"] == door)
        say(f"  {blocked} of {len(cases)} blocked")

    if a.show_block and first_block:
        cid, door, body = first_block
        head(f"What a block looks like ({cid} on {door})")
        say("  HTTP 403\n  " + body.strip().replace("\n", "\n  ")[:600])
    if a.out:
        out = Path(__file__).with_name("results") / f"{a.out}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps([{k: v for k, v in r.items() if k != "tag"} for r in rows], indent=1))
        say(f"\n  saved results/{a.out}.json")


if __name__ == "__main__":
    main()
