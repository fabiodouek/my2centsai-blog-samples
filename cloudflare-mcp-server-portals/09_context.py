"""Tool definitions cost context. Add a 40-tool server, then compare what a client receives with the portal's context options."""
import json
import os
import subprocess
import time

from common import *

st = load_state()
CATALOG_URL = f"https://catalog-mcp.{WORKERS_SUB}.workers.dev/mcp"

# A third server with 40 tools.
subprocess.run(["npm", "install", "--silent"], cwd="workers/catalog", check=True, capture_output=True)
subprocess.run(["npx", "wrangler", "deploy"], cwd="workers/catalog", env={**os.environ}, check=True, capture_output=True, text=True)
cf("POST", "access/ai-controls/mcp/servers", {"id": "catalog", "name": "Catalog", "hostname": CATALOG_URL, "auth_type": "unauthenticated"})
if "catalog" not in st["apps"]:
    st["apps"]["catalog"] = cf_ok("POST", "access/apps", {"name": "portal-demo catalog server", "type": "mcp", "policies": [{"id": st["policy_agent"], "precedence": 1}],
                                  "destinations": [{"type": "via_mcp_server_portal", "mcp_server_id": "catalog"}]})["id"]
    save_state(st)
portal = cf_ok("GET", f"access/ai-controls/mcp/portals/{PORTAL_ID}")
mapping = [{k: v for k, v in s.items() if k in ("server_id", "default_disabled", "updated_tools", "updated_prompts")} | {"on_behalf": False} for s in portal["servers"]]
cf_ok("PUT", f"access/ai-controls/mcp/portals/{PORTAL_ID}", {"servers": mapping + [{"server_id": "catalog", "on_behalf": False}]})
print("catalog server added to the portal (40 tools)")


def size(query: str):
    m = Mcp(PORTAL_URL + query, svc_headers(st))
    m.initialize()
    tools = m.tools()
    return m, tools


for _ in range(40):  # wait until the new server's tools show up
    _, tools = size("")
    if sum(t["name"].startswith("catalog_") for t in tools) == 40:
        break
    time.sleep(3)

rows = []
print(f"{'portal URL query':38} {'tools':>5} {'chars':>7} {'~tokens':>8}")
for label, query in (("(none)", ""), ("?optimize_context=minimize_tools", "?optimize_context=minimize_tools"),
                     ("?optimize_context=search_and_execute", "?optimize_context=search_and_execute"),
                     ("?codemode=search_and_execute", "?codemode=search_and_execute")):
    try:
        _, tools = size(query)
    except Exception as e:  # noqa: BLE001
        print(f"{label:38} {type(e).__name__}")
        continue
    chars = len(json.dumps(tools, separators=(",", ":")))
    rows.append({"query": query, "tools": len(tools), "chars": chars, "approx_tokens": chars // 4, "names": [t["name"] for t in tools if t["name"].startswith("portal_")]})
    print(f"{label:38} {len(tools):5} {chars:7} {chars // 4:8}")

# The cheap list still works: find a tool by pattern, then call it through the portal.
m, _ = size("?optimize_context=search_and_execute")
s, ms, msg, _h = m.call("portal_query_tools", {"pattern": "list_inv"})
print("\nportal_query_tools(pattern='list_inv') ->", str(msg.get("result", msg))[:150] if isinstance(msg, dict) else msg)
rows.append({"query_tools_http": s})
s, ms, msg, _h = m.call("portal_execute", {"tool_name": "catalog_list_invoices", "arguments": {}})
print("portal_execute(catalog_list_invoices)   ->", str(msg.get("result", msg))[:150] if isinstance(msg, dict) else msg)
rows.append({"execute_http": s})
save_result("09_context.json", rows)
