"""Failure modes: a tool call that outlasts the 60 second upstream timeout, and an upstream server that stops answering."""
import time

import httpx

from common import *

st = load_state()
m = Mcp(PORTAL_URL, svc_headers(st))
m.initialize()
out = {}

for secs in (5, 65):
    t = time.time()
    s, ms, msg, _h = m.call("tickets_slow_report", {"seconds": secs}, timeout=100)
    body = msg["result"]["content"][0]["text"] if isinstance(msg, dict) and "result" in msg else str(msg)[:160]
    print(f"slow_report({secs}s) via portal -> HTTP {s} after {time.time() - t:.1f}s: {body}")
    out[f"slow_{secs}"] = {"http": s, "seconds": round(time.time() - t, 1), "body": body[:160]}

# A server whose upstream disappears. Register a server pointing at a Worker URL that does not exist and watch its status.
cf("DELETE", "access/ai-controls/mcp/servers/broken")
cf_ok("POST", "access/ai-controls/mcp/servers", {"id": "broken", "name": "Broken", "hostname": f"https://does-not-exist.{WORKERS_SUB}.workers.dev/mcp", "auth_type": "unauthenticated"})
for _ in range(10):
    srv = cf_ok("GET", "access/ai-controls/mcp/servers/broken")
    if srv["status"] != "waiting":
        break
    time.sleep(2)
print(f"server pointing at a missing Worker: status={srv['status']}, error={srv['error']!r}, tools={len(srv.get('tools') or [])}")
out["broken"] = {"status": srv["status"], "error": srv["error"]}
cf("DELETE", "access/ai-controls/mcp/servers/broken")
save_result("10_failures.json", out)
