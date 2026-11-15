"""What a client sees at the portal URL: no credentials, a wrong service token, the right one."""
import json

import httpx

from common import *

st = load_state()
out = {}

r = httpx.post(PORTAL_URL, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
               json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}})
print("1. no credentials ->", r.status_code)
wa = r.headers.get("www-authenticate", "")
print("   WWW-Authenticate:", wa.split(", resource_metadata=")[0])
print("     resource_metadata:", wa.split("resource_metadata=")[-1].strip('"'))
out["no_credentials"] = {"status": r.status_code, "www_authenticate": r.headers.get("www-authenticate"), "body": r.text[:200]}

meta_url = f"https://{PORTAL_HOST}/.well-known/oauth-protected-resource/mcp"
m = httpx.get(meta_url)
print(f"2. protected-resource metadata -> {m.status_code}")
if m.status_code == 200:
    print("  ", json.dumps(m.json(), indent=1).replace("\n", "\n   "))
    out["resource_metadata"] = m.json()
    auth = m.json().get("authorization_servers", [None])[0]
    if auth:
        a = httpx.get(auth.rstrip("/") + "/.well-known/oauth-authorization-server")
        print("   authorization server metadata ->", a.status_code, "endpoints:", ", ".join(k.replace("_endpoint", "") for k in sorted(a.json()) if k.endswith("_endpoint")) if a.status_code == 200 else a.text[:100])

bad = Mcp(PORTAL_URL, svc_headers(st, "intruder"))
s, ms, msg, h = bad.initialize()
print(f"3. service token the policy does not allow -> {s} ({'Cloudflare Access block page' if isinstance(msg, str) and 'Access' in msg else str(msg)[:80]})")
out["intruder"] = {"status": s}

good = Mcp(PORTAL_URL, svc_headers(st))
s, ms, msg, h = good.initialize()
print(f"4. allowed service token -> {s} in {ms:.0f} ms, server={msg['result']['serverInfo']['name']}, session header: {'yes' if 'Mcp-Session-Id' in good.headers else 'no'}")
out["agent"] = {"status": s, "ms": round(ms), "session": "Mcp-Session-Id" in good.headers}
save_result("03_probe.json", out)
