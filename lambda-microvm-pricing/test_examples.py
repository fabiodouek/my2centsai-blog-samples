"""The calculator reproduces both AWS pricing examples to the cent.  python -m unittest"""

import json
import unittest
from pathlib import Path

from microvm_cost import RATES, estimate

HERE = Path(__file__).parent


def load(name: str) -> dict:
    return json.loads((HERE / "scenarios" / name).read_text())


class Examples(unittest.TestCase):
    def assertCents(self, got: float, want: float) -> None:
        self.assertEqual(round(got, 2), want)

    def test_coding_sandboxes(self):
        e = estimate(load("coding-sandboxes.json"), RATES["page"])
        for k, want in {"peak_vcpu": 498.50, "peak_memory": 132.00, "peak_total": 630.50,
                        "baseline_vcpu": 373.87, "baseline_memory": 99.00, "baseline_total": 472.88,
                        "compute": 1103.38, "write_suspend": 91.20, "read_resume": 37.20, "read_launch": 6.20,
                        "read_write": 134.60, "image_storage": 0.80, "suspended_storage": 2.44, "storage": 3.24,
                        "total": 1241.22, "per_unit": 12.41}.items():
            with self.subTest(k):
                self.assertCents(e[k], want)

    def test_ci_jobs(self):
        e = estimate(load("ci-jobs.json"), RATES["page"])
        for k, want in {"peak_vcpu": 265.87, "peak_memory": 70.40, "peak_total": 336.27,
                        "baseline_vcpu": 598.20, "baseline_memory": 158.40, "baseline_total": 756.60,
                        "compute": 1092.87, "read_launch": 31.00, "image_storage": 0.16, "total": 1124.03}.items():
            with self.subTest(k):
                self.assertCents(e[k], want)
        self.assertAlmostEqual(e["per_unit"], 0.1124, places=4)


if __name__ == "__main__":
    unittest.main()
