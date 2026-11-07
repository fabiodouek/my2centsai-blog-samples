"""Every script's --dry-run prints its plan and calls none of kubectl, helm, terraform, aws or docker.

The scripts run with those five commands replaced by stand-ins that record the call and fail, so a
dry run that touched the cluster or AWS would leave a mark. Needs ../opensandbox-eks/env.sh (the
scripts read the cluster's names from it) and the venv (common.py imports the SDK)."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["02_density.py", "03_latency.py", "04_pause_storm.py", "05_egress_ladder.py", "06_control_plane.py",
           "07_noisy.py", "08_cloudwatch.py", "99_cleanup.py"]
pytestmark = pytest.mark.skipif(not (ROOT.parent / "opensandbox-eks" / "env.sh").is_file(),
                                reason="../opensandbox-eks/env.sh is not here")


@pytest.fixture
def trap(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marks = tmp_path / "calls"
    for tool in ("kubectl", "helm", "terraform", "aws", "docker"):
        path = bin_dir / tool
        path.write_text(f"#!/bin/sh\necho \"{tool} $*\" >> '{marks}'\nexit 1\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return env, marks


@pytest.mark.parametrize("script", SCRIPTS)
def test_python_dry_run(script, trap):
    env, marks = trap
    done = subprocess.run([sys.executable, str(ROOT / script), "--dry-run"], capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=120)
    assert done.returncode == 0, done.stderr
    assert "estimated wall clock" in done.stdout
    assert not marks.exists(), marks.read_text()


@pytest.mark.parametrize("args, files", [
    (["05_egress_ladder.py", "--resolve-once"], ["ladder-resolve-once.json", "ladder-resolve-once.csv", "ladder.json"]),
    (["05_egress_ladder.py", "--resolve-once", "--resummarize"], ["ladder-resolve-once.json", "ladder.json"]),
    (["02_density.py", "--resummarize"], ["density.json"]),
    (["08_cloudwatch.py", "--resummarize"], ["cloudwatch.json"]),
    (["07_noisy.py", "--runtimes", "runc,kata"], ["noisy.json"]),
])
def test_variant_dry_runs_write_and_call_nothing(args, files, trap):
    """The variants' --dry-run: the plan (or the recomputed report), no cluster or AWS call, and none
    of the script's results files written (only those: a run in flight may be writing others)."""
    env, marks = trap

    def stamps() -> dict:
        return {name: (ROOT / "results" / name).stat().st_mtime_ns if (ROOT / "results" / name).exists() else None
                for name in files}

    before = stamps()
    done = subprocess.run([sys.executable, str(ROOT / args[0]), *args[1:], "--dry-run"], capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=120)
    assert done.returncode == 0, done.stderr
    assert not marks.exists(), marks.read_text()
    assert stamps() == before


def test_resolve_once_refuses_the_sigv4_rung(trap):
    env, marks = trap
    done = subprocess.run([sys.executable, str(ROOT / "05_egress_ladder.py"), "--resolve-once", "--rungs", "dns,sigv4",
                           "--dry-run"], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    assert done.returncode != 0 and "--resolve-once" in done.stderr and not marks.exists()


@pytest.mark.parametrize("args", [["--dry-run"], ["off", "--dry-run"]])
def test_metrics_dry_run(args, trap):
    env, marks = trap
    done = subprocess.run([str(ROOT / "01_metrics.sh"), *args], capture_output=True, text=True, env=env,
                          cwd=str(ROOT), timeout=60)
    assert done.returncode == 0, done.stderr
    assert "tfplan.py check" in done.stdout
    assert not marks.exists()


@pytest.mark.parametrize("script", SCRIPTS + ["09_charts.py"])
def test_help(script):
    done = subprocess.run([sys.executable, str(ROOT / script), "--help"], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=120)
    assert done.returncode == 0 and "usage:" in done.stdout
