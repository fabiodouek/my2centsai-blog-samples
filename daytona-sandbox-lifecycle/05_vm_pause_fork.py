"""Test 5: pause, resume and fork with memory on a Linux VM sandbox (the daytona-vm-* snapshots).
Containers can only stop, which drops memory. VM snapshots live in their own region (us-west-3 when I ran this), so the
client targets whichever region the snapshot is in. Exits early with the API's answer if the org has no linux-vm class."""

import sys
import time

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig, SessionExecuteRequest

from common import SAFETY_TTL, banner, labels, timed

SNAPSHOT = "daytona-vm-small"

banner("Create a Linux VM sandbox")
try:
    regions = Daytona().snapshot.get(SNAPSHOT).region_ids
    print(f"{SNAPSHOT}: regions {regions}")
    daytona = Daytona(DaytonaConfig(target=regions[0])) if regions else Daytona()
    with timed(f"create(snapshot='{SNAPSHOT}')"):
        vm = daytona.create(CreateSandboxFromSnapshotParams(snapshot=SNAPSHOT, labels=labels(test="05"),
                                                             ttl_minutes=SAFETY_TTL), timeout=300)
except Exception as e:
    print(f"{type(e).__name__}: {e}")
    sys.exit("No linux-vm class for this org: pause and fork are VM-only.")
print(f"vm: {vm.id} target={vm.target} class={vm.sandbox_class} auto-pause={vm.auto_pause_interval} auto-stop={vm.auto_stop_interval}")
# A container reports the host's CPUs and memory; a VM should report its own.
print("inside:", vm.process.exec("echo $(nproc) vCPU, $(free -m | awk '/Mem/{print $2}') MiB").result.strip())

# An in-memory counter: nothing on disk, so it only survives if memory does.
vm.process.create_session("counter")
vm.process.execute_session_command("counter", SessionExecuteRequest(
    command="python3 -u -c \"import time\nn=0\nwhile True:\n n+=1; print(n, flush=True); time.sleep(1)\"",
    run_async=True))
time.sleep(3)


def counter(sbx=None) -> str:
    sbx = sbx or vm
    cmds = sbx.process.get_session("counter").commands
    return sbx.process.get_session_command_logs("counter", cmds[-1].id).stdout.split()[-1]


for cycle in range(1, 4):
    banner(f"Pause cycle {cycle}")
    before = counter()
    with timed("pause()"):
        vm.pause()
    if cycle == 1:  # does a command wake a paused VM?
        try:
            vm.process.exec("echo hi")
        except Exception as e:
            print(f"exec while paused: {type(e).__name__}: {e}".strip())
    time.sleep(5)
    with timed("start() from paused"):
        vm.start()
    time.sleep(2)
    print(f"counter: {before} before pause, {counter()} two seconds after resume")

banner("fork() with a running process")
before = counter()
with timed("fork()"):
    child = vm.fork(name="my2cents-05-fork")
forked_at = time.perf_counter()
print(f"fork: {child.id} class={child.sandbox_class}, parent counter was {before} at fork time")
# If memory came along, the fork's counter carries on from the parent's value instead of starting at 1.
for _ in range(2):
    time.sleep(3)
    try:
        print(f"parent counter: {counter()}, fork counter: {counter(child)}")
    except Exception as e:
        print(f"parent counter: {counter()}, fork counter: {type(e).__name__}: {e}")
# A process that is older than the fork itself was copied across, not restarted.
age = child.process.exec("ps -C python3 -o etimes= | sort -n | tail -1").result.strip()
print(f"the fork is {time.perf_counter() - forked_at:.0f}s old; its counter process has been running for {age}s")

banner("Delete the parent while it has a fork")
try:
    vm.delete()
except Exception as e:
    print(f"{type(e).__name__}: {str(e)[:160]}")
child.delete(wait=True)
vm.delete()
print("deleted the fork, then the parent")
