"""What happens when AWS WAF cannot reach Amazon Bedrock?

1. Break the IAM role (it allows bedrock:ApplyGuardrail, the wrong action). WAF accepts it.
2. Send a jailbreak and a normal request: by default both pass (fail-open).
3. Override Guardrails_CallFailed to Block: now everything that needs a check is refused (fail-closed).
4. Restore the role and the default actions.
"""
import subprocess
import sys
import time

from cases import CASES
from common import BOLD, GREEN, RED, RESET, YELLOW, head, labels_of, load_state, new_tag, say, send, verdict, waf_log_records

PICK = ["llm-ok", "llm-inject", "mcp-ok", "mcp-inject", "mcp-list"]


def protect(*args):
    subprocess.run([sys.executable, "04_protect.py", *args], check=True)


def run(title):
    s = load_state()
    head(title)
    start = int(time.time() * 1000) - 5000
    rows = []
    for cid, _, what, build, _ in [c for c in CASES if c[0] in PICK]:
        tag = new_tag()
        code = send(s["alb_url"], build(), tag)[0]
        rows.append((cid, what, code, tag))
    recs = waf_log_records(s["log_group"], [r[3] for r in rows], wait=240, start_ms=start)
    say(f"  {BOLD}{'case':<12}{'what it is':<38}{'HTTP':<6}rule / labels{RESET}")
    for cid, what, code, tag in rows:
        rec = recs.get(tag)
        labels = [l for l in labels_of(rec) if l.startswith(("guardrails:call_failed", "request:guardrails", "guardrails:not_assessed"))] if rec else ["(not in the log)"]
        rule = verdict(rec)[1] if rec else ""
        say(f"  {cid:<12}{what:<38}{RED if code == 403 else GREEN}{code:<6}{RESET}{YELLOW}{rule}{RESET} {' '.join(labels)}")


def wait_until(want_failed):
    """Send a probe until the Bedrock call fails (or works again). IAM changes take a while to bite."""
    s = load_state()
    build = [c for c in CASES if c[0] == "llm-ok"][0][3]
    for _ in range(40):
        tag = new_tag()
        start = int(time.time() * 1000) - 5000
        send(s["alb_url"], build(), tag)
        rec = waf_log_records(s["log_group"], [tag], wait=90, start_ms=start).get(tag)
        failed = bool(rec) and any("call_failed" in l for l in labels_of(rec))
        if rec and failed == want_failed:
            return
        time.sleep(15)
    raise SystemExit("the role change did not take effect in time")


def main():
    t0 = time.time()
    protect("--break-role")
    say("  waiting for AWS WAF to notice (it keeps the role's session for a while) ...")
    wait_until(True)
    say(f"  Bedrock calls started failing {int(time.time() - t0)}s after the policy change")
    run("Default actions, broken role: does the jailbreak get through?")
    protect("--mode", "enforce", "--fail-closed")
    run("Guardrails_CallFailed overridden to Block, broken role")
    protect("--fix-role")
    protect("--mode", "enforce", "--settle", "0")
    wait_until(False)
    run("Role fixed, default actions again")


if __name__ == "__main__":
    main()
