"""Test 3: suspend and resume, five times. The task's command records every boot
in /workspace, so we can see what each resume actually does."""

from common import delete, ax, banner, create, sh, status, summary, timed

NAME = "lc-cycles"
CYCLES = 5

# spec.command runs again on every resume; it appends a line per boot.
BOOT = ["sh", "-c", 'echo "boot $(date +%T) pid $$" >> /workspace/boots.log; exec sleep infinity']

banner("create, resume, and write 50 MB plus a counter into /workspace")
create(NAME, BOOT)
ax("resume", "task", NAME, quiet=True)
print(sh(NAME, "dd if=/dev/urandom of=/workspace/blob bs=1M count=50 2>/dev/null; "
               "sha256sum /workspace/blob > /workspace/blob.sha; echo 0 > /workspace/counter; "
               "echo /tmp-was-here > /tmp/marker; cat /workspace/boots.log"))

susp, res = [], []
for c in range(1, CYCLES + 1):
    banner(f"cycle {c}")
    _, ms = timed("ax suspend", ax, "suspend", "task", NAME, quiet=True)
    susp.append(ms)
    print(f"             -> {status(NAME)['phase']}")
    _, ms = timed("ax resume", ax, "resume", "task", NAME, quiet=True)
    res.append(ms)
    print(sh(NAME, "c=$(($(cat /workspace/counter)+1)); echo $c > /workspace/counter; "
                   "echo counter=$c; sha256sum -c /workspace/blob.sha | sed 's/^/blob: /'; "
                   "ls /tmp/marker 2>&1 | sed 's/^/tmp: /'"))

banner("results")
summary("suspend", susp)
summary("resume ", res)
print(sh(NAME, "cat /workspace/boots.log"))

delete(NAME)
