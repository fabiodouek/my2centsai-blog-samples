"""Default-deny egress: one host allowed at creation, a second host allowed while the sandbox runs."""

import sys
import time
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.models.filesystem import WriteEntry
from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule, SandboxImageSpec

from common import IMAGE, connection, require_server

ALLOWED_AT_START = "pypi.org"
ALLOWED_LATER = "github.com"

# python:3.12-slim has no curl, so the sandbox checks a URL with this small script.
# "blocked" means the name does not resolve or the connection is refused; a timeout is reported as such.
PROBE = """import socket, sys, urllib.error, urllib.request
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=10).status)
except urllib.error.HTTPError as e:
    print(e.code)
except urllib.error.URLError as e:
    if isinstance(e.reason, (socket.gaierror, ConnectionRefusedError, ConnectionResetError)):
        print("blocked")
    elif isinstance(e.reason, TimeoutError):
        print("timeout")
    else:
        print(f"error: {e.reason}")
except TimeoutError:
    print("timeout")
except OSError as e:
    print(f"error: {e}")
"""

failures: list[str] = []


def describe(policy: NetworkPolicy) -> str:
    rules = ", ".join(f"{r.action} {r.target}" for r in policy.egress or [])
    return f"default {policy.default_action}; {rules}"


def check(sb: SandboxSync, host: str, expected: str) -> None:
    """Print the HTTP status of https://<host>/ as seen from inside the sandbox, and record a surprise."""
    execution = sb.commands.run(f"python3 /tmp/probe.py https://{host}/")
    answer = "".join(part.text for part in execution.logs.stdout).strip() or "no answer"
    print(f"{host}: {answer}")
    if answer != expected:
        failures.append(f"{host}: expected {expected}, got {answer}")


require_server()

started = time.monotonic()
sb = SandboxSync.create(
    image=SandboxImageSpec(IMAGE),
    timeout=timedelta(minutes=10),
    network_policy=NetworkPolicy(
        defaultAction="deny",
        egress=[NetworkRule(action="allow", target=ALLOWED_AT_START)],
    ),
    connection_config=connection(),
)
try:
    print(f"created sandbox {sb.id} in {time.monotonic() - started:.1f} s")
    sb.files.write_files([WriteEntry(path="/tmp/probe.py", data=PROBE, mode=644)])
    print(f"policy before: {describe(sb.get_egress_policy())}")

    check(sb, ALLOWED_AT_START, "200")
    check(sb, ALLOWED_LATER, "blocked")

    sb.patch_egress_rules([NetworkRule(action="allow", target=ALLOWED_LATER)])
    print(f"policy after:  {describe(sb.get_egress_policy())}")
    check(sb, ALLOWED_LATER, "200")
finally:
    try:
        sb.kill()
        print("killed")
    finally:
        sb.close()

if failures:
    sys.exit("unexpected result: " + "; ".join(failures))
