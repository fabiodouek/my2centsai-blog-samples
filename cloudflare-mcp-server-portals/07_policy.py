"""Policies decide who gets in, and which servers they see. Two tokens, one portal, different views."""
import time

from common import *

st = load_state()
apps = st["apps"]


def set_policies(app_key: str, policy_ids: list[str]) -> None:
    app = cf_ok("GET", f"access/apps/{apps[app_key]}")
    app["policies"] = [{"id": p, "precedence": i + 1} for i, p in enumerate(policy_ids)]
    cf_ok("PUT", f"access/apps/{apps[app_key]}", {k: app[k] for k in ("name", "type", "domain", "destinations", "policies") if k in app})


def view(label: str, which: str, expect: set[str] | None = None) -> None:
    """Open a session and list the tools. Policy changes take a moment, so retry until the view matches `expect`."""
    for _ in range(15):
        m = Mcp(PORTAL_URL, svc_headers(st, which))
        s, ms, msg, h = m.initialize()
        names = [t["name"] for t in m.tools() if not t["name"].startswith("portal_")] if s == 200 else []
        servers = {n.split("_")[0] for n in names}
        if expect is None or servers == expect:
            break
        time.sleep(3)
    print(f"  {label:9} -> HTTP {s}" + (f", tools: {names}" if s == 200 else ", no session"))


if "policy_both" not in st:
    st["policy_both"] = cf_ok("POST", "access/policies", {"name": "portal-demo: both tokens", "decision": "non_identity",
        "include": [{"service_token": {"token_id": st["service_tokens"]["agent"]["id"]}},
                    {"service_token": {"token_id": st["service_tokens"]["intruder"]["id"]}}]})["id"]
    save_state(st)
pa, pb = st["policy_agent"], st["policy_both"]

print("1. the portal's policy allows only the agent token")
view("agent", "agent", {"notes", "tickets"})
view("intruder", "intruder")

print("\n2. portal and notes allow both tokens; tickets still allows only the agent")
set_policies("portal", [pb])
set_policies("notes", [pb])
view("agent", "agent", {"notes", "tickets"})
view("intruder", "intruder", {"notes"})

print("\n3. back to the original policies")
for k in ("portal", "notes"):
    set_policies(k, [pa])
time.sleep(5)
view("intruder", "intruder")
