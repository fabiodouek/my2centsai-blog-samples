"""probes/: each probe's --dry-run prints its plan and makes no subprocess, exec or socket call (an
audit hook watches the whole run, imports included), and writes nothing. Needs ../opensandbox-eks/env.sh
and the venv, like the scripts' own dry-run tests; no cluster."""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBES = sorted((ROOT / "probes").glob("*.py"))
needs_env = pytest.mark.skipif(not (ROOT.parent / "opensandbox-eks" / "env.sh").is_file(),
                               reason="../opensandbox-eks/env.sh is not here")

AUDITED = textwrap.dedent("""
    import json, runpy, sys
    WATCH = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
             "socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.bind")
    seen = []
    sys.addaudithook(lambda event, args: seen.append(event) if event in WATCH else None)
    path = sys.argv[1]
    sys.argv = [path, "--dry-run"]
    code = 0
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as stop:
        code = stop.code or 0
    print("@@audit " + json.dumps({"calls": seen, "code": code}))
""")


def test_there_are_four_probes():
    assert [p.name for p in PROBES] == ["kata_ceiling.py", "kata_plain.py", "kata_probe.py", "nft_probe.py"]


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_no_local_paths_or_session_notes(probe):
    text = probe.read_text()
    assert not re.search(r"/Users/|/home/|/private/tmp|scratchpad|<this file>", text)


@needs_env
@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_dry_run_calls_nothing_and_writes_nothing(probe, tmp_path):
    logs = tmp_path / "logs"
    before = {p: p.stat().st_mtime_ns for p in (ROOT / "results").glob("*")}
    done = subprocess.run([sys.executable, "-c", AUDITED, str(probe)], capture_output=True, text=True, cwd=str(ROOT),
                          timeout=120, env={**__import__("os").environ, "OSB_STRESS_LOGS": str(logs)})
    audit = json.loads(next(line for line in done.stdout.splitlines() if line.startswith("@@audit "))[8:])
    assert audit == {"calls": [], "code": 0}, done.stdout[-500:] + done.stderr[-500:]
    assert "estimated wall clock" in done.stdout
    assert not logs.exists() or not any(logs.rglob("*"))                    # no log written
    assert {p: p.stat().st_mtime_ns for p in (ROOT / "results").glob("*")} == before
