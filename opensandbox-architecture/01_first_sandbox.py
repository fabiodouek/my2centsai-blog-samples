"""Create a sandbox, run commands in it, move a file in and out, read its metrics, kill it."""

import time
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.models.filesystem import WriteEntry
from opensandbox.models.sandboxes import SandboxImageSpec

from common import IMAGE, connection, require_server, run

require_server()

started = time.monotonic()
sb = SandboxSync.create(
    image=SandboxImageSpec(IMAGE),
    timeout=timedelta(minutes=10),
    connection_config=connection(),
)
try:
    print(f"created sandbox {sb.id} from {IMAGE} in {time.monotonic() - started:.1f} s")
    print(f"state: {sb.get_info().status.state}")

    run(sb, "uname -a")
    run(sb, "python3 --version")

    text = "hello from the host\n"
    sb.files.write_files([WriteEntry(path="/tmp/hello.txt", data=text, mode=644)])
    assert sb.files.read_file("/tmp/hello.txt") == text, "file content changed on the way"
    print("file round trip: ok")

    m = sb.get_metrics()
    print(f"metrics: {m.cpu_count:g} CPU, {m.cpu_used_percentage:.1f}% used, "
          f"{m.memory_used_in_mib:.0f} of {m.memory_total_in_mib:.0f} MiB memory")
finally:
    try:
        sb.kill()
        print("killed")
    finally:
        sb.close()
