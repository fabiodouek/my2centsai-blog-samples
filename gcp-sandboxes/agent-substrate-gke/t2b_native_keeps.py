"""Test 2b: what does a native Substrate suspend keep? The same probes, in a native actor (memory fidelity) made
from manifests/probe-actor-template.yaml.tmpl. The actor reports on itself every 5 s; we read its stdout with
`kubectl ate logs actor` before and after each of five suspend/resume cycles."""
import os, re, time
from pathlib import Path
from common import HERE, PROJECT_ID, WORK, banner, kate, kate_logs, summary, timed

AS, TEMPLATE, NAME = "native", "probe", "p1"
CYCLES = 5

def tick_lines(): return [l for l in kate_logs(NAME, AS, "tick=")]

banner("atespace, template, actor, egress rule")
kate("create", "atespace", AS, quiet=True, check=False)
img = re.search(r"AX_DEFAULT_TASK_IMAGE=(\S+)", (WORK / "records" / "source-and-images.txt").read_text()).group(1)
tmpl = (HERE / "manifests" / "probe-actor-template.yaml.tmpl").read_text().replace("${TASK_IMAGE}", img)
kate("create", "actor-template", "-f", "-", input=tmpl, quiet=True)
while True:   # the golden snapshot is taken after the template is created
    out = kate("get", "actor-template", TEMPLATE, atespace=AS, quiet=True, check=False).stdout
    if "GOLDEN" in out and TEMPLATE in out and re.search(rf"{TEMPLATE}\s+\S+", out): break
    time.sleep(3)
time.sleep(15)
kate("create", "actor", NAME, "--template", TEMPLATE, atespace=AS, quiet=True)
kate("create", "egress-policy", NAME, "-f", "-", atespace=AS, quiet=True,
     input='rules:\n- http:\n    hostnames: ["www.google.com"]\n    ports:\n      numbers: [80]\n')
_, ms = timed("kubectl ate resume actor (first)", kate, "resume", "actor", NAME, atespace=AS, quiet=True)
time.sleep(18)
print("before the first suspend:"); print("  " + "\n  ".join(tick_lines()[-2:]))

susp, res = [], []
for c in range(1, CYCLES + 1):
    banner(f"cycle {c}")
    before = len(tick_lines())
    _, ms = timed("suspend", kate, "suspend", "actor", NAME, atespace=AS, quiet=True); susp.append(ms)
    _, ms = timed("resume ", kate, "resume", "actor", NAME, atespace=AS, quiet=True); res.append(ms)
    time.sleep(14)
banner("what the actor reported (one line per 5 s tick; a gap is a suspend)")
ticks = tick_lines()
print("\n".join(ticks))
starts = kate_logs(NAME, AS, "start:")
print(f"\nstart lines (the script's first line runs once per boot): {len(starts)}")
banner("results")
summary("suspend", susp)
summary("resume ", res)
kate("delete", "actor", NAME, atespace=AS, check=False)
kate("delete", "actor-template", TEMPLATE, atespace=AS, check=False)
