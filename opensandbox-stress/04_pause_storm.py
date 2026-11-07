"""Pause storm: 20 sandboxes with 100 MB each pause at once to ECR, then resume at once.

Needs part 3's snapshot registry: ../opensandbox-lifecycle/00_snapshot_config.sh (it writes the
ECR token into the Secret opensandbox-snapshot-ecr, valid 12 hours, and upgrades the controller
with the snapshot settings). The script refuses a token that expires within the hour.

Per run (RUNS runs, default 3):
  1. --size sandboxes (default 20, shape 250m/512Mi; with --egress each also gets the egress
     sidecar, which makes two snapshot images per sandbox instead of one)
  2. each writes --mb MiB (default 100) of random data to /root/blob.bin (random, so the
     compressed snapshot layer is as big as the data) and its sha256
  3. pause() on all of them at once. Every 2 s the script reads the server's state of each sandbox
     and its SandboxSnapshot: "durable" is the moment the snapshot reached Succeed (the commit Job
     pushed the image to ECR), "paused" the moment the server reports Paused (the pod is gone)
  4. the commit Jobs' own run times and nodes, and the images in ECR opensandbox-eks/snapshots
     (count and compressed size)
  5. resume on all of them at once: time to a healthy sandbox and to the first command, which
     checks the sha256 of /root/blob.bin (the data came back)
  6. delete the run's sandboxes
The pause and resume windows (UTC) go into the results, so 08_cloudwatch.py can report the bytes
the NAT gateway carried in each: pushes to ECR go through it; image pulls come from S3 through the
VPC's gateway endpoint and do not.

Snapshot images stay in ECR until 99_cleanup.py, which deletes those of the sandboxes recorded in
results/created-sandboxes.txt and no others. Writes results/pause_storm.json and
results/pause_storm.csv. The server is not changed.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from opensandbox import SandboxSync
from opensandbox.models.sandboxes import SandboxFilter
from opensandbox.sync import SandboxManagerSync

from checks import counted
from common import (
    AWS_REGION,
    BATCHSANDBOXES,
    ECHO_HOST,
    RUNS,
    SANDBOX_NAMESPACE,
    SNAPSHOT_REPO_NAME,
    SNAPSHOTS,
    Failures,
    Plan,
    Sampler,
    cli,
    connection,
    create_sandbox,
    delete_run,
    deny_policy,
    describe,
    ensure_port_forward,
    heading,
    kube_json,
    new_run_id,
    node_alias,
    own_metadata,
    parallel,
    preconditions,
    record_created,
    run_in,
    save,
    say,
    show,
    spread,
    wait_no_pods,
)

SCRIPT = "04_pause_storm"
LIMIT = 1800       # seconds to wait for every snapshot to be durable, and for every resume


def arguments(parser) -> None:
    parser.add_argument("--size", type=int, default=20, help="sandboxes per storm (default 20)")
    parser.add_argument("--mb", type=int, default=100, help="MiB of random data per sandbox (default 100)")
    parser.add_argument("--egress", action="store_true", help="give each sandbox the egress sidecar (two images each)")


def utc(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_seconds(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def ecr_images(ids: set[str]) -> tuple[int, int]:
    """(images whose tag starts with one of these sandbox ids, their total compressed bytes)."""
    done = subprocess.run(["aws", "ecr", "describe-images", "--region", AWS_REGION, "--repository-name",
                           SNAPSHOT_REPO_NAME, "--output", "json"], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f"aws ecr describe-images failed: {show(done.stderr.strip()[:160])}")
    details = json.loads(done.stdout).get("imageDetails", [])
    # The tag is "<sandbox id>-<container>-snap-gen<n>" (part 3's values/controller-snapshot.yaml).
    mine = [d for d in details if any(t.startswith(i + "-") for t in d.get("imageTags", []) for i in ids)]
    return len(mine), sum(d.get("imageSizeInBytes", 0) for d in mine)


def snapshot_states(owners: dict[str, str]) -> dict[str, dict]:
    """{sandbox id: {phase, name, node}} of the newest SandboxSnapshot each BatchSandbox owns.
    owners maps BatchSandbox uid -> sandbox id."""
    out: dict[str, dict] = {}
    for snap in kube_json("-n", SANDBOX_NAMESPACE, "get", SNAPSHOTS, timeout=60).get("items", []):
        owner = next((owners[r["uid"]] for r in snap["metadata"].get("ownerReferences", []) if r.get("uid") in owners), None)
        if owner is None:
            continue
        current = out.get(owner)
        if current and current["created"] >= snap["metadata"]["creationTimestamp"]:
            continue
        status = snap.get("status") or {}
        out[owner] = {"phase": status.get("phase", "Pending"), "name": snap["metadata"]["name"],
                      "node": status.get("sourceNodeName", ""), "created": snap["metadata"]["creationTimestamp"]}
    return out


def commit_jobs(snapshots: dict[str, dict]) -> list[dict]:
    """The commit Job of each snapshot: its own run time (startTime to completionTime) and node."""
    jobs = {j["metadata"]["name"]: j for j in kube_json("-n", SANDBOX_NAMESPACE, "get", "jobs", timeout=60).get("items", [])}
    out = []
    for sandbox_id, snap in snapshots.items():
        job = jobs.get(f"{snap['name']}-commit")
        status = (job or {}).get("status", {})
        if status.get("startTime") and status.get("completionTime"):
            out.append({"sandbox": sandbox_id, "seconds": iso_seconds(status["completionTime"]) - iso_seconds(status["startTime"]),
                        "node": node_alias(snap["node"]) if snap.get("node") else ""})
    return out


def one_run(n: int, args, sampler: Sampler, failures: Failures) -> dict:
    run_id = new_run_id()
    meta = own_metadata(SCRIPT, run_id)
    policy = deny_policy(ECHO_HOST) if args.egress else None
    heading(f"storm {n} of {RUNS}: {args.size} sandboxes x {args.mb} MiB (run={run_id})")
    record: dict = {"run": n, "windows": {}, "problems": []}

    def problem(text: str) -> None:
        """A failure in this storm: counted, printed, and the storm is left out of the medians."""
        record["problems"].append(show(text))
        failures.add(f"storm {n}: {text}")

    try:
        def create(_: int) -> str:
            sb = create_sandbox(meta, resource={"cpu": "250m", "memory": "512Mi"}, policy=policy, ttl_minutes=120)
            record_created(sb.id)
            sb.close()
            return sb.id

        made = parallel(create, range(args.size), workers=10)
        ids = [r.value for r in made if r.ok]
        record["create_failed"] = args.size - len(ids)
        if len(ids) < args.size:
            first = next(r for r in made if not r.ok)
            problem(f"{args.size - len(ids)} create(s) failed: {first.error_type}: {(first.error or '')[:120]}")
        say(f"{len(ids)} sandboxes created")

        def fill(sandbox_id: str) -> str:
            sb = SandboxSync.connect(sandbox_id, connection_config=connection("proxy"))
            try:
                out = run_in(sb, f"head -c {args.mb * 1024 * 1024} /dev/urandom > /root/blob.bin && sha256sum /root/blob.bin",
                             timeout=300)
                if out.rc != 0:
                    raise RuntimeError(f"fill failed: {out.text[-120:]}")
                return out.text.split()[0]
            finally:
                sb.close()

        filled = parallel(fill, ids, workers=10)
        digests = {r.item: r.value for r in filled if r.ok}
        say(f"{len(digests)} sandboxes hold {args.mb} MiB of random data each")

        bs = kube_json("-n", SANDBOX_NAMESPACE, "get", BATCHSANDBOXES, timeout=60)
        owners = {b["metadata"]["uid"]: b["metadata"]["name"] for b in bs.get("items", []) if b["metadata"]["name"] in ids}

        def pause(sandbox_id: str) -> None:
            with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
                manager.pause_sandbox(sandbox_id)

        with sampler.phase(f"pause {n}"):
            record["windows"]["pause_start"] = utc()
            started = time.monotonic()
            accepted = parallel(pause, ids, workers=len(ids), together=True)
            record["pause_accept_ms"] = describe("pause accepted", [r.ms for r in accepted if r.ok])
            durable: dict[str, float] = {}
            paused: dict[str, float] = {}
            timeline = []
            with SandboxManagerSync.create(connection_config=connection("proxy")) as manager:
                while time.monotonic() - started < LIMIT:
                    now = time.monotonic() - started
                    for sid, snap in snapshot_states(owners).items():
                        if snap["phase"] == "Succeed":
                            durable.setdefault(sid, now)
                        elif snap["phase"] == "Failed" and sid not in durable:
                            durable[sid] = float("nan")
                    ensure_port_forward()
                    infos = manager.list_sandbox_infos(SandboxFilter(metadata={"run": run_id}, page_size=100)).sandbox_infos
                    for info in infos:
                        if str(info.status.state) == "Paused":
                            paused.setdefault(info.id, now)
                    timeline.append([round(now, 1), len([v for v in durable.values() if not math.isnan(v)]), len(paused)])
                    if len(durable) >= len(ids) and len(paused) >= len(ids):
                        break
                    time.sleep(2)
            record["windows"]["pause_end"] = utc()
        failed = [sid for sid, t in durable.items() if math.isnan(t)]
        record["snapshot_failed"] = len(failed)
        record["snapshot_not_durable"] = len(ids) - len([t for t in durable.values() if not math.isnan(t)])
        if failed:
            problem(f"{len(failed)} snapshot(s) Failed")
        elif record["snapshot_not_durable"]:
            problem(f"{record['snapshot_not_durable']} snapshot(s) not durable within {LIMIT} s")
        ok_durable = [t for t in durable.values() if not math.isnan(t)]
        record.update({"durable_s": sorted(round(t, 1) for t in ok_durable),
                       "all_durable_s": round(max(ok_durable), 1) if len(ok_durable) == len(ids) else None,
                       "first_durable_s": round(min(ok_durable), 1) if ok_durable else None,
                       "all_paused_s": round(max(paused.values()), 1) if len(paused) == len(ids) else None,
                       "timeline": timeline})
        say(f"durable: first after {record['first_durable_s']} s, all {len(ok_durable)} after {record['all_durable_s']} s; "
            f"all Paused after {record['all_paused_s']} s")
        jobs = commit_jobs(snapshot_states(owners))
        record["commit_job_s"] = describe("commit Job", [1000 * j["seconds"] for j in jobs])
        record["commit_jobs_per_node"] = {node: sum(1 for j in jobs if j["node"] == node) for node in sorted({j["node"] for j in jobs})}
        count, size = ecr_images(set(ids))
        record["images"] = {"count": count, "bytes": size, "expected": len(ids) * (2 if args.egress else 1)}
        say(f"ECR: {count} image(s) for this storm, {size / 1e9:.2f} GB compressed")
        if count != record["images"]["expected"]:
            problem(f"{count} snapshot images in ECR, expected {record['images']['expected']}")

        def resume(sandbox_id: str) -> dict:
            started = time.monotonic()
            sb = SandboxSync.resume(sandbox_id, connection_config=connection("proxy"),
                                    resume_timeout=timedelta(seconds=LIMIT))
            healthy = time.monotonic() - started
            try:
                out = run_in(sb, "sha256sum /root/blob.bin", timeout=120)
                first = time.monotonic() - started
                return {"healthy_ms": 1000 * healthy, "command_ms": 1000 * first,
                        "data_ok": out.rc == 0 and out.text.split()[:1] == [digests.get(sandbox_id)]}
            finally:
                sb.close()

        with sampler.phase(f"resume {n}"):
            record["windows"]["resume_start"] = utc()
            ensure_port_forward()
            resumed = parallel(resume, ids, workers=len(ids), together=True)
            record["windows"]["resume_end"] = utc()
        good = [r.value for r in resumed if r.ok]
        record["resume_healthy_ms"] = describe("resume to healthy", [g["healthy_ms"] for g in good])
        record["resume_command_ms"] = describe("resume to first command", [g["command_ms"] for g in good])
        record["data_ok"] = sum(1 for g in good if g["data_ok"])
        record["resume_failed"] = len(resumed) - len(good)
        record["resume_errors"] = sorted({show(f"{r.error_type}: {(r.error or '')[:120]}") for r in resumed if not r.ok})[:3]
        if record["resume_failed"]:
            problem(f"{record['resume_failed']} resume(s) failed (not counted in the resume times): "
                    f"{record['resume_errors'][:1]}")
        say(f"resumed {len(good)}: healthy p50 {record['resume_healthy_ms']['p50']} ms, p95 {record['resume_healthy_ms']['p95']}; "
            f"data intact in {record['data_ok']}")
        if record["data_ok"] != len(good):
            problem(f"the data came back in {record['data_ok']} of {len(good)} resumed sandboxes")
    except Exception as error:  # noqa: BLE001 - one line, then clean up
        problem(f"{type(error).__name__}: {str(error)[:200]}")
    finally:
        delete_run(run_id)
        wait_no_pods(f"run={run_id}", timeout=600)
    return record


def main() -> int:
    args = cli(__doc__, arguments)
    plan = Plan(f"04_pause_storm.py: {args.size} sandboxes x {args.mb} MiB pause at once, then resume, x{RUNS}")
    plan.checks += ["no sandboxes and no Pools; server runc + base egress + dns+nft + gateway; 2 nodes Ready",
                    "the snapshot Secret's ECR token is accepted by ECR and valid for at least an hour "
                    "(run ../opensandbox-lifecycle/00_snapshot_config.sh first)"]
    for n in range(1, RUNS + 1):
        plan.step(f"storm {n}: create {args.size}, write {args.mb} MiB each, pause all, wait until every snapshot "
                  f"is durable in ECR, resume all, check the data, delete", 5)
    plan.writes += ["results/pause_storm.json", "results/pause_storm.csv", "results/created-sandboxes.txt",
                    f"{RUNS * args.size * (2 if args.egress else 1)} snapshot images in ECR (99_cleanup.py deletes them)"]
    plan.print()
    if args.dry_run:
        return 0

    preconditions(snapshots=True)
    failures = Failures()
    with Sampler("pause_storm", every=2.0) as sampler:
        runs = [one_run(n, args, sampler, failures) for n in range(1, RUNS + 1)]
        phases = sampler.phase_list()
    for run in runs:
        run["failed"] = len(run.get("problems", []))
    use, flagged = counted(runs)
    results = {"size": args.size, "mb": args.mb, "egress": args.egress, "runs": runs, "phases": phases,
               "storms_counted": [r["run"] for r in use], "median_includes_failed_storms": flagged,
               "all_durable_s": spread([r.get("all_durable_s") for r in use]),
               "all_paused_s": spread([r.get("all_paused_s") for r in use]),
               "resume_healthy_p50_ms": spread([(r.get("resume_healthy_ms") or {}).get("p50") for r in use]),
               "resume_command_p95_ms": spread([(r.get("resume_command_ms") or {}).get("p95") for r in use]),
               "repository": f"<registry>/{SNAPSHOT_REPO_NAME}"}
    save("pause_storm", results)
    heading("pause storm")
    print(f"  every snapshot durable after {results['all_durable_s']['median']} s (median of {RUNS}, spread "
          f"{results['all_durable_s']['spread']} s); all Paused after {results['all_paused_s']['median']} s")
    print(f"  resume to healthy p50 {results['resume_healthy_p50_ms']['median']} ms (median of the runs' p50)")
    left_out = [r["run"] for r in runs if r["failed"]]
    if flagged:
        print("  * every storm had a failure: these medians include them")
    elif left_out:
        print(f"  storms left out of the medians (a failure in them): {left_out}")
    print("  NAT gateway bytes for these windows: uv run python 08_cloudwatch.py")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
