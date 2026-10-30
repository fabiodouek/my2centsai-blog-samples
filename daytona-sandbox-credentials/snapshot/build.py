"""Build the snapshot every test uses: Daytona's default image plus pinned Claude Code and gh.

The default snapshot ships an old Claude Code and no gh. Takes a few minutes the first time.
Run it once: python snapshot/build.py
"""

import sys
import time
from pathlib import Path

from daytona import CreateSnapshotParams, Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import SNAPSHOT, banner, daytona  # noqa: E402

CLAUDE_CODE = "2.1.312"
GH = "2.103.3"

image = (
    Image.base("daytonaio/sandbox:0.8.0")
    .dockerfile_commands(["USER root"])
    .run_commands(
    f"npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE}",
    f"curl -sL https://github.com/cli/cli/releases/download/v{GH}/gh_{GH}_linux_amd64.tar.gz"
    " | tar -xz -C /usr/local --strip-components=1",
    )
    .dockerfile_commands(["USER daytona"])
)

banner(f"Build snapshot {SNAPSHOT}")
print(image.dockerfile())
for old in daytona.snapshot.list().items:
    if old.name == SNAPSHOT:
        daytona.snapshot.delete(old)
        while any(s.name == SNAPSHOT for s in daytona.snapshot.list().items):  # delete is async
            time.sleep(2)
snap = daytona.snapshot.create(CreateSnapshotParams(name=SNAPSHOT, image=image), on_logs=print, timeout=0)
print(f"{snap.name}: {snap.state}")
