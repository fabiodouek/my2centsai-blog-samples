"""T4: state, default TTLs per type, TTL reset on execute, deletion at expiry."""
import threading
from sbx import *

rt = runtime_name()
rel = rt.split("locations/us-central1/")[1]
R = {"defaults": {}, "reset": {}}


def raw(sb_name):
    code, j = rest("GET", sb_name.split("locations/us-central1/")[1])
    return code, j


def parse(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


print("== (a) Code Execution keeps Python state between calls")
sb = client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                             config={"ttl": "600s", "display_name": "t4-state"}).response
run(sb.name, "x = 41")
print("  x + 1 ->", run(sb.name, "print(x + 1)")[0].strip())
client.sandboxes.delete(name=sb.name)

print("\n== (b) Default TTL: create each type WITHOUT ttl, read it back over REST, delete at once")
makers = {
    "code execution": lambda: client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}}, config={"display_name": "t4-nottl"}).response,
    "shell (template)": lambda: client.sandboxes.create(name=rt, config={"sandbox_environment_template": template("shell-net-off"), "display_name": "t4-nottl"}).response,
    "computer use (template)": lambda: client.sandboxes.create(name=rt, config={"sandbox_environment_template": template("computer-use"), "display_name": "t4-nottl"}).response,
}
for label, mk in makers.items():
    sb = mk()
    code, j = raw(sb.name)
    ct, et = j.get("createTime"), j.get("expireTime")
    ttl = (parse(et) - parse(ct)).total_seconds() if et else None
    human = f"{ttl/3600:.1f} h = {ttl/86400:.2f} days" if ttl else "no expireTime"
    print(f"  {label:24s} ttl field: {j.get('ttl')!s:6s} expireTime - createTime = {human}")
    R["defaults"][label] = {"ttl_field": j.get("ttl"), "create": ct, "expire": et, "seconds": ttl}
    client.sandboxes.delete(name=sb.name)


def reset_check(label, make, touch):
    """ttl=300s; touch at t=240s; does expireTime move? then wait for expiry and look again."""
    sb = make()
    t0 = time.time()
    _, j = raw(sb.name)
    exp0 = parse(j["expireTime"])
    touch(sb.name)
    time.sleep(max(0, 240 - (time.time() - t0)))
    _, j = raw(sb.name)
    exp_before = parse(j["expireTime"])
    touch(sb.name)
    _, j = raw(sb.name)
    exp_after = parse(j["expireTime"])
    created = parse(j["createTime"])
    gap = (exp_after - exp_before).total_seconds()
    R["reset"][label] = {"expire_initial_from_create": (exp0 - created).total_seconds(),
                         "expire_after_touch_from_create": (exp_after - created).total_seconds(),
                         "moved_seconds": gap}
    # wait for the (possibly extended) expiry, then see what GET says
    time.sleep(max(0, (exp_after - datetime.datetime.now(datetime.timezone.utc)).total_seconds()) + 20)
    code, j = raw(sb.name)
    R["reset"][label]["after_expiry"] = {"http": code, "status": (j.get("error", {}).get("status") if isinstance(j, dict) else j)}


print("\n== (c, d) ttl=300s, touch at t=240s: does the clock reset? then wait for deletion (about six minutes)")
mk_ce = lambda: client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                                        config={"ttl": "300s", "display_name": "t4-reset-ce"}).response
mk_sh = lambda: client.sandboxes.create(name=rt, config={"sandbox_environment_template": template("shell-net-off"),
                                                         "ttl": "300s", "display_name": "t4-reset-sh"}).response
th = [threading.Thread(target=reset_check, args=("code execution (execute_code)", mk_ce, lambda n: run(n, "print(1)"))),
      threading.Thread(target=reset_check, args=("shell (execute_bash)", mk_sh, lambda n: sh(n, "echo hi")))]
[t.start() for t in th]
[t.join() for t in th]
for label, d in R["reset"].items():
    print(f"  {label}")
    print(f"    expireTime was create + {d['expire_initial_from_create']:.0f}s; after an execute at t=240s it is create + {d['expire_after_touch_from_create']:.0f}s "
          f"(moved {d['moved_seconds']:+.0f}s)")
    print(f"    GET after expiry: HTTP {d['after_expiry']['http']} {d['after_expiry']['status']}")
save_result("t4", R)
