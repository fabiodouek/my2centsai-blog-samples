"""Test 9: ask the deployed ADK agent (adk-sbx) something that needs code, and print the events.
usage: python3 t9_adk_ask.py SESSION "prompt" [max-seconds]   (the service is private: uses your identity token)
"""
import subprocess, sys, json, time, urllib.request
sid, prompt = sys.argv[1], sys.argv[2]; maxs = int(sys.argv[3]) if len(sys.argv) > 3 else 120
tok = subprocess.run(["gcloud","auth","print-identity-token"],capture_output=True,text=True).stdout.strip()
U = subprocess.run(["gcloud","run","services","describe","adk-sbx","--format=value(status.url)"],capture_output=True,text=True).stdout.strip()
def post(path, body, t):
    r = urllib.request.Request(U+path, data=json.dumps(body).encode(), headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json"})
    return urllib.request.urlopen(r, timeout=t).read()
post(f"/apps/sbx_agent/users/u/sessions/{sid}", {}, 10)
t0 = time.time()
try:
    d = json.loads(post("/run", {"app_name":"sbx_agent","user_id":"u","session_id":sid,"new_message":{"role":"user","parts":[{"text":prompt}]}}, maxs))
except Exception as e:
    print("request failed after %.0f s: %s" % (time.time()-t0, e)); sys.exit(1)
print("took %.1f s" % (time.time()-t0))
for e in d:
    print("EVENT", e.get("author"), e.get("finishReason") or "")
    for p in (e.get("content") or {}).get("parts", []):
        for k, v in p.items():
            if k != "thoughtSignature" and v: print("  ", k, "->", str(v)[:500].replace("\n", " | "))
