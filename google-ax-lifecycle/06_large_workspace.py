"""Test 6: a 2 GB workspace. Does suspend/resume time grow with size, and does
every byte come back? (google/ax#453 reports a large workspace lost on suspend
while AX reported success.)"""

from common import delete, ax, banner, create, sh, summary, timed

NAME = "lc-big"

banner(f"{NAME}: 2 GB of random data in /workspace")
create(NAME)
ax("resume", "task", NAME, quiet=True)
print(sh(NAME, "for i in 1 2 3 4; do dd if=/dev/urandom of=/workspace/part$i bs=1M count=512 2>/dev/null; done; "
               "sha256sum /workspace/part* > /workspace/sums; du -sh /workspace; df -h /workspace | tail -1"))

susp, res = [], []
for c in (1, 2, 3):
    banner(f"cycle {c}")
    _, ms = timed("ax suspend", ax, "suspend", "task", NAME, quiet=True)
    susp.append(ms)
    _, ms = timed("ax resume", ax, "resume", "task", NAME, quiet=True)
    res.append(ms)
    print(sh(NAME, "sha256sum -c --quiet /workspace/sums && echo 'all 4 parts intact'", check=False))

banner("results")
summary("suspend", susp)
summary("resume ", res)

delete(NAME)
