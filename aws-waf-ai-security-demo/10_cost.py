"""What does a checked message cost? Ask Amazon Bedrock directly.

AWS WAF calls bedrock:InvokeGuardrailChecks for you and does not show the usage,
so this script makes the same call with text of different sizes and reads
`usage.textUnits` from the response. Then it prices one million messages.

Prices are the public us-east-1 rates on 2026-11-24 (check the pricing pages before relying on them).
"""
import json
import statistics
import time
from pathlib import Path

from common import BOLD, RESET, client, head, say

PRICE = {"contentFilter": 0.07, "promptAttack": 0.08, "sensitiveInformation": 0.10}  # USD per 1,000 text units
WAF_PER_M = 0.60 + 5.00       # base request fee + premium fee, per 1M requests (pricing from 2027-02-01)
APPLY_GUARDRAIL = {"contentFilter": 0.15, "sensitiveInformation": 0.10}  # standard guardrail, per 1,000 text units

# The categories AWS WAF asks for, per its documentation.
CHECKS = {
    "contentFilter": {"categories": [{"category": c} for c in ["VIOLENCE", "HATE", "SEXUAL", "MISCONDUCT", "INSULTS"]]},
    "promptAttack": {"categories": [{"category": c} for c in ["JAILBREAK", "PROMPT_INJECTION", "PROMPT_LEAKAGE"]]},
    "sensitiveInformation": {"entities": [{"type": t} for t in [
        "AWS_ACCESS_KEY", "AWS_SECRET_KEY", "PASSWORD", "EMAIL", "CREDIT_DEBIT_CARD_NUMBER", "US_SOCIAL_SECURITY_NUMBER",
        "INTERNATIONAL_BANK_ACCOUNT_NUMBER", "US_PASSPORT_NUMBER", "US_BANK_ACCOUNT_NUMBER"]]},
}
# Which checks AWS WAF runs per message kind (from the developer guide's table).
KINDS = {
    "MCP tools/call, request": ["promptAttack"],
    "MCP tools/call, response": ["promptAttack", "sensitiveInformation"],
    "LLM chat, request": ["promptAttack", "contentFilter"],
    "LLM chat, response": ["sensitiveInformation"],
}
FILLER = "The customer asked about the return policy for the blue kettle and whether the lid is covered. "


def text_of(chars):
    return (FILLER * (chars // len(FILLER) + 1))[:chars]


def main():
    br = client("bedrock-runtime")
    sizes = [100, 1000, 2000, 8000]
    units = {}
    head("InvokeGuardrailChecks: text units billed per check, by message size")
    say(f"  {BOLD}{'characters':<12}{'contentFilter':<16}{'promptAttack':<15}{'sensitiveInformation':<22}{'call time (median of 5)'}{RESET}")
    for n in sizes:
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            r = br.invoke_guardrail_checks(
                messages=[{"role": "user", "content": [{"text": text_of(n)}]}], checks=CHECKS)
            times.append((time.perf_counter() - t0) * 1000)
        units[n] = {k: v["textUnits"] for k, v in r["usage"].items()}
        u = units[n]
        say(f"  {n:<12}{u['contentFilter']:<16}{u['promptAttack']:<15}{u['sensitiveInformation']:<22}{round(statistics.median(times))} ms")

    head("Cost of one million messages (USD): Amazon Bedrock checks, and the AWS WAF fee")
    say(f"  {BOLD}{'message kind':<28}" + "".join(f"{str(n) + ' chars':<14}" for n in sizes) + RESET)
    table = {}
    for kind, checks in KINDS.items():
        cells = ""
        for n in sizes:
            bedrock = sum(units[n][c] * PRICE[c] / 1000 for c in checks) * 1_000_000
            table[f"{kind}|{n}"] = round(bedrock, 2)
            cells += f"{'$' + format(bedrock, ',.0f'):<14}"
        say(f"  {kind:<28}{cells}")
    say(f"  {'AWS WAF fee (any size)':<28}${WAF_PER_M:.2f} per 1M requests, charged once per request")

    n = 2000
    both = table[f"MCP tools/call, request|{n}"] + table[f"MCP tools/call, response|{n}"]
    head(f"Example: 1M MCP tool calls, {n} characters each way, request and response checked")
    say(f"  Amazon Bedrock checks   ${both:,.0f}")
    say(f"  AWS WAF                 ${WAF_PER_M:.2f}")
    app = (units[n]["contentFilter"] * APPLY_GUARDRAIL["contentFilter"] * 2 + units[n]["sensitiveInformation"] * APPLY_GUARDRAIL["sensitiveInformation"]) / 1000 * 1_000_000
    say(f"  For comparison, ApplyGuardrail in your own code (content filter with prompt attack on both sides,")
    say(f"  sensitive information on the response): ${app:,.0f}")
    out = Path(__file__).with_name("results") / "cost.json"
    out.write_text(json.dumps({"text_units": units, "bedrock_usd_per_1m": table, "waf_usd_per_1m": WAF_PER_M}, indent=1))
    say("\n  saved results/cost.json")


if __name__ == "__main__":
    main()
