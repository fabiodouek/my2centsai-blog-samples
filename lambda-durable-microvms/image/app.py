"""The code inside the MicroVM image: the compute worker and the jobs it runs.

Lambda starts this process when it builds the image and snapshots it after the
ready hook. Every MicroVM the durable function launches resumes that snapshot,
so the worker is already listening when a job arrives (in the run hook, or over
HTTPS for session jobs). Each job's return value becomes the result of the
durable operation; an exception fails it with MicrovmJobFailedError.

Jobs (picked by job_input["kind"] for the default handler):
  scan    clone a public repo and count files, lines and secret-looking strings
  sleep   run for job_input["seconds"], checking for cancellation
  fail    raise, like a failing test suite would
  crash   kill this process mid-job, so heartbeats stop
  flaky   fail on attempt 0, succeed after that
Session routes (06_session.py):
  /build  clone and pip install a repo, and remember it in memory
  /test   run the repo's tests, using what /build left on disk and in memory
"""

import collections
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from aws_durable_execution_sdk_python_compute_worker.microvm import (
    MicrovmJobContext,
    start_microvm_worker,
)

WORK = Path("/work")
# Module-level state: lives as long as this process, so a session's later jobs see it
MEMORY: dict = {"booted_pid": os.getpid()}

SECRET_PATTERNS = {
    "aws_access_key_id": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "password_assignment": re.compile(r"(?i)\bpassword\s*=\s*['\"][^'\"]{4,}['\"]"),
}


def _clone(repo: str) -> tuple[Path, float]:
    dest = WORK / repo.replace("/", "__")
    t0 = time.time()
    if not dest.exists():
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", f"https://github.com/{repo}.git", str(dest)],
                       check=True, capture_output=True, timeout=300)
    return dest, round(time.time() - t0, 2)


def scan(job: dict, ctx: MicrovmJobContext) -> dict:
    started = time.time()
    src, clone_s = _clone(job["repo"])
    files, lines = 0, 0
    by_ext: collections.Counter = collections.Counter()
    findings: collections.Counter = collections.Counter()
    for p in src.rglob("*"):
        if ".git" in p.parts or not p.is_file():
            continue
        files += 1
        by_ext[p.suffix or "(none)"] += 1
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        lines += text.count("\n")
        for name, rx in SECRET_PATTERNS.items():
            findings[name] += len(rx.findall(text))
    return {
        "repo": job["repo"],
        "files": files,
        "lines": lines,
        "top_extensions": dict(by_ext.most_common(3)),
        "secret_like_strings": dict(findings),  # counts only, never the matches
        "clone_seconds": clone_s,
        "job_seconds": round(time.time() - started, 2),
        "microvm_id": ctx.microvm_id,
        "started_at": started,
    }


def sleep(job: dict, ctx: MicrovmJobContext) -> dict:
    started = time.time()
    target = float(job["seconds"])
    while time.time() - started < target:
        if ctx.cancelled.wait(5):  # set on heartbeat-timeout answers and the terminate hook
            return {"cancelled": True, "ran_seconds": round(time.time() - started, 1)}
    return {"ran_seconds": round(time.time() - started, 1), "microvm_id": ctx.microvm_id, "started_at": started}


def fail(job: dict, ctx: MicrovmJobContext) -> dict:
    raise RuntimeError(f"3 of 412 tests failed in {job.get('repo', 'the repo')}")


def crash(job: dict, ctx: MicrovmJobContext) -> dict:
    time.sleep(float(job.get("after", 5)))
    logging.getLogger("demo").error("crash job: killing the worker process now")
    os._exit(1)  # no result, no more heartbeats


def flaky(job: dict, ctx: MicrovmJobContext) -> dict:
    if int(job["attempt"]) < 1:
        raise RuntimeError(f"flaky failure on attempt {job['attempt']}")
    return {"attempt": job["attempt"], "microvm_id": ctx.microvm_id}


KINDS = {"scan": scan, "sleep": sleep, "fail": fail, "crash": crash, "flaky": flaky}


def default(job: dict, ctx: MicrovmJobContext) -> dict:
    return KINDS[job["kind"]](job, ctx)


def build(job: dict, ctx: MicrovmJobContext) -> dict:
    started = time.time()
    src, clone_s = _clone(job["repo"])
    pip = subprocess.run(["pip", "install", "--quiet", "--no-cache-dir", "--root-user-action=ignore", str(src)],
                         capture_output=True, text=True, timeout=600)
    if pip.returncode:
        raise RuntimeError(f"pip install failed: {pip.stderr[-300:]}")
    commit = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    MEMORY.update(repo=job["repo"], commit=commit, built_at=started, build_pid=os.getpid())
    return {"repo": job["repo"], "commit": commit, "clone_seconds": clone_s,
            "job_seconds": round(time.time() - started, 2), "pid": os.getpid(), "microvm_id": ctx.microvm_id}


def test(job: dict, ctx: MicrovmJobContext) -> dict:
    started = time.time()
    src = WORK / job["repo"].replace("/", "__")
    on_disk = src.exists()
    r = subprocess.run(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(src / "tests")],
                       capture_output=True, text=True, timeout=1200, cwd="/tmp")
    summary = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    return {"repo_on_disk": on_disk, "memory": dict(MEMORY), "pid": os.getpid(),
            "pytest_exit": r.returncode, "pytest_summary": summary,
            "job_seconds": round(time.time() - started, 2), "microvm_id": ctx.microvm_id}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    WORK.mkdir(exist_ok=True)
    worker = start_microvm_worker(handler=default, routes={"/build": build, "/test": test})
    worker.wait()


if __name__ == "__main__":
    main()
