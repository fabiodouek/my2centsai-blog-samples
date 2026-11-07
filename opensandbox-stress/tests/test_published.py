"""results/ and charts/ are committed: nothing in them may name the account, an ARN with it, an IP
address, a node by its IP-bearing name, or a home directory. Empty before phase 2; run it again after
the runs (99_cleanup.py runs the same scan at its end)."""

from pathlib import Path

from measure import scan_published

ROOT = Path(__file__).resolve().parent.parent


def test_results_and_charts_are_publishable():
    found = scan_published(ROOT / "results", ROOT / "charts")
    assert found == [], "\n".join(f"{name}: {kind}" for name, kind in found)
