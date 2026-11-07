"""05_egress_ladder.py's pure helpers for the --resolve-once variant. 05 imports common.py, which
needs ../opensandbox-eks/env.sh and the venv; no cluster."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not (ROOT.parent / "opensandbox-eks" / "env.sh").is_file(),
                                reason="../opensandbox-eks/env.sh is not here")


@pytest.fixture(scope="module")
def ladder():
    spec = importlib.util.spec_from_file_location("ladder05", ROOT / "05_egress_ladder.py")
    module = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["05_egress_ladder.py"]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = argv
    return module


def test_failure_pattern(ladder):
    assert ladder.failure_pattern(None, 12.0) is None                     # nothing failed
    assert ladder.failure_pattern(41.2, 41.2) == "expired"                 # nothing succeeded after it
    assert ladder.failure_pattern(41.2, None) == "expired"
    assert ladder.failure_pattern(3.1, 55.0) == "isolated"                 # a slow connection, then on as before


def test_labels_compare_like_with_like(ladder):
    assert ladder.split_label("dns+nft/cached") == ("dns+nft", "/cached")
    assert ladder.split_label("mitm/cached@8") == ("mitm", "/cached@8")
    assert ladder.split_label("vault") == ("vault", "")


def test_the_reduced_pass_takes_the_flagged_rung_and_what_it_is_compared_with(ladder):
    rungs = ["none", "dns+nft", "mitm", "vault"]

    def rec(rung, bound):
        return {"resolve_once": True, "base_rung": rung, "target_bound": bound}

    assert ladder.reduced_pass([rec("none", True), rec("mitm", False)], rungs) == ["none", "dns+nft"]
    assert ladder.reduced_pass([rec("dns+nft", True)], rungs) == rungs
    assert ladder.reduced_pass([rec("mitm", None), rec("vault", False)], rungs) == []   # unknown is not bound


def test_summary_compares_cached_rungs_with_cached_bases(ladder):
    def rec(label, base, rps, n=1):
        load = {"rps": rps, "mean": 5.0, "p50": 4.0, "p99": 9.0}
        return {"rung": label, "base_rung": base, "run": n, "failed": 0, "sequential_ms": [2.0, 3.0],
                "sequential": {"p50": 2.5, "mean": 2.5}, "load": load, "lookup": {"p50": 60.0},
                "resolve_once": True, "first_failure_s": None, "failure_pattern": None, "failure_kinds": {}}

    out = ladder.summarize([rec("dns+nft/cached", "dns+nft", 2000.0), rec("mitm/cached", "mitm", 1000.0),
                            rec("mitm/cached@8", "mitm", 900.0), rec("dns+nft/cached@8", "dns+nft", 1200.0)])
    assert out["mitm/cached"]["vs_dns+nft"]["rps_pct"] == -50.0
    assert out["mitm/cached@8"]["vs_dns+nft"]["rps_pct"] == -25.0
    assert out["dns+nft/cached"]["lookup_once_ms"]["median"] == 60.0
    assert out["dns+nft/cached"]["first_failure_s_by_run"] == [None]


def test_phase2_split_from_the_main_ladder(ladder):
    records = [{"rung": "mitm", "sequential_ms": [64.0, 66.0], "sequential": {"mean": 65.0}, "lookup": {"mean": 60.16}},
               {"rung": "mitm", "sequential_ms": [70.0], "sequential": {"mean": 70.0}, "lookup": {"mean": 65.0}},
               {"rung": "sigv4", "sequential_ms": [9.0], "lookup": {"mean": 1.0}}]
    assert ladder.phase2_connect_ms({"records": records}) == {"mitm": 4.92}
    assert ladder.phase2_connect_ms(None) == {}


def _row(t, pod, container, cpu_m, cpu_s=None, cpu_at=None):
    return {"t": t, "pod": pod, "container": container, "cpu_m": cpu_m, "cpu_s": cpu_s, "cpu_at": cpu_at}


def test_proxy_cpu_reads_the_sidecar_and_flags_one_core(ladder):
    rows = [_row(10, "p", "sandbox", 60), _row(10, "p", "egress", 900, 100.0, 1000.0),
            _row(20, "p", "egress", 1040, 110.4, 1010.0), _row(30, "p", "egress", 1030, 120.7, 1020.0),
            _row(25, "q", "egress", 2000)]                                      # another pod: ignored
    out = ladder.proxy_cpu(rows, "p", (999.0, 1031.0), (5, 35))
    assert out == {"sidecar_load_avg_cpu_m": 1035.0, "sidecar_load_peak_cpu_m": 1040, "proxy_bound": True}
    # One stamped reading inside the load: the average is unknown, the peak rate decides.
    one = ladder.proxy_cpu(rows, "p", (1005.0, 1015.0), (5, 35))
    assert one["sidecar_load_avg_cpu_m"] is None and one["proxy_bound"] is True
    idle = ladder.proxy_cpu([_row(10, "p", "egress", 5, 1.0, 1000.0), _row(20, "p", "egress", 4, 1.05, 1012.0)],
                            "p", (999.0, 1013.0), (5, 25))
    assert idle["proxy_bound"] is False
    assert ladder.proxy_cpu([_row(10, "p", "sandbox", 900)], "p", (0, 1), (0, 20))["proxy_bound"] is None   # no sidecar


def test_run_pod_needs_exactly_one_sandbox_in_the_window(ladder):
    rows = [_row(10, "a", "sandbox", 1), _row(12, "a", "egress", 1), _row(40, "b", "sandbox", 1)]
    assert ladder.run_pod(rows, (5, 20)) == "a"
    assert ladder.run_pod(rows, (5, 45)) is None
    assert ladder.run_pod(rows, (50, 60)) is None
