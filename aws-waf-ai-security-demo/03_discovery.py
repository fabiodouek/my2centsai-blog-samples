"""API Portal discovery: turn it on, send client traffic, and watch the inventory.

  python 03_discovery.py enable     turn on discovery with AI discovery (billable: see README)
  python 03_discovery.py traffic    send well-formed MCP, A2A and LLM traffic to both front doors
  python 03_discovery.py status     print the inventory and how long each API took to appear
  python 03_discovery.py watch      traffic + status every 10 minutes until every workload is labeled
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from common import (BOLD, DIM, GREEN, RESET, YELLOW, a2a_send, chat, client, head, http, load_state, mcp, mcp_call,
                    mcp_init, save_state, say, send)

TIMELINE = Path(__file__).with_name("results") / "discovery-timeline.json"
EXPECT = {"/mcp": "MCP", "/a2a": "A2A", "/a2a/message:send": "A2A", "/.well-known/agent-card.json": "A2A",
          "/v1/chat/completions": "LLM"}


def enable():
    ap = client("apiportal")
    head("Enable API Portal discovery (this account, this Region) with AI discovery")
    cfg = ap.put_discovery_configuration(discoverySources=["API_GATEWAY", "ELB"], features=["AI_DISCOVERY"],
                                         tags={"project": "waf-ai-security"})["discoveryConfiguration"]
    say(f"  status {cfg['status']}  sources {cfg['discoverySources']}  features {cfg['features']}")
    if not load_state().get("discovery_enabled"):
        save_state(discovery_enabled=int(time.time()))
    arn = cfg["arn"]
    started = False
    for i in range(40):
        st = ap.list_discovery_statuses()["items"]
        line = "  ".join(f"{s['discoverySource']}={s['status']}" for s in st) or "(no status yet)"
        say(f"  {datetime.now().strftime('%H:%M:%S')}  {line}")
        if st and all(s["status"] == "RUNNING" for s in st):
            break
        if i >= 3 and not started and st and all(s["status"] == "STOPPED" for s in st):
            # In my run the configuration said ENABLED but every source stayed STOPPED
            # for ten minutes, until this call. The getting-started page does not mention it.
            say("  still STOPPED: calling start-discovery with the configuration ARN")
            ap.start_discovery(id=arn)
            save_state(discovery_started=int(time.time()))
            started = True
        time.sleep(15)


def traffic(rounds=3):
    s = load_state()
    head("Client traffic: what a normal MCP client, A2A client and OpenAI SDK would send")
    n = 0
    for base in (s["api_url"], s["alb_url"]):
        for _ in range(rounds):
            reqs = [mcp_init(), mcp("tools/list"), mcp_call("lookup_order", {"order_id": "A-1042"}),
                    mcp_call("search_kb", {"query": "return policy"}),
                    a2a_send("How long does a refund take?"), a2a_send("How long does a refund take?", rest=True),
                    chat("Say hello.")]
            for r in reqs:
                send(base, r)
                n += 1
            http("GET", base + "/.well-known/agent-card.json", headers={"A2A-Version": "1.0"})
            n += 1
    say(f"  sent {n} requests to the REST API and the ALB")


def fmt_age(seconds):
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"


def expected(path):
    for suffix, workload in EXPECT.items():
        if path.endswith(suffix):
            return workload
    return None


def status():
    """Print the inventory. Also keeps a timeline (results/discovery-timeline.json) of when
    each API first showed up in my polls and when it first carried a workload label."""
    s = load_state()
    ap = client("apiportal")
    items, kw = [], {}
    while True:
        page = ap.list_api_inventory_items(**kw)
        items += page["items"]
        if not page.get("nextToken"):
            break
        kw["nextToken"] = page["nextToken"]
    t0 = s.get("discovery_started") or s.get("discovery_enabled") or time.time()
    now = time.time()
    tl = json.loads(TIMELINE.read_text()) if TIMELINE.exists() else {}
    head(f"API inventory, {fmt_age(now - t0)} after discovery started: {len(items)} APIs")
    say(f"  {BOLD}{'source':<13}{'method':<7}{'path':<36}{'class':<8}{'WAF':<9}{'workloads':<10}{'listed':<9}labeled{RESET}")
    done = True
    for it in sorted(items, key=lambda i: (i["discoverySource"], i["path"])):
        key = f"{it['discoverySource']} {it['method']} {it['path']}"
        wl = ",".join(it.get("workloads") or [])
        rec = tl.setdefault(key, {"listed": int(now - t0)})
        if wl and "labeled" not in rec:
            rec["labeled"] = int(now - t0)
            rec["workloads"] = wl
        want = expected(it["path"])
        ok = want is None or want in wl
        done &= ok
        colour = GREEN if (want and ok) else (YELLOW if want else DIM)
        labeled = "+" + fmt_age(rec["labeled"]) if "labeled" in rec else "-"
        say(f"  {it['discoverySource']:<13}{it['method']:<7}{it['path']:<36}{it['classification']:<8}{it['wafStatus']:<9}"
            f"{colour}{wl or '-':<10}{RESET}{'+' + fmt_age(rec['listed']):<9}{labeled}")
    TIMELINE.parent.mkdir(exist_ok=True)
    TIMELINE.write_text(json.dumps(tl, indent=1))
    sources = {i["discoverySource"] for i in items}
    return items, bool(items) and done and {"API_GATEWAY", "ELB"} <= sources


def watch():
    while True:
        traffic(rounds=1)
        say(f"\n{DIM}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
        _, done = status()
        if done:
            say("\n  every expected endpoint is labeled")
            return
        time.sleep(600)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"enable": enable, "traffic": traffic, "status": status, "watch": watch}[cmd]()
