"""T0: set up. One Agent Runtime instance (no agent deployed) is the parent of every sandbox, template and
snapshot. Creating it, then the first Code Execution sandbox in it, shows the cold path."""
from sbx import *

print("== Agent Runtime instance")
t0 = time.perf_counter()
code, j = rest("POST", "reasoningEngines", {"displayName": "sbx-part3"})
assert code == 200, j
op = j["name"].split("locations/us-central1/")[1]
while not rest("GET", op)[1].get("done"):
    time.sleep(2)
rid = op.split("/")[1]
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime"), "w").write(
    f"projects/{PROJECT}/locations/{LOCATION}/reasoningEngines/{rid}")
print(f"  created in {time.perf_counter()-t0:.1f}s. No agent is deployed; it only parents the sandboxes.")

print("\n== the first Code Execution sandbox in a new instance")
(op, t) = timed(client.sandboxes.create, name=runtime_name(),
                spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                config={"ttl": "600s", "display_name": "t0-first"})
sb = op.response
print(f"  create: {t:.1f}s  state: {sb.state.name}")
(out, _, _), t2 = timed(run, sb.name, "import platform; print(platform.uname().release)")
print(f"  first execute: {t2:.1f}s  -> {out.strip()}")
client.sandboxes.delete(name=sb.name)
save_result("t0", {"runtime_seconds": None, "first_create": t, "first_execute": t2, "kernel": out.strip()})
