"""Curate what the portal exposes: hide a destructive tool, rename one, and watch a new upstream tool arrive on sync."""
import os
import subprocess
import time

from common import *

st = load_state()


def agent_tools():
    m = Mcp(PORTAL_URL, svc_headers(st))
    m.initialize()
    return m, [t["name"] for t in m.tools() if not t["name"].startswith("portal_")]


def wait_until(check, what, limit=90):
    """Portal changes are not instant. Poll with fresh sessions until the agent's view matches, and say how long it took."""
    t = time.time()
    while True:
        m, names = agent_tools()
        if check(names) or time.time() - t > limit:
            print(f"  ({what} after {time.time() - t:.0f}s)")
            return m, names
        time.sleep(2)


def put_mapping(tickets_tools, notes_tools=None):
    """A portal update with `servers` replaces the whole mapping, so both servers are always sent."""
    body = {"servers": [{"server_id": "notes", "on_behalf": False, **({"updated_tools": notes_tools} if notes_tools else {})},
                        {"server_id": "tickets", "on_behalf": False, "updated_tools": tickets_tools}]}
    return cf_ok("PUT", f"access/ai-controls/mcp/portals/{PORTAL_ID}", body)


_, before = agent_tools()
print("before:", before)

put_mapping([{"name": "delete_ticket", "enabled": False},
             {"name": "list_tickets", "enabled": True, "alias": "recent", "description": "Recent tickets (curated description)."}])
m, after = wait_until(lambda n: "tickets_recent" in n and "tickets_delete_ticket" not in n, "curation visible to a new session")
print("after: ", after)
s, ms, msg, _h = m.call("tickets_delete_ticket", {"id": 1})
print("calling the hidden tool anyway ->", s, str(msg)[:160])
s, ms, msg, _h = m.call("tickets_recent", {"open_only": True})
print("calling the renamed tool       ->", s, str(msg.get("result", msg))[:120])

# A new tool appears upstream: redeploy notes with one more tool, ask the portal to sync.
print("\nredeploying notes-mcp with an extra tool (count_notes)...")
env = {**os.environ}
subprocess.run(["npx", "wrangler", "deploy", "--var", "EXTRA_TOOL:1"], cwd="workers/notes", env=env, check=True,
               capture_output=True, text=True)
t = time.time()
cf_ok("POST", "access/ai-controls/mcp/servers/notes/sync")
for _ in range(20):
    srv = cf_ok("GET", "access/ai-controls/mcp/servers/notes")
    names = [x["name"] for x in srv.get("tools") or []]
    if "count_notes" in names:
        break
    time.sleep(2)
print(f"sync done in {time.time() - t:.1f}s; server tools now: {names}")
_, seen = wait_until(lambda n: "notes_count_notes" in n, "new tool visible to the agent")
print("agent sees:", [n for n in seen if n.startswith("notes_")])

# Put everything back.
subprocess.run(["npx", "wrangler", "deploy"], cwd="workers/notes", env=env, check=True, capture_output=True, text=True)
cf_ok("POST", "access/ai-controls/mcp/servers/notes/sync")
put_mapping([])
save_result("05_curate.json", {"before": before, "after": after, "after_sync": seen})
