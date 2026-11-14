"""T9: what lands in Cloud Logging. One create / execute / pause / resume / snapshot / delete per type,
with code that prints MARKER-4f2a, then read the audit entries back with gcloud logging read.
Needs Data Access audit logs on for aiplatform.googleapis.com (ADMIN_READ and DATA_WRITE)."""
import collections, subprocess
from sbx import *

rt = runtime_name()
MARK = "MARKER-4f2a"
t_start = datetime.datetime.now(datetime.timezone.utc)
R = {}

print("== one lifecycle per type")
# Code Execution: create, execute, delete (no pause or snapshot for this type)
sb = client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                             config={"ttl": "600s", "display_name": "t9-ce"}).response
print("  code execution:", run(sb.name, f"print('{MARK}')")[0].strip())
client.sandboxes.delete(name=sb.name)

# Shell: create, execute, pause, resume, snapshot, delete
sb, _ = create_from_template("shell-net-off", ttl="600s", display_name="t9-shell")
print("  shell         :", sh(sb.name, f"echo {MARK}")["stdout"].strip())
client.sandboxes.pause(name=sb.name)
client.sandboxes.resume(name=sb.name)
retry(lambda: sh(sb.name, "true"))
snap = client.sandboxes.snapshots.create(source_sandbox_environment_name=sb.name, config={"ttl": "1800s", "display_name": "t9-snap"}).response
client.sandboxes.delete(name=sb.name)
client.sandboxes.snapshots.delete(name=snap.name)

# Computer Use: create, a CDP command (data-plane stream, not an execute RPC), delete
sb, _ = create_from_template("computer-use", ttl="600s", display_name="t9-cu")
cdp(sb, "Runtime.evaluate", {"expression": f"console.log('{MARK}')"})
client.sandboxes.delete(name=sb.name)
t_end = datetime.datetime.now(datetime.timezone.utc)


def read(filter_, limit=200):
    out = subprocess.run(["gcloud", "logging", "read", filter_, "--project", PROJECT, "--format=json",
                          f"--limit={limit}", "--freshness=1h"], capture_output=True, text=True)
    return json.loads(out.stdout or "[]")


base = (f'protoPayload.serviceName="aiplatform.googleapis.com" AND '
        f'(protoPayload.resourceName:"sandboxEnvironment" OR protoPayload.methodName:"SandboxEnvironment") '
        f'AND timestamp>="{t_start.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
print("\n== waiting for the audit entries to appear in Cloud Logging (they lag by a minute or two)")
entries, last = [], -1
for i in range(30):
    entries = read(base)
    if len(entries) == last and i >= 6:      # nothing new for 20 s: they have all arrived
        break
    last = len(entries)
    time.sleep(20)

counts = collections.OrderedDict()
for e in sorted(entries, key=lambda e: e["timestamp"]):
    pp = e["protoPayload"]
    kind = e["logName"].split("%2F")[-1]
    key = (pp["methodName"].split(".")[-1] if pp["methodName"].count(".") > 3 else pp["methodName"], kind)
    counts[key] = counts.get(key, 0) + 1
print(f"\n== {len(entries)} audit entries for sandbox resources")
print(f"  {'method':58s} {'log':11s} count")
for (m, k), n in sorted(counts.items(), key=lambda kv: kv[0][1]):
    print(f"  {m:58s} {k:11s} {n}")
R["entries"] = [{"method": m, "log": k, "count": n} for (m, k), n in counts.items()]

print(f"\n== does the code ({MARK}) appear anywhere in Cloud Logging?")
hits = read(f'"{MARK}" AND timestamp>="{t_start.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
print(f"  gcloud logging read '\"{MARK}\"' -> {len(hits)} entries")
R["marker_hits"] = len(hits)
execs = [e for e in entries if e["protoPayload"]["methodName"].endswith("execute") or "Execute" in e["protoPayload"]["methodName"]]
if execs:
    pp = execs[0]["protoPayload"]
    print(f"\n== one execute entry: {pp['methodName'].split('.')[-1]}")
    print("  fields:", sorted(pp.keys()))
    req = pp.get("request") or pp.get("requestMetadata", {})
    print("  request payload fields:", sorted(k for k in (pp.get("request") or {}) if k != "@type"), "(no code, no stdout)")
    print("  caller user agent:", pp.get("requestMetadata", {}).get("callerSuppliedUserAgent", "")[:70])
    R["execute_entry_keys"] = sorted(pp.keys())
    R["execute_request"] = redact(json.dumps(pp.get("request", None)))[:300]
save_result("t9", R)
