"""T1: cold vs warm create for the four sandbox types, and template sprawl.

Creates the three standard templates in the background (shell-net-off, shell-net-on,
computer-use), times create -> ACTIVE for each, and meanwhile times Code Execution creates.
Then times Shell from a template, Shell from an inline spec, and Computer Use from a template.
Every sandbox gets a short ttl and is deleted right after its timing.
"""
import threading
from sbx import *

rt = runtime_name()
R = {"templates": {}, "code_execution": [], "shell_template": [], "shell_inline": [], "computer_use": []}


def tpl_job():
    out = create_templates_parallel()
    R["templates"] = {k: {"name": short(v[0]), "seconds_to_active": v[1]} for k, v in out.items()}


th = threading.Thread(target=tpl_job)
th.start()
print("templates: creating shell-net-off, shell-net-on, computer-use in the background", flush=True)

print("\n== Code Execution, inline spec, 5 sequential creates")
for i in range(5):
    op, t = timed(client.sandboxes.create, name=rt,
                  spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                  config={"ttl": "600s", "display_name": f"t1-ce-{i}"})
    sb = op.response
    (out, _, _), t_exec = timed(run, sb.name, "print(40+2)")
    print(f"  create {i}: {t:7.2f}s  first execute: {t_exec:5.2f}s  -> {out.strip()}", flush=True)
    R["code_execution"].append({"create": t, "first_execute": t_exec})
    client.sandboxes.delete(name=sb.name)

th.join()
print("\n== Templates: create -> ACTIVE")
for k, v in R["templates"].items():
    print(f"  {k:14s} {v['seconds_to_active']:6.1f}s")

for tname, key, n in (("shell-net-off", "shell_template", 5), ("computer-use", "computer_use", 3)):
    print(f"\n== {tname} from its template, {n} creates")
    for i in range(n):
        sb, t = create_from_template(tname, ttl="600s", display_name=f"t1-{tname}-{i}")
        if tname.startswith("shell"):
            r, t_exec = timed(sh, sb.name, "echo ready")
            ex = f"first execute: {t_exec:5.2f}s"
        else:
            ex = ""
            t_exec = None
        print(f"  create {i}: {t:7.2f}s  {ex}", flush=True)
        R[key].append({"create": t, "first_execute": t_exec})
        client.sandboxes.delete(name=sb.name)

print("\n== Shell, inline spec and no template (the SDK makes a template for each create)")
live = lambda: [tp for tp in client.sandboxes.templates.list(name=rt) if tp.state.name != "DELETED"]
before = len(live())
for i in range(2):
    op, t = timed(client.sandboxes.create, name=rt, spec={"shell_environment": {}},
                  config={"ttl": "600s", "display_name": f"t1-inline-{i}"})
    sb = op.response
    print(f"  create {i}: {t:7.2f}s", flush=True)
    R["shell_inline"].append({"create": t})
    client.sandboxes.delete(name=sb.name)
after = live()
print(f"  templates before: {before}, after: {len(after)}")
for tp in after:
    print("   ", tp.display_name, tp.state.name)
R["templates_before_inline"] = before
R["templates_after_inline"] = len(after)

print("\n== summary (seconds)")
for k in ("code_execution", "shell_template", "shell_inline", "computer_use"):
    xs = [x["create"] for x in R[k]]
    if xs:
        print(f"  {k:16s} first {xs[0]:7.2f}  p50 {p50(xs):7.2f}  max {max(xs):7.2f}  (n={len(xs)})")
save_result("t1", R)
