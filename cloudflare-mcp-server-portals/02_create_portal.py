"""Create everything a portal needs through the API, and time it.

The dashboard does several of these steps for you. The API does not: it creates the servers and the portal,
but not the DNS record, the Access applications or the policy that lets an agent in.
"""
import time

import httpx

from common import *

T0 = time.time()


def step(msg: str) -> None:
    print(f"[t+{time.time() - T0:5.1f}s] {msg}", flush=True)


state = load_state()
state.setdefault("service_tokens", {})
state.setdefault("apps", {})

# 1. Two upstream servers, registered by URL. Cloudflare connects and lists their tools.
for sid, name, url in (("notes", "Notes", NOTES_URL), ("tickets", "Tickets", TICKETS_URL)):
    status, data = cf("POST", "access/ai-controls/mcp/servers",
                      {"id": sid, "name": name, "hostname": url, "auth_type": "unauthenticated"})
    step(f"server {sid}: HTTP {status}" + ("" if status < 300 else f" {data.get('errors')}"))
for sid in ("notes", "tickets"):
    for _ in range(30):
        srv = cf_ok("GET", f"access/ai-controls/mcp/servers/{sid}")
        if srv["status"] != "waiting":
            break
        time.sleep(2)
    step(f"server {sid}: status={srv['status']}, {len(srv.get('tools') or [])} tools discovered")

# 2. Service tokens: one for the agent, one that the policy will not allow.
for key, name in (("agent", "portal-demo-agent"), ("intruder", "portal-demo-intruder")):
    if key not in state["service_tokens"]:
        r = cf_ok("POST", "access/service_tokens", {"name": name, "duration": "8760h"})
        state["service_tokens"][key] = {"id": r["id"], "client_id": r["client_id"], "client_secret": r["client_secret"]}
save_state(state)
step("service tokens: agent and intruder")

# 3. One Access policy: only the agent token (decision non_identity = Service Auth).
if "policy_agent" not in state:
    r = cf_ok("POST", "access/policies", {"name": "portal-demo: allow agent token", "decision": "non_identity",
                                          "include": [{"service_token": {"token_id": state["service_tokens"]["agent"]["id"]}}]})
    state["policy_agent"] = r["id"]
    save_state(state)
step("policy: Service Auth, agent token only")

# 4. The portal itself, then the DNS record the API leaves to you (proxied CNAME to gateway.agents.cloudflare.com).
status, data = cf("POST", "access/ai-controls/mcp/portals",
                  {"id": PORTAL_ID, "name": "Demo portal", "hostname": PORTAL_HOST,
                   "servers": [{"server_id": "notes", "on_behalf": False}, {"server_id": "tickets", "on_behalf": False}]})
step(f"portal {PORTAL_ID}: HTTP {status}, hostname {PORTAL_HOST}")
zone = cf_ok("GET", f"{API}/zones", params={"name": DOMAIN})[0]["id"]
status, data = cf("POST", f"{API}/zones/{zone}/dns_records",
                  {"type": "CNAME", "name": "mcp-portal-demo", "content": "gateway.agents.cloudflare.com", "proxied": True, "ttl": 1})
step(f"DNS CNAME to gateway.agents.cloudflare.com: HTTP {status}")

# 5. Access applications: one per server (type mcp) and one for the portal (type mcp_portal), all with the same policy.
pol = [{"id": state["policy_agent"], "precedence": 1}]
for sid in ("notes", "tickets"):
    if sid not in state["apps"]:
        r = cf_ok("POST", "access/apps", {"name": f"portal-demo {sid} server", "type": "mcp", "policies": pol,
                                          "destinations": [{"type": "via_mcp_server_portal", "mcp_server_id": sid}]})
        state["apps"][sid] = r["id"]
if "portal" not in state["apps"]:
    r = cf_ok("POST", "access/apps", {"name": "portal-demo portal", "type": "mcp_portal", "domain": PORTAL_HOST, "policies": pol,
                                      "destinations": [{"type": "public", "uri": PORTAL_HOST}]})
    state["apps"]["portal"] = r["id"]
save_state(state)
step("Access apps: 2 servers + 1 portal")

# 6. Wait until the portal answers. Right after creation the edge says 403 (error 1014) for a while.
probe = {"Accept": "application/json, text/event-stream"}
first = {}
while True:
    try:
        code = httpx.get(PORTAL_URL, headers=probe, timeout=30).status_code
    except httpx.ConnectError:  # the name does not resolve yet (a resolver may cache the earlier NXDOMAIN)
        code = "no-dns"
    first.setdefault(code, round(time.time() - T0, 1))
    if code == 401:
        break
    if time.time() - T0 > 600:
        raise SystemExit(f"portal still answers {code}")
    time.sleep(5)
step(f"portal answers 401 (statuses seen: {first})")
save_result("02_setup.json", {"seconds_to_portal_401": round(time.time() - T0, 1), "first_seen_at_s": first})
