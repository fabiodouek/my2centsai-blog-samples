"""T2: fingerprint and real size of each sandbox type.

Code Execution at the default size and at MACHINE_CONFIG_VCPU4_RAM4GIB, a Shell sandbox,
and a Computer Use sandbox (through CDP). Prints kernel, dmesg, CPUs, memory, and the size
the platform put in the sandbox's routing token.
"""
from sbx import *

rt = runtime_name()
R = {}

CODE = r'''
import os, platform, subprocess
print("uname      ", platform.uname().release, platform.uname().machine)
print("proc/version", open("/proc/version").read().strip()[:60])
print("cpu_count  ", os.cpu_count(), " sched_getaffinity", len(os.sched_getaffinity(0)))
print("MemTotal   ", [l for l in open("/proc/meminfo") if l.startswith("MemTotal")][0].strip())
print("dmesg      ", subprocess.run("dmesg 2>&1 | head -4", shell=True, capture_output=True, text=True).stdout.strip().replace("\n", " | ")[:70])
print("python     ", platform.python_version())
print("user       ", os.getuid(), os.environ.get("USER"))
'''

for label, spec in (
    ("code_execution_default", {"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}}),
    ("code_execution_4vcpu_4gib", {"code_execution_environment": {
        "code_language": "LANGUAGE_PYTHON", "machine_config": "MACHINE_CONFIG_VCPU4_RAM4GIB"}}),
):
    print(f"\n== {label}")
    sb = client.sandboxes.create(name=rt, spec=spec, config={"ttl": "600s", "display_name": f"t2-{label}"}).response
    out, stderr, _ = run(sb.name, CODE)
    print(out + stderr)
    R[label] = {"output": out, "token_claims": routing_claims(sb) if sb.connection_info else None}
    print("platform says:", R[label]["token_claims"])
    client.sandboxes.delete(name=sb.name)

SHELL = ("uname -r; dmesg 2>&1 | head -2; id; echo cpus: $(nproc); free -m | head -2; "
         "grep PRETTY_NAME /etc/os-release; python3 -V; (node -v 2>&1 | head -1)")
for label, tname in (("shell", "shell-net-off"),):
    print(f"\n== {label}")
    sb, _ = create_from_template(tname, ttl="600s", display_name=f"t2-{label}")
    r = sh(sb.name, SHELL)
    print(r["stdout"], r["stderr"])
    R[label] = {"output": r["stdout"] + r["stderr"], "token_claims": routing_claims(sb)}
    print("platform says:", R[label]["token_claims"])
    client.sandboxes.delete(name=sb.name)

print("\n== computer use (through CDP)")
sb, _ = create_from_template("computer-use", ttl="600s", display_name="t2-computer-use")
tok = cu_token()
js = "JSON.stringify({cores: navigator.hardwareConcurrency, mem: navigator.deviceMemory, ua: navigator.userAgent, platform: navigator.platform})"
res = cdp(sb, "Runtime.evaluate", {"expression": js, "returnByValue": True}, tok=tok)
val = res["result"]["value"] if isinstance(res, dict) and "result" in res else res
print(val)
R["computer_use"] = {"navigator": val, "token_claims": routing_claims(sb)}
print("platform says:", R["computer_use"]["token_claims"])
client.sandboxes.delete(name=sb.name)
save_result("t2", R)
