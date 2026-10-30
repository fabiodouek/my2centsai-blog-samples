"""Build the snapshot every test uses: Node 22 + pinned Claude Code and gh (+ boto3 for the AWS tests).

Run it once: python snapshot/build.py. Takes a few minutes; the build runs in a temporary builder sandbox.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langsmith.sandbox import SandboxClient  # noqa: E402

from common import GiB, SNAPSHOT, banner  # noqa: E402

# The SDK's default HTTP timeout is 10 s, and create_snapshot_from_dockerfile(timeout=...) doesn't
# extend it for the final capture call, which takes minutes for an image this size.
client = SandboxClient(timeout=600)

banner(f"Build snapshot {SNAPSHOT}")
for old in client.list_snapshots(name_contains=SNAPSHOT):
    if old.name == SNAPSHOT:
        client.delete_snapshot(old.id)
snap = client.create_snapshot_from_dockerfile(
    SNAPSHOT,
    Path(__file__).parent / "Dockerfile",
    fs_capacity_bytes=16 * GiB,  # the builder starts from a 16 GiB base
    context=Path(__file__).parent,
    vcpus=2,
    on_build_log=lambda line: print(line, end=""),
    timeout=1800,
)
print(f"\n{snap.name}: {snap.status}")
