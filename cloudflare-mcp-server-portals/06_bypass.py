"""Hiding a tool in the portal is not enforcement: the upstream server is still reachable by its own URL.
Then close that door with a self-hosted Access application in front of the Worker."""
import json
import os
import subprocess
import time

import httpx

from common import *

st = load_state()
ORIGIN_HOST = f"tickets.{DOMAIN}"
ORIGIN_URL = f"https://{ORIGIN_HOST}/mcp"
result = {}


def listed_through_portal(tool: str) -> bool:
    agent = Mcp(PORTAL_URL, svc_headers(st))
    agent.initialize()
    return tool in [t["name"] for t in agent.tools()]


# The portal hides delete_ticket from agents. Wait until a fresh session no longer lists it.
cf_ok("PUT", f"access/ai-controls/mcp/portals/{PORTAL_ID}",
      {"servers": [{"server_id": "notes", "on_behalf": False}, {"server_id": "tickets", "on_behalf": False, "updated_tools": [{"name": "delete_ticket", "enabled": False}]}]})
t0 = time.time()
while listed_through_portal("tickets_delete_ticket") and time.time() - t0 < 90:
    time.sleep(2)
print(f"through the portal, delete_ticket is hidden (took {time.time() - t0:.0f}s to apply)")
result["hide_applied_after_s"] = round(time.time() - t0)

print("\n--- the bypass")
raw = Mcp(TICKETS_URL)  # the Worker's own URL, no credentials at all
s, ms, msg, _h = raw.call("delete_ticket", {"id": 2})
text = msg["result"]["content"][0]["text"]
print(f"direct call to the Worker's own URL, no credentials: HTTP {s}, {text}")
result["direct_delete"] = {"http": s, "text": text}

print("\n--- the fix: Access in front of the Worker, the portal holds the credential")
if not st["service_tokens"].get("origin"):
    r = cf_ok("POST", "access/service_tokens", {"name": "portal-demo-origin", "duration": "8760h"})
    st["service_tokens"]["origin"] = {"id": r["id"], "client_id": r["client_id"], "client_secret": r["client_secret"]}
origin = st["service_tokens"]["origin"]
if "policy_origin" not in st:
    st["policy_origin"] = cf_ok("POST", "access/policies", {"name": "portal-demo: origin token", "decision": "non_identity",
                                "include": [{"service_token": {"token_id": origin["id"]}}]})["id"]
if "origin_app" not in st["apps"]:
    st["apps"]["origin_app"] = cf_ok("POST", "access/apps", {"name": "portal-demo tickets origin", "type": "self_hosted",
                                     "domain": ORIGIN_HOST, "policies": [{"id": st["policy_origin"], "precedence": 1}]})["id"]
save_state(st)
print("self-hosted Access app on", ORIGIN_HOST, "with a Service Auth policy for the origin token")

# Same Worker code, now on a custom domain only (workers.dev switched off).
cfg = {"name": "tickets-mcp", "main": "src/index.ts", "compatibility_date": "2026-07-02", "compatibility_flags": ["nodejs_compat"],
       "workers_dev": False, "preview_urls": False, "routes": [{"pattern": ORIGIN_HOST, "custom_domain": True}]}
with open("workers/tickets/wrangler.protected.jsonc", "w") as f:
    json.dump(cfg, f, indent=2)
subprocess.run(["npx", "wrangler", "deploy", "-c", "wrangler.protected.jsonc"], cwd="workers/tickets", env={**os.environ},
               check=True, capture_output=True, text=True)
print("redeployed tickets-mcp on the custom domain, workers.dev off")

# A bearer server defaults to per-user authorization (on_behalf), which service-token sessions never see, so the
# mapping says on_behalf False. A server's hostname and auth_type cannot be changed with PUT (only name, credentials and tool overrides),
# so tickets leaves the portal, is deleted, and is registered again as a bearer server that carries the origin token.
creds = json.dumps({"headers": {"CF-Access-Client-Id": origin["client_id"], "CF-Access-Client-Secret": origin["client_secret"]}})
cf_ok("PUT", f"access/ai-controls/mcp/portals/{PORTAL_ID}", {"servers": [{"server_id": "notes", "on_behalf": False}]})
status, _ = cf("DELETE", "access/ai-controls/mcp/servers/tickets")
print("delete the open tickets server: HTTP", status)
cf_ok("POST", "access/ai-controls/mcp/servers",
      {"id": "tickets", "name": "Tickets", "hostname": ORIGIN_URL, "auth_type": "bearer", "auth_credentials": creds})
t_reg = time.time()
for _ in range(40):
    srv = cf_ok("GET", "access/ai-controls/mcp/servers/tickets")
    if srv["status"] == "ready" and srv.get("tools"):
        break
    if srv["status"] == "error":  # a new hostname may not resolve for the portal yet: ask for another sync
        print(f"  status=error ({(srv.get('error') or '')[:70]}...), syncing again")
        cf("POST", "access/ai-controls/mcp/servers/tickets/sync")
    time.sleep(5)
# The server-level Access application points at the old server record, so it is created again for the new one.
cf("DELETE", f"access/apps/{st['apps']['tickets']}")
st["apps"]["tickets"] = cf_ok("POST", "access/apps", {"name": "portal-demo tickets server", "type": "mcp",
                              "policies": [{"id": st["policy_agent"], "precedence": 1}],
                              "destinations": [{"type": "via_mcp_server_portal", "mcp_server_id": "tickets"}]})["id"]
save_state(st)
result["server_ready_after_s"] = round(time.time() - t_reg)
print(f"portal server tickets: status={srv['status']}, auth_type={srv['auth_type']}, {len(srv.get('tools') or [])} tools ({time.time() - t_reg:.0f}s)")
cf_ok("PUT", f"access/ai-controls/mcp/portals/{PORTAL_ID}",
      {"servers": [{"server_id": "notes", "on_behalf": False}, {"server_id": "tickets", "on_behalf": False, "updated_tools": [{"name": "delete_ticket", "enabled": False}]}]})

print("\n--- the same attempts again")
attempts = (("old workers.dev URL, no credentials", TICKETS_URL, {}),
            ("custom domain, no credentials", ORIGIN_URL, {}),
            ("custom domain, intruder token", ORIGIN_URL, svc_headers(st, "intruder")),
            ("custom domain, origin token", ORIGIN_URL, {"CF-Access-Client-Id": origin["client_id"], "CF-Access-Client-Secret": origin["client_secret"]}))
for label, url, hdr in attempts:
    try:
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, follow_redirects=False, timeout=30,
                       headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **hdr})
        print(f"  {label:38} -> HTTP {r.status_code}")
        result[label] = r.status_code
    except httpx.HTTPError as e:
        print(f"  {label:38} -> {type(e).__name__}")
        result[label] = type(e).__name__
t_back = time.time()
while not listed_through_portal("tickets_list_tickets") and time.time() - t_back < 90:
    time.sleep(2)
agent = Mcp(PORTAL_URL, svc_headers(st))
agent.initialize()
s, ms, msg, _h = agent.call("tickets_list_tickets", {})
text = msg["result"]["content"][0]["text"][:60] if "result" in msg else str(msg)[:80]
print(f"  {'through the portal, agent token':38} -> HTTP {s}, {text}")
result["portal_after_fix"] = s
save_result("06_bypass.json", result)
