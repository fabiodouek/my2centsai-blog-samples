"""What gets logged: one allowed call, one call to a hidden tool, one blocked token. Then the portal log, the counts and the Access log."""
import time
from collections import Counter

from common import *

st = load_state()
agent = Mcp(PORTAL_URL, svc_headers(st))
agent.initialize()
agent.call("notes_get_note", {"id": "n2"})
agent.call("tickets_delete_ticket", {"id": 1})      # hidden in 05/06: the portal answers with an error
agent.call("notes_get_note", {"id": "no-such-note"})  # the upstream returns isError
Mcp(PORTAL_URL, svc_headers(st, "intruder")).initialize()  # blocked by the portal's Access policy
time.sleep(10)

print("MCP portal log (newest first; the five columns of the dashboard's Logs tab):")
logs = cf_ok("GET", "access/ai-controls/mcp/logs")
print(f"{'time (UTC)':20} {'server':8} {'capability':11} {'tool':22} {'ms':>5}  status")
for x in logs[:8]:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(x["created_at"] / 1000))
    print(f"{ts:20} {x['server_id'] or '-':8} {x['capability_name']:11} {(x['tool_name'] or '-'):22} {x['duration']:5}  {x['response_status']}")

print("\nCounts by status over the trailing 7 days (analytics endpoint):")
for status in ("success", "error", "all"):
    r = cf_ok("GET", "access/ai-controls/mcp/analytics/tool-calls/timeseries", params={"days": 7, "response_status": status})
    print(f"  {status:8} {r['total']}")

print("\nAccess log, the blocked token (field names only; IPs and emails stay out of the output):")
acc = cf_ok("GET", "access/logs/access_requests", params={"limit": 50})
print(" ", Counter((x["app_type"], x["allowed"], x["action"]) for x in acc))
save_result("08_logs.json", {"sample": [{k: x[k] for k in ("server_id", "capability_name", "tool_name", "duration", "response_status")} for x in logs[:8]],
                              "counts": {s: cf_ok("GET", "access/ai-controls/mcp/analytics/tool-calls/timeseries", params={"days": 7, "response_status": s})["total"] for s in ("success", "error", "all")}})
