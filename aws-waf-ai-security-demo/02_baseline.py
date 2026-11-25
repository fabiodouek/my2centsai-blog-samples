"""Baseline: send the whole request matrix to both front doors with no WAF in front.

Every request, hostile or not, should come back 200. That is the problem.
"""
from cases import CASES
from common import BOLD, GREEN, RED, RESET, head, load_state, say, send


def main():
    s = load_state()
    head(f"No WAF yet: {len(CASES)} requests to each front door, most of them hostile or malformed")
    say(f"  {BOLD}{'case':<14}{'proto':<6}{'what it is':<48}{'REST API':<10}{'ALB':<6}{RESET}")
    passed = 0
    for cid, proto, what, build, _ in CASES:
        codes = [send(base, build())[0] for base in (s["api_url"], s["alb_url"])]
        passed += sum(c == 200 for c in codes)
        cells = "".join(f"{GREEN if c == 200 else RED}{c:<10}{RESET}" for c in codes)
        say(f"  {cid:<14}{proto:<6}{what:<48}{cells}")
    say(f"\n  {passed} of {2 * len(CASES)} requests answered 200. Nothing looked at them on the way in or on the way out.")


if __name__ == "__main__":
    main()
