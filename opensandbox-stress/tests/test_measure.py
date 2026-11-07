"""measure.py: statistics, parallel(), save(), the CSV writer and the Sampler, against a recorded
kubelet stats/summary (tests/fixtures/stats-summary.json: one node of the cluster, anonymized,
plus two sandbox pods in the same shape). No cluster, no SDK."""

import csv
import json
import math
import threading
import time
from pathlib import Path

import pytest

import measure
from measure import (
    CSV_HEADER,
    CsvSink,
    NodeAliases,
    Sampler,
    container_rows,
    describe,
    leaks,
    mask,
    node_growth,
    node_growth_span,
    parallel,
    pct,
    pod_stats,
    read_csv,
    save,
    spread,
    storm_nat,
    summary_rows,
    window_stats,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stats-summary.json"
SANDBOX = "11111111-1111-4111-8111-111111111111-0"
FRESH = "22222222-2222-4222-8222-222222222222-0"


@pytest.fixture
def summary() -> dict:
    return json.loads(FIXTURE.read_text())


# --- pct, describe, spread ---------------------------------------------------------------------

def test_pct_nearest_rank():
    values = list(range(1, 101))
    assert pct(values, 50) == 50
    assert pct(values, 95) == 95
    assert pct(values, 99) == 99
    assert pct(values, 100) == 100
    assert pct(values, 0) == 1


def test_pct_small_lists_report_a_measured_value():
    assert pct([10, 20], 50) == 10          # lower middle, no interpolation
    assert pct([10, 20], 51) == 20
    assert pct([7], 99) == 7
    assert pct([3, 1, 2], 50) == 2          # unsorted input


def test_pct_does_not_mutate_and_skips_none():
    values = [3, None, 1, 2]
    assert pct(values, 100) == 3
    assert values == [3, None, 1, 2]


def test_pct_empty_and_bad_p():
    assert math.isnan(pct([], 50))
    with pytest.raises(ValueError):
        pct([1, 2], 101)


def test_describe():
    d = describe("create", [100.04, 200.0, 300.0, 400.0])
    assert d == {"label": "create", "n": 4, "min": 100.0, "p50": 200.0, "p95": 400.0, "p99": 400.0, "max": 400.0,
                 "mean": 250.0, "p99_is_max": True}
    assert describe("x", list(range(1, 201)))["p99_is_max"] is False


def test_describe_empty():
    d = describe("none", [])
    assert d["n"] == 0 and d["p50"] is None and d["mean"] is None


def test_spread_median_and_width():
    s = spread([10.0, 14.0, 12.0])
    assert s["median"] == 12.0 and s["min"] == 10.0 and s["max"] == 14.0 and s["spread"] == 4.0
    assert s["spread_pct"] == pytest.approx(33.3)
    assert s["runs"] == [10.0, 14.0, 12.0]


def test_spread_keeps_missing_runs_out_of_the_numbers():
    s = spread([5.0, None, 7.0])
    assert s["median"] == 6.0 and s["runs"] == [5.0, None, 7.0]
    assert spread([None])["median"] is None


# --- parallel ------------------------------------------------------------------------------------

def test_parallel_keeps_order_and_catches_errors():
    def fn(x):
        if x == 2:
            raise RuntimeError("boom")
        time.sleep(0.01 * (5 - x))
        return x * 10

    results = parallel(fn, range(5), workers=5)
    assert [r.item for r in results] == [0, 1, 2, 3, 4]
    assert [r.value for r in results] == [0, 10, None, 30, 40]
    assert results[2].error == "boom" and results[2].error_type == "RuntimeError" and not results[2].ok
    assert all(r.end >= r.start for r in results)


def test_parallel_respects_the_worker_count():
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def fn(_):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.02)
        with lock:
            state["now"] -= 1

    parallel(fn, range(12), workers=3)
    assert state["peak"] <= 3


def test_parallel_together_starts_the_first_wave_at_once():
    results = parallel(lambda _: time.sleep(0.05), range(8), workers=8, together=True)
    starts = [r.start for r in results]
    assert max(starts) - min(starts) < 0.04


def test_parallel_edge_cases():
    assert parallel(lambda x: x, [], workers=4) == []
    with pytest.raises(ValueError):
        parallel(lambda x: x, [1], workers=0)


# --- what may be written ----------------------------------------------------------------------------

def test_mask_and_leaks():
    text = "pushed 123456789012.dkr.ecr.us-east-1.amazonaws.com/opensandbox-eks/x from 10.0.0.7 in /Users/someone/x"
    masked = mask(text, registry="123456789012.dkr.ecr.us-east-1.amazonaws.com")
    assert masked == "pushed <registry>/opensandbox-eks/x from <ip> in ~/x"
    assert leaks(masked) == []
    assert set(leaks(text)) == {"a 12-digit number (account ID)", "an IPv4 address", "a home directory path"}
    assert leaks("release-1.2.0 and v1.37.0-eksbuild.32") == []


def test_save_writes_clean_results(tmp_path):
    path = save("density", {"nodes": {"node-a": 1}, "bytes": 107294470144}, results=tmp_path / "results")
    assert json.loads(path.read_text())["bytes"] == 107294470144   # a 12-digit number, but not a string


def test_save_masks_instead_of_refusing(tmp_path):
    results, logs = tmp_path / "results", tmp_path / "logs"
    data = {"error": "connect to 127.0.0.1:18080 failed", "role": "arn:aws:iam::123456789012:role/x",
            "node": "ip-10-0-0-1.ec2.internal", "bytes": 107294470144,
            "registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com/opensandbox-eks/x"}
    path = save("ladder", data, results=results, quarantine=logs,
                registry="123456789012.dkr.ecr.us-east-1.amazonaws.com")
    written = json.loads(path.read_text())
    assert written == {"error": "connect to <ip>:18080 failed", "role": "<arn>", "node": "<node>.ec2.internal",
                       "bytes": 107294470144, "registry": "<registry>/opensandbox-eks/x"}
    assert json.loads((logs / "ladder.raw.json").read_text())["error"].startswith("connect to 127.0.0.1")
    assert measure.scan_published(results) == []


def test_save_writes_no_raw_copy_when_nothing_was_masked(tmp_path):
    save("clean", {"n": 1}, results=tmp_path / "results", quarantine=tmp_path / "logs")
    assert not (tmp_path / "logs").exists()


def test_node_aliases():
    alias = NodeAliases(["ip-10-0-1-9.ec2.internal", "ip-10-0-0-7.ec2.internal"])
    assert alias("ip-10-0-0-7.ec2.internal") == "node-a"
    assert alias("ip-10-0-1-9.ec2.internal") == "node-b"
    assert alias("unknown") == "node-?"


# --- the CSV writer and the stats/summary rows ---------------------------------------------------------

def test_csv_sink_header_rows_and_empty_values(tmp_path):
    sink = CsvSink(tmp_path / "x.csv", CSV_HEADER)
    sink.write([[1.0, "node-a", "_node", 261.6, 2182.8], [1.0, "node-a", "pod", None, 5.0]])
    sink.close()
    lines = (tmp_path / "x.csv").read_text().splitlines()
    assert lines[0] == "t,node,pod,cpu_m,mem_mib"
    assert lines[2] == "1.0,node-a,pod,,5.0"
    assert sink.rows == 2


def test_csv_sink_rejects_a_short_row(tmp_path):
    sink = CsvSink(tmp_path / "x.csv", CSV_HEADER)
    with pytest.raises(ValueError):
        sink.write([[1.0, "node-a"]])
    sink.close()


def test_summary_rows_node_and_pods(summary):
    rows = summary_rows(summary, 5.0, "node-a")
    node = rows[0]
    assert node[:3] == [5.0, "node-a", "_node"]
    assert node[3] == round(summary["node"]["cpu"]["usageNanoCores"] / 1e6, 1)
    assert node[4] == round(summary["node"]["memory"]["workingSetBytes"] / 2**20, 1)
    by_pod = {r[2]: r for r in rows[1:]}
    assert by_pod[SANDBOX][3:] == [5.6, 101.0]          # pod-level values win over the containers' sum
    assert by_pod[FRESH][3] is None                      # no CPU rate yet: empty, never 0
    assert by_pod[FRESH][4] == 5.0                       # no pod-level memory: the containers' sum
    assert any(name.startswith("opensandbox-server-") for name in by_pod)


def test_summary_rows_namespace_filter(summary):
    rows = summary_rows(summary, 0.0, "node-a", namespaces=["opensandbox-system"])
    assert rows[0][2] == "_node"
    assert {r[2].rsplit("-", 2)[0] for r in rows[1:]} == {"opensandbox-server", "opensandbox-controller-manager"}


def test_container_rows_carry_the_sidecar_rss(summary):
    rows = container_rows(summary, 2.0, "node-a", namespaces=["opensandbox"])
    egress = [r for r in rows if r[2] == SANDBOX and r[3] == "egress"][0]
    assert egress[:7] == [2.0, "node-a", SANDBOX, "egress", 3.5, 80.0, 64.0]
    assert egress[7] == 0.0 and egress[8] > 1.7e9            # the cumulative counter and its kubelet time


def test_pod_stats(summary):
    stats = pod_stats(summary, SANDBOX)
    assert stats["mem_mib"] == 101.0 and stats["rss_mib"] == 77.0
    assert stats["containers"]["egress"]["rss_mib"] == 64.0
    assert pod_stats(summary, "no-such-pod") is None


# --- the Sampler ---------------------------------------------------------------------------------------

def test_sampler_writes_the_schema(summary, tmp_path):
    calls = []

    def fetch(node):
        calls.append(node)
        return summary

    names = ["ip-10-1-1-2.ec2.internal", "ip-10-1-1-1.ec2.internal"]
    with Sampler("density", every=0.05, nodes=names, fetch=fetch, alias=NodeAliases(names), containers=True,
                 namespaces=["opensandbox", "opensandbox-system"], results=tmp_path) as sampler:
        with sampler.phase("batch 1", run=1):
            time.sleep(0.2)
    with (tmp_path / "density.csv").open() as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["t", "node", "pod", "cpu_m", "mem_mib"]
    assert {r[1] for r in rows[1:]} == {"node-a", "node-b"}           # aliases, never the IP-bearing names
    assert not leaks((tmp_path / "density.csv").read_text())
    assert sampler.samples >= 2 and sampler.errors == 0
    assert (tmp_path / "density-containers.csv").read_text().splitlines()[0] == ",".join(measure.CONTAINER_HEADER)
    phase = sampler.phase_list()[0]
    assert phase["name"] == "batch 1" and phase["run"] == 1 and phase["end"] >= phase["start"]
    assert set(sampler.node_totals()) == {"node-a", "node-b"}
    assert sampler.pods_now()[SANDBOX]["node"] in {"node-a", "node-b"}


def test_sampler_counts_failed_fetches(tmp_path, summary):
    def fetch(node):
        if node == "bad":
            raise RuntimeError("kubelet unreachable")
        return summary

    sampler = Sampler("x", every=1, nodes=["good", "bad"], fetch=fetch, results=tmp_path)
    sampler.sample_once()
    assert sampler.errors == 1 and sampler.samples == 1 and set(sampler.latest) == {"good"}


def test_read_csv_and_window_stats(tmp_path):
    path = tmp_path / "cp.csv"
    path.write_text("t,node,pod,cpu_m,mem_mib\n"
                    "0.0,node-a,opensandbox-server-x,100,200\n"
                    "2.0,node-a,opensandbox-server-x,900,250\n"
                    "2.0,node-b,opensandbox-server-y,50,100\n"
                    "4.0,node-a,opensandbox-server-x,,260\n"
                    "6.0,node-a,opensandbox-server-x,300,240\n"
                    "2.0,node-a,opensandbox-controller-manager-z,10,60\n")
    rows = read_csv(path)
    assert rows[3]["cpu_m"] is None and rows[0]["t"] == 0.0
    s = window_stats(rows, "opensandbox-server-", 1.0, 5.0)
    assert s == {"samples": 2, "cpu_mean_m": 950.0, "cpu_peak_m": 950.0, "mem_peak_mib": 350.0}
    assert window_stats(rows, "nothing-", 0, 10)["cpu_peak_m"] is None


def test_read_csv_skips_a_row_still_being_written(tmp_path):
    path = tmp_path / "growing.csv"
    path.write_text("t,node,pod,cpu_m,mem_mib\n1.0,node-a,p,5,6\n2.0,node-a\n")
    rows = read_csv(path)
    assert len(rows) == 1 and rows[0]["cpu_m"] == 5.0


def test_mask_leaves_sandbox_ids_alone():
    sandbox = "c1d2e3f4-0000-4000-8000-123456789012"
    assert mask(f"pod {sandbox}-0 on 123456789012") == f"pod {sandbox}-0 on <account>"
    assert leaks(f"{sandbox}-0") == []


# --- the node-side VM sampler's log --------------------------------------------------------------------

SID_A = "a" * 64
SID_B = "b" * 64
POD_A = "11111111-1111-4111-8111-111111111111-0"
VMM_LOG = f"""vmm-sampler started 1793530000
vmm 1793530000 qemu 4101 276480 {SID_A} {POD_A}
vmm 1793530000 virtiofsd 4090 3072 {SID_A} {POD_A}
vmm 1793530000 virtiofsd 4092 6144 {SID_A} {POD_A}
vmm 1793530000 shim 4080 40960 {SID_A} {POD_A}
vmm 1793530000 qemu 4201 270336 {SID_B} -
vmm 1793530005 qemu 4101 281600 {SID_A} {POD_A}
vmm 1793530005 virtiofsd 4090 3072 {SID_A} {POD_A}
vmm 1793530005 virtiofsd 4092 6144 {SID_A} {POD_A}
vmm 1793530005 shim 4080 40960 {SID_A} {POD_A}
vmm 1793530005 qemu 4201 2703
vmm 1793530005 bogus 1 1 - -
"""


def test_parse_vmm_reads_lines_and_skips_the_rest():
    samples = measure.parse_vmm(VMM_LOG, "node-a")
    assert len(samples) == 9
    first = samples[0]
    assert first == {"t": 1793530000, "node": "node-a", "kind": "qemu", "pid": 4101, "rss_mib": 270.0,
                     "sandbox": SID_A, "pod": POD_A}
    assert samples[4]["pod"] is None and samples[4]["sandbox"] == SID_B


def test_vmm_by_pod_latest_sample_and_summed_processes():
    by_pod = measure.vmm_by_pod(measure.parse_vmm(VMM_LOG, "node-a"))
    assert by_pod[POD_A] == {"qemu": 275.0, "virtiofsd": 9.0, "shim": 40.0, "total": 324.0}
    assert by_pod[f"sandbox:{SID_B[:16]}"] == {"qemu": 264.0, "total": 264.0}
    earlier = measure.vmm_by_pod(measure.parse_vmm(VMM_LOG, "node-a"), at=1793530002)
    assert earlier[POD_A]["qemu"] == 270.0


def test_vmm_gap_paired_by_pod():
    vmm = {POD_A: {"qemu": 275.0, "virtiofsd": 9.0, "shim": 40.0, "total": 324.0}}
    kubelet = {POD_A: {"mem_mib": 6.0, "rss_mib": 4.0}, "other-0": {"mem_mib": 8.0, "rss_mib": 5.0}}
    gap = measure.vmm_gap(vmm, kubelet)
    assert gap["paired_by"] == "pod" and gap["joined"] == 1 and gap["vms"] == 1 and gap["pods"] == 2
    assert gap["vmm_total_mib"] == 324.0 and gap["qemu_mib"] == 275.0 and gap["kubelet_mem_mib"] == 6.0
    assert gap["gap_mib"] == 318.0


def test_vmm_gap_falls_back_to_medians():
    vmm = {f"sandbox:{SID_B[:16]}": {"qemu": 264.0, "total": 300.0}}
    gap = measure.vmm_gap(vmm, {POD_A: {"mem_mib": 6.0, "rss_mib": 4.0}})
    assert gap["paired_by"] == "medians" and gap["joined"] == 0 and gap["gap_mib"] == 294.0
    assert measure.vmm_gap({}, {})["gap_mib"] is None


def test_mem_mib_of():
    assert measure.mem_mib_of("128Mi") == 128 and measure.mem_mib_of("1Gi") == 1024
    assert measure.mem_mib_of("160Mi") + measure.mem_mib_of("128Mi") == 288 and measure.mem_mib_of(None) == 0


# --- templates and the published-files scan ------------------------------------------------------------

def test_render_template_fills_upper_case_only():
    text = "# ${COMMENT_ONLY}\nname: ${NAME}\nscript: echo ${p#/proc/} ${pod:--}\n"
    assert measure.render_template(text, {"NAME": "x"}) == "# ${COMMENT_ONLY}\nname: x\nscript: echo ${p#/proc/} ${pod:--}\n"
    with pytest.raises(ValueError, match=r"\$\{NODE\}"):
        measure.render_template("node: ${NODE}\n", {"NAME": "x"}, "vmm-sampler.yaml")


def test_leaks_arns_and_node_names():
    assert "an ARN with an account ID" in leaks("arn:aws:iam::123456789012:role/osb-sandbox")
    assert leaks("ran on ip-10-0-1-9.ec2.internal") == ["a node name with an IP"]
    assert mask("ran on ip-10-0-1-9.ec2.internal") == "ran on <node>.ec2.internal"


def _png_with_text(path, text):
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    data = zlib.compress(b"\x00\x00")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"tEXt", text.encode()) +
                     chunk(b"IDAT", data) + chunk(b"IEND", b""))


def test_scan_published(tmp_path):
    results, charts = tmp_path / "results", tmp_path / "charts"
    results.mkdir()
    charts.mkdir()
    (results / "clean.json").write_text(json.dumps({"bytes": 107294470144, "pod": "11111111-1111-4111-8111-111111111111-0"}))
    (results / "clean.csv").write_text("t,node,pod,cpu_m,mem_mib\n1.0,node-a,_node,123456789012,1\n")
    (results / "created-sandboxes.txt").write_text("c1d2e3f4-0000-4000-8000-123456789012\n")
    _png_with_text(charts / "ok-light.png", "Software\0Matplotlib version3.11.2")
    assert measure.scan_published(results, charts) == []
    (results / "bad.json").write_text(json.dumps({"role": "arn:aws:iam::123456789012:role/x"}))
    (results / "bad.csv").write_text("t,node,pod,cpu_m,mem_mib\n1.0,ip-10-0-0-7.ec2.internal,_node,1,1\n")
    _png_with_text(charts / "bad-dark.png", "Comment\0/Users/someone/charts")
    found = measure.scan_published(results, charts)
    assert ("results/bad.json", "an ARN with an account ID") in found
    assert ("results/bad.csv", "a node name with an IP") in found
    assert ("charts/bad-dark.png", "a home directory path") in found
    assert not any(name.startswith(("results/clean", "charts/ok", "results/created")) for name, _ in found)


def test_peaks_by_pod_per_replica():
    rows = [{"t": 1.0, "pod": "echo-stress-a", "cpu_m": 300.0}, {"t": 3.0, "pod": "echo-stress-a", "cpu_m": 820.0},
            {"t": 3.0, "pod": "echo-stress-b", "cpu_m": 410.0}, {"t": 9.0, "pod": "echo-stress-b", "cpu_m": 990.0},
            {"t": 3.0, "pod": "echo-stress-c", "cpu_m": None}, {"t": 3.0, "pod": "other", "cpu_m": 999.0}]
    peaks = measure.peaks_by_pod(rows, ["echo-stress-a", "echo-stress-b", "echo-stress-c"], 0.0, 5.0)
    assert peaks == {"echo-stress-a": 820.0, "echo-stress-b": 410.0, "echo-stress-c": None}


def cadvisor_rows(load_start: float, load_end: float, *, idle_m: float = 50, busy_m: float = 1000,
                  cadence: float = 10.0, read_every: float = 2.0, phase: float = 7.0) -> list[dict]:
    """What the sampler records for one container when the kubelet refreshes the counter every
    `cadence` seconds: a read every 2 s returns the last refresh, stamped up to `cadence` s earlier.
    The container is busy (busy_m) between load_start and load_end, idle around it."""
    def counter(at: float) -> float:          # cumulative CPU seconds at time `at`
        busy = max(0.0, min(at, load_end) - load_start)
        return round((idle_m * at + (busy_m - idle_m) * busy) / 1000, 6)

    rows, t = [], 0.0
    while t <= load_end + 25:
        stamp = phase + cadence * ((t - phase) // cadence) if t >= phase else None
        if stamp is not None:
            rows.append({"t": t - 1000, "pod": "p", "container": "c", "cpu_s": counter(stamp), "cpu_at": stamp})
        t += read_every
    return rows


def test_window_cpu_picks_readings_by_their_stamp_not_by_when_they_were_read():
    # The load runs from 40 to 70 s at 1000m, 50m around it; the kubelet refreshes every 10 s (at 7, 17, ...).
    rows = cadvisor_rows(40.0, 70.0)
    assert measure.window_cpu(rows, "p", "c", 40.0, 70.0) == 1000.0       # readings stamped 47, 57, 67
    # Picking by read time instead (the r1 bug) would take a reading stamped 37 (before the load)
    # and one stamped 77 (after it): an average well under 1000m, which no longer looks busy.
    stale = [r for r in rows if 40.0 <= r["t"] + 1000 <= 80.0]
    first, last = min(r["cpu_at"] for r in stale), max(r["cpu_at"] for r in stale)
    by_read_time = 1000 * (max(r["cpu_s"] for r in stale) - min(r["cpu_s"] for r in stale)) / (last - first)
    assert by_read_time < 800


def test_window_cpu_is_unknown_without_two_readings_10_s_apart():
    rows = cadvisor_rows(40.0, 55.0)                                    # 15 s of load: only the stamp 47 is inside
    assert measure.window_cpu(rows, "p", "c", 40.0, 55.0) is None       # one reading inside: unknown
    rows = cadvisor_rows(40.0, 60.0, phase=3.0)                         # stamps 43 and 53: 10 s apart
    assert measure.window_cpu(rows, "p", "c", 40.0, 60.0) == 1000.0
    assert measure.window_cpu(rows, "p", "c", 40.0, 60.0, min_span=15) is None
    assert measure.window_cpu(rows, "p", "other", 40.0, 60.0) is None   # another container: no readings


def test_window_cpu_unknown_after_a_restart():
    rows = [{"pod": "p", "container": "c", "cpu_s": 10.0, "cpu_at": 100.0},
            {"pod": "p", "container": "c", "cpu_s": 0.5, "cpu_at": 112.0}]
    assert measure.window_cpu(rows, "p", "c", 100.0, 120.0) is None


def test_unknown_is_never_not_bound():
    assert measure.bound(None, 800) is None
    assert measure.bound(900, 800) is True and measure.bound(700, 800) is False
    assert measure.any_bound([700, None, 300], 800) is None             # a replica unknown: maybe bound
    assert measure.any_bound([700, None, 850], 800) is True
    assert measure.any_bound([700, 600, 300], 800) is False
    assert measure.any_bound([], 800) is None


def test_mask_and_leaks_endpoints_and_load_balancers():
    endpoint = "ABCDEF0123456789ABCDEF0123456789.gr7.us-east-1.eks.amazonaws.com"
    nlb = "osb-gateway-0123456789abcdef.elb.us-east-1.amazonaws.com"
    assert "the EKS API endpoint" in leaks(f"https://{endpoint}")
    assert "a load balancer host name" in leaks(nlb)
    assert mask(f"dial {endpoint} and {nlb}") == "dial <eks-endpoint> and <nlb>"
    assert leaks(mask(f"arn:aws:sts::123456789012:assumed-role/osb-sandbox/{'x' * 8}")) == []


def test_pod_stats_says_where_the_memory_number_came_from(summary):
    assert pod_stats(summary, SANDBOX)["mem_source"] == "pod"
    assert pod_stats(summary, FRESH)["mem_source"] == "containers"


# --- node_growth: what one more sandbox costs the nodes ----------------------------------------------

def _batch(running: int, mem_a: float, mem_b: float | None, failed: int = 0) -> dict:
    return {"running": running, "failed": failed,
            "node_totals": {"node-a": {"mem_mib": mem_a}, "node-b": {"mem_mib": mem_b}}}


def test_node_growth_stops_at_the_first_batch_with_a_refused_create():
    # Kata run 1's shape: 10 -> 20 clean, then a batch where creates failed (its pods were being deleted).
    batches = [_batch(10, 3600, 3588), _batch(20, 5200, 5232), _batch(25, 6300, 6119, failed=5)]
    assert node_growth(batches) == 324.4
    # A refusal in the second batch leaves one clean batch: no growth to measure.
    assert node_growth([_batch(10, 3600, 3588), _batch(14, 4000, 4000, failed=6)]) is None


def test_node_growth_needs_both_nodes_and_two_batches():
    assert node_growth([]) is None
    assert node_growth([_batch(10, 3000, 3000)]) is None
    assert node_growth([_batch(10, 3000, 3000), _batch(20, 3100, None)]) is None
    assert node_growth([_batch(10, 3000, 3000), _batch(10, 3100, 3000)]) is None      # nothing added
    assert node_growth([_batch(10, 2000, 2000), _batch(20, 2100, 2000), _batch(80, 2420, 2420)]) == 12.0


def test_node_growth_span_says_how_many_sandboxes_it_rests_on():
    kata = [_batch(10, 3600, 3588), _batch(20, 5200, 5232), _batch(25, 6300, 6119, failed=5)]
    assert node_growth_span(kata) == 10                                   # one 10-sandbox step
    assert node_growth_span([_batch(10, 2000, 2000), _batch(80, 2420, 2420), _batch(89, 2500, 2500, failed=1)]) == 70
    assert node_growth_span([_batch(10, 2000, 2000)]) is None
    assert node_growth_span([_batch(10, 2000, 2000), _batch(20, 2100, None)]) is None


# --- storm_nat: NAT bytes per pause storm, each minute once -----------------------------------------

def _series(points: dict[str, float]) -> dict:
    """One point per minute from 15:24 to 15:31; `points` maps "HH:MM" to bytes, the rest are small."""
    ts = [f"2026-11-01T15:{m:02d}:00Z" for m in range(24, 32)]
    return {"t": ts, "v": [points.get(t[11:16], 4e6) for t in ts]}


def _storm(run: int, start: str, end: str) -> dict:
    return {"run": run, "start": f"2026-11-01T{start}Z", "end": f"2026-11-01T{end}Z",
            "snapshot_bytes": 2963261548, "fresh_bytes": 20 * 100 * 2**20}


def test_storms_closer_than_their_windows_are_counted_once():
    """Phase 2's case: three storms about 64 s apart, one-minute points, each storm's push in the
    minute after its pause began. Summing every point inside each padded window counted the middle
    minutes two and three times (2.18 / 6.57 / 4.39 GB); merged, each minute counts once."""
    storms = [_storm(1, "15:26:31", "15:26:54"), _storm(2, "15:27:35", "15:28:01"), _storm(3, "15:28:38", "15:29:01")]
    series = _series({"15:27": 2.184e9, "15:28": 2.180e9, "15:29": 2.205e9})
    per_storm, groups = storm_nat(storms, series, 60)
    assert len(groups) == 1 and groups[0]["runs"] == [1, 2, 3]
    total = groups[0]["nat_bytes_out"]
    assert abs(total - (2.184e9 + 2.180e9 + 2.205e9 + 4e6 * 2)) < 1        # 15:25 is outside; 15:26 and 15:30 in
    assert groups[0]["busy_points"] == ["2026-11-01T15:27:00Z", "2026-11-01T15:28:00Z", "2026-11-01T15:29:00Z"]
    assert [s["nat_bytes_out"] for s in per_storm] == [2.184e9, 2.180e9, 2.205e9]
    assert {s["attributed"] for s in per_storm} == {"minute"}
    assert groups[0]["snapshot_bytes"] == 3 * 2963261548


def test_storms_far_apart_keep_their_own_windows():
    storms = [_storm(1, "15:24:10", "15:24:30"), _storm(2, "15:29:10", "15:29:30")]
    series = _series({"15:25": 2.1e9, "15:30": 2.2e9})
    per_storm, groups = storm_nat(storms, series, 60)
    assert len(groups) == 2
    assert [s["attributed"] for s in per_storm] == ["window", "window"]
    assert abs(per_storm[0]["nat_bytes_out"] - (2.1e9 + 4e6)) < 1           # 15:24 and 15:25 (15:23 is not a point)
    assert abs(per_storm[1]["nat_bytes_out"] - (2.2e9 + 4e6)) < 1           # 15:29 and 15:30 (15:31 is past 15:30:30)


def test_storms_that_cannot_be_told_apart_report_only_their_total():
    storms = [_storm(1, "15:26:05", "15:26:20"), _storm(2, "15:26:40", "15:26:55")]
    one_point = _series({"15:27": 4.3e9})                                    # both pushes in the same minute
    per_storm, groups = storm_nat(storms, one_point, 60)
    assert len(groups) == 1 and [s["nat_bytes_out"] for s in per_storm] == [None, None]
    assert {s["attributed"] for s in per_storm} == {None} and groups[0]["per_storm_mean"] > 2e9
    longer = [_storm(1, "15:26:05", "15:26:20"), _storm(2, "15:26:40", "15:27:10")]   # window up to 15:28:10
    three_points = _series({"15:26": 1.4e9, "15:27": 1.5e9, "15:28": 1.4e9})  # more busy minutes than storms
    assert [s["nat_bytes_out"] for s in storm_nat(longer, three_points, 60)[0]] == [None, None]
    # Two busy minutes for two storms, but the second lies outside the second storm's own window: no.
    early = [_storm(1, "15:25:00", "15:25:20"), _storm(2, "15:26:10", "15:26:15")]     # 15:24-15:26:20, 15:25:10-15:27:15
    assert [s["nat_bytes_out"] for s in storm_nat(early, _series({"15:24": 2e9, "15:25": 2e9}), 60)[0]] == [None, None]


def test_save_replaces_the_file_whole(tmp_path):
    results = tmp_path / "results"
    save("x", {"a": 1}, results=results, quarantine=tmp_path / "logs")
    save("x", {"a": 2}, results=results, quarantine=tmp_path / "logs")
    assert json.loads((results / "x.json").read_text()) == {"a": 2}
    assert sorted(p.name for p in results.iterdir()) == ["x.json"]          # no partial file left behind


def test_a_storm_tail_in_a_quiet_minute_keeps_the_figures_from_being_claimed():
    """Three busy minutes for three storms, but 5% of the bytes in a fourth (a tail below the busy
    threshold): the per-minute figures would drop it, so only the group's total stands."""
    storms = [_storm(1, "15:26:31", "15:26:54"), _storm(2, "15:27:35", "15:28:01"), _storm(3, "15:28:38", "15:29:01")]
    series = _series({"15:27": 2.0e9, "15:28": 2.0e9, "15:29": 2.0e9, "15:30": 0.33e9})
    per_storm, groups = storm_nat(storms, series, 60)
    assert [s["nat_bytes_out"] for s in per_storm] == [None, None, None]
    assert groups[0]["unattributed_bytes"] is None
    clean = _series({"15:27": 2.0e9, "15:28": 2.0e9, "15:29": 2.0e9})
    per_storm, groups = storm_nat(storms, clean, 60)
    assert [s["attributed"] for s in per_storm] == ["minute"] * 3
    assert abs(groups[0]["unattributed_bytes"] - 2 * 4e6) < 1               # the quiet points at 15:26 and 15:30
