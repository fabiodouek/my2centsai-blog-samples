"""Test 3: how the lifecycle timers behave.
A: auto-stop fires even while a process is running.  B: refresh_activity() keeps it alive.
C: ttl_minutes destroys the sandbox in any state.     D: auto_delete_interval=0 deletes on stop."""

import time

from daytona import CreateSandboxFromSnapshotParams, Daytona, SessionExecuteRequest

from common import SAFETY_TTL, banner, labels

daytona = Daytona()


def state_of(sandbox_id: str) -> str:
    try:
        return str(daytona.get(sandbox_id).state.value)
    except Exception as e:
        return f"gone ({type(e).__name__})"


def busy(sbx) -> None:
    sbx.process.create_session("busy")
    sbx.process.execute_session_command("busy", SessionExecuteRequest(command="sleep 3600", run_async=True))


banner("Create A (auto-stop 1 min) and B (same, plus refresh_activity)")
a = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="03", case="A"), auto_stop_interval=1, ttl_minutes=SAFETY_TTL))
b = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="03", case="B"), auto_stop_interval=1, ttl_minutes=SAFETY_TTL))
busy(a)
busy(b)
print(f"A {a.id}\nB {b.id}\nboth running 'sleep 3600'")

banner("Create C (ttl 2 min, then stopped) and D (auto-delete on stop)")
c = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="03", case="C"), ttl_minutes=2))
print(f"C {c.id} auto_destroy_at={getattr(c, 'auto_destroy_at', None)}")
c.stop()
print(f"C stopped: {state_of(c.id)}")
d = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="03", case="D"), auto_delete_interval=0, ttl_minutes=SAFETY_TTL))
d.stop()
time.sleep(5)
print(f"D after stop(): {state_of(d.id)}")

banner("Poll every 15s for 3.5 min (B gets refresh_activity each tick)")
start = time.time()
while time.time() - start < 210:
    b.refresh_activity()
    t = time.time() - start
    print(f"t={t:5.0f}s  A={state_of(a.id):10s} B={state_of(b.id):10s} C={state_of(c.id)}")
    time.sleep(15)

for s in (a, b):
    s.delete()
