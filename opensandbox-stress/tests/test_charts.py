"""09_charts.py on made-up results in the shape the scripts write: every chart comes out as a light
and a dark PNG at 2x, and a missing results file only skips its charts."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("charts", ROOT / "09_charts.py")
charts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(charts)


def sp(median, *runs):
    return {"median": median, "min": min(runs or [median]), "max": max(runs or [median]), "spread": 0,
            "spread_pct": 0, "runs": list(runs or [median])}


def d(p50, p95=None, p99=None, mean=None):
    return {"label": "x", "n": 30, "min": p50 / 2, "p50": p50, "p95": p95 or p50 * 1.5, "p99": p99 or p50 * 2,
            "max": p50 * 3, "mean": mean or p50 * 1.1}


def batch(running, mem):
    return {"running": running, "node_totals": {"node-a": {"mem_mib": mem}, "node-b": {"mem_mib": mem * 0.9}}}


def write_results(folder: Path) -> None:
    folder.mkdir()
    modes = {}
    for k, ceiling, mem in (("runc", 88, 20), ("runc-egress", 88, 95), ("kata", 40, 290)):
        runs = [{"ceiling": ceiling, "batches": [batch(n, 2000 + n * mem) for n in range(10, ceiling + 1, 10)]}]
        modes[k] = {"ceiling": sp(ceiling), "limit": "pods" if k != "kata" else "cpu", "sandbox_mem_mib": sp(mem),
                    "node_growth_mib": sp(mem * 1.9), "runs": runs}
    modes["kata"]["runtimeclass_overhead"] = {"cpu": "250m", "memory": "160Mi"}
    modes["kata"]["node_side"] = {"kubelet_mem_mib": sp(6.0), "vmm_total_mib": sp(324.0), "gap_mib": sp(318.0)}
    (folder / "density.json").write_text(json.dumps({"shape": {"cpu": "100m", "memory": "128Mi"}, "batch": 10,
                                                     "runs_per_mode": 3, "modes": modes}))
    levels = [1, 5, 10, 20, 40]
    (folder / "latency.json").write_text(json.dumps({
        "levels": levels, "shape": {"cpu": "250m", "memory": "512Mi"}, "min_samples": 30,
        "cold": {str(n): {"describe": d(2200 + 40 * n)} for n in levels},
        "warm": {str(n): {"describe": d(1300 + 10 * n)} for n in levels},
        "delivery": {"n": 84, "controller_allocated_ms": sp(1800.0), "api_wall_ms": sp(9500.0)}}))
    (folder / "pause_storm.json").write_text(json.dumps({"size": 20, "mb": 100, "runs": [
        {"run": r, "timeline": [[t, min(20, max(0, (t - 20) // 2)), 0] for t in range(0, 80, 2)],
         "resume_command_ms": d(6000 + 300 * r)} for r in (1, 2, 3)]}))
    rungs = {}
    for i, rung in enumerate(("none", "dns", "dns+nft", "mitm", "vault")):
        rungs[rung] = {"sequential": d(1.5 + i), "sequential_p50_ms": sp(1.5 + i, 1.5, 1.6, 1.4),
                       "load_rps": sp(900 - 120 * i), "sidecar_peak_rss_mib": sp(None if rung == "none" else 20 + 25 * i),
                       "target_bound_runs": 1 if rung == "none" else 0}
    rungs["aws-floor"] = {"sequential": d(8.0)}
    rungs["sigv4"] = {"sequential": d(21.0)}
    records = [{"rung": rung, "run": n, "lookup": {"p50": 0.3 + 15 * i, "p95": 0.5 + 25 * i}}
               for i, rung in enumerate(("none", "dns", "dns+nft", "mitm", "vault")) for n in (1, 2, 3)]
    (folder / "ladder.json").write_text(json.dumps({"summary": rungs, "sequential": 500, "concurrency": 16,
                                                    "duration_s": 20, "records": records}))
    cached = {f"{rung}/cached": {"load_rps": sp(rps), "target_bound_runs": 1 if rps > 5000 else 0,
                                 "proxy_bound_runs": 3 if rps < 500 else 0}
              for rung, rps in (("none", 5700.0), ("dns+nft", 5600.0), ("mitm", 385.0), ("vault", 359.0))}
    (folder / "ladder-resolve-once.json").write_text(json.dumps({"summary": cached, "concurrency": 16}))
    (folder / "control_plane.json").write_text(json.dumps({
        "phases": [{"name": "api x16 run 1", "start": 40, "end": 60}, {"name": "create x20 run 1", "start": 100, "end": 110}],
        "ramp_by_level": {str(n): {"rps": sp(100.0 * min(n, 8)), "p99_ms": sp(10.0 * n)} for n in (1, 4, 16, 64)}}))
    with (folder / "control_plane.csv").open("w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["t", "node", "pod", "cpu_m", "mem_mib"])
        for t in range(0, 140, 2):
            w.writerow([t, "node-a", "opensandbox-server-abc", 900 if 40 <= t <= 60 else 20, 300])
            w.writerow([t, "node-b", "opensandbox-controller-manager-def", 200 if 100 <= t <= 110 else 5, 60])
            w.writerow([t, "node-a", "_node", 1000, 4000])
    summary = {}
    for rt, factor in (("runc", 1.0), ("kata", 1.2)):
        summary[rt] = {sc: {"cpu_ms": d(300 * factor * f), "io_ms": d(600 * factor * g), "fsync_ms": d(3 * g)}
                       for sc, f, g in (("baseline", 1, 1), ("cpu", 1.1, 1), ("disk", 1, 4))}
    (folder / "noisy.json").write_text(json.dumps({"summary": summary, "victims": 4, "hogs": 8,
                                                   "victim_shape": {"cpu": "1", "memory": "1Gi"},
                                                   "hog_requests": {"cpu": "100m"}, "hog_limits": {"cpu": "2"}}))
    ts = [f"2026-11-01T10:{m:02d}:00Z" for m in range(0, 30)]
    (folder / "cloudwatch.json").write_text(json.dumps({
        "window": {"start": ts[0], "end": ts[-1]},
        "nodes": {"node-a": {"node_cpu_utilization": {"t": ts, "v": [20 + m for m in range(30)]}},
                  "node-b": {"node_cpu_utilization": {"t": ts, "v": [15 + m for m in range(30)]}}},
        "nat": {"nat-1": {"BytesOutToDestination": {"t": ts, "v": [1e6 * (400 if m == 12 else 1) for m in range(30)]}}},
        "pause_storm_nat": [{"run": 1, "pause_window": ["2026-11-01T10:12:00Z", "2026-11-01T10:13:00Z"]}]}))


EXPECTED = ["density-ceiling", "density-memory", "density-kata-memory", "density-per-sandbox", "latency-create",
            "latency-delivery", "pause-durable", "pause-resume", "ladder-latency", "ladder-throughput", "ladder-lookup",
            "ladder-sidecar-rss", "ladder-sigv4", "control-plane-cpu", "control-plane-rps", "control-plane-p99",
            "noisy-cpu", "noisy-io", "noisy-fsync", "cloudwatch-node-cpu", "cloudwatch-nat"]


@pytest.fixture(scope="module")
def drawn(tmp_path_factory):
    base = tmp_path_factory.mktemp("charts")
    write_results(base / "results")
    assert charts.main(["--results", str(base / "results"), "--charts", str(base / "charts")]) == 0
    return base / "charts"


def test_every_chart_in_both_themes(drawn):
    names = sorted(p.name for p in drawn.glob("*.png"))
    assert names == sorted(f"{n}-{mode}.png" for n in EXPECTED for mode in ("light", "dark"))


def test_charts_are_2x(drawn):
    from PIL import Image
    with Image.open(drawn / "latency-create-light.png") as img:
        assert img.size == (1600, 900)


def test_dark_and_light_differ(drawn):
    assert (drawn / "noisy-io-light.png").read_bytes() != (drawn / "noisy-io-dark.png").read_bytes()


def test_no_results_draws_nothing(tmp_path):
    (tmp_path / "results").mkdir()
    assert charts.main(["--results", str(tmp_path / "results"), "--charts", str(tmp_path / "charts")]) == 0
    assert not list((tmp_path / "charts").glob("*.png"))
