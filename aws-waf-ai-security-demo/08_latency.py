"""How much latency does AI Security add? Nobody publishes a number, so measure it.

Four configurations, same requests, same client, one keep-alive connection per front door:
  no WAF  ->  WAF with Bedrock calls off (Disable)  ->  request checks  ->  request + response checks

  python 08_latency.py [--n 100]

Sequential requests from one machine, so read the numbers as "what one client sees",
not as a load test. Leaves the web ACLs attached, in enforce mode with both phases on.
"""
import argparse
import http.client
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from common import BOLD, RESET, chat, client, head, load_state, mcp, mcp_call, say

KINDS = {
    "MCP tools/call": lambda: mcp_call("lookup_order", {"order_id": "A-1042"}),
    "MCP tools/list": lambda: mcp("tools/list"),
    "LLM chat": lambda: chat("What is your return policy for kettles?"),
}
CONFIGS = [("no WAF", None), ("WAF, Bedrock off", "disable"), ("request checks", "request"), ("request + response", "both")]


class Conn:
    def __init__(self, base):
        u = urlparse(base)
        self.u, self.prefix = u, u.path.rstrip("/")
        self.open()

    def open(self):
        cls = http.client.HTTPSConnection if self.u.scheme == "https" else http.client.HTTPConnection
        self.c = cls(self.u.netloc, timeout=30)

    def call(self, req):
        method, path, headers, body = req
        data = json.dumps(body).encode()
        h = {"Content-Type": "application/json", "User-Agent": "waf-ai-demo/1.0", **headers}
        for attempt in (1, 2):
            try:
                t0 = time.perf_counter()
                self.c.request(method, self.prefix + path, body=data, headers=h)
                r = self.c.getresponse()
                r.read()
                return r.status, (time.perf_counter() - t0) * 1000
            except (http.client.HTTPException, OSError):
                self.open()
        return 0, 0.0


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def set_config(waf, s, guardrails):
    resources = {"apigw": s["api_arn"], "alb": s["alb_arn"]}
    if guardrails is None:
        for arn in resources.values():
            waf.disassociate_web_acl(ResourceArn=arn)
    else:
        subprocess.run([sys.executable, "04_protect.py", "--mode", "enforce", "--guardrails", guardrails, "--settle", "0"],
                       check=True, stdout=subprocess.DEVNULL)
        for key, arn in resources.items():
            waf.associate_web_acl(WebACLArn=s["web_acls"][key], ResourceArn=arn)
    time.sleep(75)  # let the change reach the front doors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    a = ap.parse_args()
    s = load_state()
    waf = client("wafv2")
    doors = {"REST API": s["api_url"], "ALB": s["alb_url"]}
    results = []
    for name, guardrails in CONFIGS:
        head(f"Configuration: {name}")
        set_config(waf, s, guardrails)
        for door, base in doors.items():
            conn = Conn(base)
            for build in KINDS.values():  # warm up Lambda and the connection
                for _ in range(3):
                    conn.call(build())
            for kind, build in KINDS.items():
                times, bad = [], 0
                for _ in range(a.n):
                    code, ms = conn.call(build())
                    if code == 200:
                        times.append(ms)
                    else:
                        bad += 1
                row = {"config": name, "door": door, "kind": kind, "n": len(times), "not_200": bad,
                       "p50": round(statistics.median(times)), "p95": round(pct(times, 95)), "mean": round(statistics.mean(times))}
                results.append(row)
                say(f"  {door:<9}{kind:<16} n={row['n']:<4} p50 {row['p50']:>4} ms   p95 {row['p95']:>4} ms" + (f"   ({bad} not 200)" if bad else ""))

    head(f"Median latency in ms (p95 in brackets), {a.n} sequential requests per cell")
    for door in doors:
        say(f"\n  {BOLD}{door:<18}" + "".join(f"{c[0]:<22}" for c in CONFIGS) + RESET)
        for kind in KINDS:
            cells = ""
            for cname, _ in CONFIGS:
                r = [x for x in results if x["door"] == door and x["kind"] == kind and x["config"] == cname][0]
                cells += f"{str(r['p50']) + ' (' + str(r['p95']) + ')':<22}"
            say(f"  {kind:<18}{cells}")
    out = Path(__file__).with_name("results") / "latency.json"
    out.write_text(json.dumps(results, indent=1))
    say("\n  saved results/latency.json")


if __name__ == "__main__":
    main()
