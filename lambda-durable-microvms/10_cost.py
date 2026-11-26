"""What the runs cost, from the usage records: durable operations, Lambda seconds and
MicroVM seconds, priced at the published us-east-1 rates (Cost Explorer lags about a day).

Per demo, from results/*.json; then every MicroVM this image has launched (list-microvms
keeps terminated MicroVMs for a while), which is the real MicroVM compute bill so far, and
the snapshot reads those launches add. The price list bills snapshot reads "in GB of memory
and disk used by snapshot"; GetMicrovmImageBuild reports the memory, disk and code-install
sizes, so the script prints a range: memory + disk, and memory + disk + code install.
"""

import json

from common import (RATE_DURABLE_OP, RATE_LAMBDA_ARM_GB_S, RESULTS, banner, image_arn, list_microvms, microvm,
                    microvm_cost, mvm, save, say, ts)

RATE_SNAPSHOT_READ_GB = 0.00155


def snapshot_gb() -> tuple[float, float]:
    """(memory + disk, memory + disk + code install) of the image's latest build, in GB."""
    img = mvm.get_microvm_image(imageIdentifier=image_arn())
    version = img["latestActiveImageVersion"]
    builds = mvm.list_microvm_image_builds(imageIdentifier=image_arn(), imageVersion=version)["items"]
    b = mvm.get_microvm_image_build(imageIdentifier=image_arn(), imageVersion=version,
                                    buildId=builds[0]["buildId"])["snapshotBuild"]
    low = (b["memorySnapshotSizeInBytes"] + b["diskSnapshotSizeInBytes"]) / 1e9
    return low, low + b["codeInstallSizeInBytes"] / 1e9


LAMBDA_GB = 0.25  # the function's 256 MB


def summaries(obj, path=""):
    """Every execution summary inside a results file, with a label."""
    if isinstance(obj, dict):
        if "summary" in obj and isinstance(obj["summary"], dict) and "billed_operations" in obj["summary"]:
            yield path or "run", obj
        for k, v in obj.items():
            if k not in ("summary", "result"):
                yield from summaries(v, f"{path}/{k}" if path else k)


def vm_seconds(run: dict) -> float:
    if "microvms" in run:
        return sum(m.get("seconds") or 0 for m in run["microvms"])
    if "lifetime_seconds" in run:
        return run.get("running_seconds", run["lifetime_seconds"])
    return 0.0


def main() -> None:
    banner("Cost: orchestration vs sandbox")
    rows = []
    for f in sorted(RESULTS.glob("0[2-9]_*.json")):
        data = json.loads(f.read_text())
        for label, run in summaries(data):
            s = run["summary"]
            ops, lam_s, vm_s = s["billed_operations"], s["lambda_seconds"], vm_seconds(run)
            rows.append({"demo": f"{f.stem}:{label}", "operations": ops, "lambda_seconds": lam_s,
                         "microvm_seconds": round(vm_s, 1), "ops_usd": ops * RATE_DURABLE_OP,
                         "lambda_usd": lam_s * LAMBDA_GB * RATE_LAMBDA_ARM_GB_S, "microvm_usd": microvm_cost(vm_s)})
    say(f"{'run':<34} {'ops':>4} {'Lambda s':>9} {'MicroVM s':>10} {'ops $':>9} {'Lambda $':>9} {'MicroVM $':>10}")
    for r in rows:
        say(f"{r['demo'][:34]:<34} {r['operations']:>4} {r['lambda_seconds']:>9.2f} {r['microvm_seconds']:>10.1f} "
            f"{r['ops_usd']:>9.6f} {r['lambda_usd']:>9.6f} {r['microvm_usd']:>10.5f}")
    tot = {k: sum(r[k] for r in rows) for k in ("operations", "lambda_seconds", "microvm_seconds", "ops_usd",
                                                "lambda_usd", "microvm_usd")}
    say(f"{'total (these runs)':<34} {tot['operations']:>4} {tot['lambda_seconds']:>9.2f} "
        f"{tot['microvm_seconds']:>10.1f} {tot['ops_usd']:>9.6f} {tot['lambda_usd']:>9.6f} {tot['microvm_usd']:>10.5f}")

    vms = list_microvms()
    secs = 0.0
    for v in vms:
        d = microvm(v["microvmId"])
        if d.get("terminatedAt"):
            secs += ts(d["terminatedAt"]) - ts(d["startedAt"])
    say(f"\nevery MicroVM of the image so far: {len(vms)} MicroVMs, {secs / 60:.1f} minutes, "
        f"${microvm_cost(secs):.4f} of compute")
    low, high = snapshot_gb()
    say(f"snapshot read per launch: {low:.2f} to {high:.2f} GB, ${low * RATE_SNAPSHOT_READ_GB:.4f} to "
        f"${high * RATE_SNAPSHOT_READ_GB:.4f}; for {len(vms)} launches ${len(vms) * low * RATE_SNAPSHOT_READ_GB:.3f} "
        f"to ${len(vms) * high * RATE_SNAPSHOT_READ_GB:.3f} (suspends, resumes and image storage not included)")
    save("10_cost", {"rows": rows, "totals": tot, "all_microvms": {"count": len(vms), "seconds": round(secs, 1),
                                                                  "usd": round(microvm_cost(secs), 4)},
                     "snapshot_read_gb": [round(low, 3), round(high, 3)]})


if __name__ == "__main__":
    main()
