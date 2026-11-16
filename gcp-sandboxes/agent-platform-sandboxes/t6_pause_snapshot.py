"""T6: pause vs snapshot: what survives, how long it takes, what the errors say."""
import re
from sbx import *

rt = runtime_name()
R = {}


def probe(sb_name):
    """What is still there? Returns a dict of yes/no/values."""
    r, waited = retry(lambda: sh(sb_name, "echo -n \"proc=\"; kill -0 $(cat /workspace/pid) 2>/dev/null && echo alive || echo gone; "
                    "echo -n \"port9000=\"; python3 -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9000/',timeout=3).status)\" 2>/dev/null || echo down; "
                    "echo -n \"file=\"; cat /workspace/f 2>/dev/null || echo missing; "
                    "echo -n \"tmpfile=\"; cat /tmp/g 2>/dev/null || echo missing; "
                    "echo -n \"pkg=\"; python3 -c 'import cowsay;print(\"present\")' 2>/dev/null || echo missing; "
                    "echo -n \"boot_id=\"; cat /proc/sys/kernel/random/boot_id; "
                    "echo -n \"uptime=\"; cut -d' ' -f1 /proc/uptime"))
    d = dict(l.split("=", 1) for l in r["stdout"].strip().splitlines() if "=" in l)
    d["data_plane_wait_s"] = waited
    return d


def show(label, d):
    print(f"  {label:22s} " + "  ".join(f"{k}={v}" for k, v in d.items()))


def state(name):
    return client.sandboxes.get(name=name).state.name


print("== set up a Shell sandbox (internet on): a process, two files, a package")
sb, t = create_from_template("shell-net-on", ttl="3600s", display_name="t6-shell")
n = sb.name
print(f"  create {t:.1f}s")
sh(n, "setsid nohup python3 -m http.server 9000 >/dev/null 2>&1 & echo $! > /workspace/pid; echo hi > /workspace/f; echo tmp > /tmp/g")
r = sh(n, "pip install cowsay 2>&1 | grep -E 'Successfully|ERROR'", timeout=90)
print("  pip:", r["stdout"].strip()[:80])
time.sleep(1)
before = probe(n)
show("before", before)
R["before"] = before

print("\n== PAUSE")
op, t = timed(client.sandboxes.pause, name=n)
R["pause_seconds"] = t
print(f"  pause took {t:.1f}s, state {state(n)}")
t0 = time.perf_counter()
try:
    sh(n, "echo hello")
    msg = "no error"
except Exception as e:  # noqa: BLE001
    msg = err(e, 330)
print(f"  execute while paused ({time.perf_counter()-t0:.1f}s) ->", msg[:60])
print("   ", re.search(r"'message': '([^']*)'", msg).group(1) if re.search(r"'message': '([^']*)'", msg) else "")
R["paused_execute_error"] = msg
t0 = time.perf_counter()
while state(n) != "STATE_RUNNING":
    time.sleep(1)
R["auto_resume_seconds"] = round(time.perf_counter() - t0, 1)
print(f"  ...and the sandbox is RUNNING again {R['auto_resume_seconds']}s later, nobody called resume()")
after_auto = probe(n)
show("after auto-resume", after_auto)
R["after_auto_resume"] = after_auto

print("\n== explicit PAUSE then RESUME")
client.sandboxes.pause(name=n)
op, t = timed(client.sandboxes.resume, name=n)
R["resume_seconds"] = t
print(f"  resume took {t:.1f}s, state {state(n)}")
after_pause = probe(n)
show("after pause/resume", after_pause)
R["after_pause"] = after_pause
try:
    client.sandboxes.resume(name=n)
    msg = "no error"
except Exception as e:  # noqa: BLE001
    msg = err(e, 330)
print("  resume on a running sandbox ->", msg[:100])
R["double_resume_error"] = msg

print("\n== SNAPSHOT (source keeps running) and RESTORE into a new sandbox")
snap_op, t = timed(client.sandboxes.snapshots.create, source_sandbox_environment_name=n,
                   config={"display_name": "t6-snap", "ttl": "7200s"})
snap = client.sandboxes.snapshots.get(name=snap_op.response.name)
R["snapshot_seconds"] = t
R["snapshot"] = {"sizeBytes": snap.size_bytes, "expire": str(snap.expire_time), "parent": snap.parent_snapshot,
                 "post_action": str(snap.post_snapshot_action)}
code, raw = rest("GET", snap.name.split("locations/us-central1/")[1])
R["snapshot_rest_fields"] = sorted(raw)
print(f"  snapshot took {t:.1f}s; the REST resource has fields {', '.join(sorted(raw))}")
print(f"  sizeBytes: {raw.get('sizeBytes')}   parentSnapshot: {raw.get('parentSnapshot')}   expireTime - createTime: "
      f"{(datetime.datetime.fromisoformat(raw['expireTime'].replace('Z','+00:00')) - datetime.datetime.fromisoformat(raw['createTime'].replace('Z','+00:00'))).total_seconds():.0f}s")
print(f"  source sandbox state after snapshot: {state(n)}")
rs, t = timed(client.sandboxes.create, name=rt, config={"sandbox_environment_snapshot": snap.name, "ttl": "1800s", "display_name": "t6-restore"})
r1 = rs.response
R["restore_seconds"] = t
print(f"  restore into a NEW sandbox took {t:.1f}s; new sandbox is a different resource: {r1.name != n}")
try:
    restored = probe(r1.name)
except Exception as e:  # noqa: BLE001
    restored = {"error": err(e, 200)}
show("restored", restored)
R["restored"] = restored
print("  source still has its process:", probe(n).get("proc"))

print("\n== restore the same snapshot a second time, and snapshot the restored sandbox (ancestry)")
rs2, t = timed(client.sandboxes.create, name=rt, config={"sandbox_environment_snapshot": snap.name, "ttl": "1800s", "display_name": "t6-restore2"})
print(f"  second restore {t:.1f}s")
snap2 = client.sandboxes.snapshots.create(source_sandbox_environment_name=r1.name, config={"display_name": "t6-snap-child", "ttl": "7200s"}).response
snap2 = client.sandboxes.snapshots.get(name=snap2.name)
R["child_snapshot"] = {"parent": snap2.parent_snapshot, "sizeBytes": snap2.size_bytes}
code, raw2 = rest("GET", snap2.name.split("locations/us-central1/")[1])
print("  child snapshot: parentSnapshot =", raw2.get("parentSnapshot"), "| sizeBytes =", raw2.get("sizeBytes"), "| source is the restored sandbox:", raw2.get("sourceSandboxEnvironment", "").endswith(r1.name.rsplit("/", 1)[-1]))

print("\n== negatives: Code Execution")
ce = client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                             config={"ttl": "600s", "display_name": "t6-ce"}).response
for label, fn in (("snapshot", lambda: client.sandboxes.snapshots.create(source_sandbox_environment_name=ce.name, config={"ttl": "600s"})),
                  ("pause", lambda: client.sandboxes.pause(name=ce.name))):
    try:
        fn()
        msg = "no error"
    except Exception as e:  # noqa: BLE001
        msg = err(e, 260)
    print(f"  code execution {label} -> {msg[:105]}")
    R[f"ce_{label}"] = msg
client.sandboxes.delete(name=ce.name)

for s in (n, r1.name, rs2.response.name):
    client.sandboxes.delete(name=s)
for sn in (snap2.name, snap.name):
    try:
        client.sandboxes.snapshots.delete(name=sn)
    except Exception as e:  # noqa: BLE001
        print("snapshot delete:", err(e, 200))
save_result("t6", R)
