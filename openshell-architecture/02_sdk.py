"""Test 2: the same lifecycle from the Python SDK, with the phase changes timestamped."""

from __future__ import annotations

import time

from common import WORKLOAD_IMAGE, WORKSPACE, client, require_gateway, run

NAME = "sdk-demo"


def phase_name(phase: int) -> str:
    """SANDBOX_PHASE_READY -> Ready. The SDK returns the raw enum number and doesn't
    re-export the enum, so read its name from the generated protobuf module."""
    from openshell._proto import openshell_pb2

    return openshell_pb2.SandboxPhase.Name(phase).removeprefix("SANDBOX_PHASE_").title()


def main() -> None:
    require_gateway()
    with client() as c:
        t0 = time.monotonic()
        ref = c.create(workspace=WORKSPACE, name=NAME, spec={"template": {"image": WORKLOAD_IMAGE}})
        print(f"created {ref.name} (id {ref.id})")

        last = None
        while time.monotonic() - t0 < 300:
            phase = phase_name(c.get(NAME, workspace=WORKSPACE).status.phase)
            if phase != last:
                print(f"  +{time.monotonic() - t0:5.1f}s  {phase}")
                last = phase
            if phase in ("Ready", "Error"):
                break
            time.sleep(0.25)
        c.wait_ready(NAME, workspace=WORKSPACE, timeout_seconds=300)
        print(f"create to Ready: {time.monotonic() - t0:.1f} s")

        run(c, NAME, ["python3", "--version"])
        run(c, NAME, ["sh", "-c", "grep PRETTY_NAME /etc/os-release"])

        deletion = c.delete(NAME, workspace=WORKSPACE)
        c.wait_deleted(NAME, workspace=WORKSPACE, expected_sandbox_id=deletion.sandbox_id)
        print("deleted")


if __name__ == "__main__":
    main()
