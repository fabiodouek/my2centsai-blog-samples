"""The calculator reproduces both AWS pricing examples to the cent.  python -m unittest"""

import json
import unittest
from pathlib import Path

from microvm_cost import RATES, estimate, rates_from_price_list

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



def offer(usage: dict) -> dict:
    """A minimal AWS Price List offer file: {usagetype: USD}."""
    return {"products": {f"SKU{i}": {"attributes": {"usagetype": u}} for i, u in enumerate(usage)},
            "terms": {"OnDemand": {f"SKU{i}": {"T": {"priceDimensions": {"D": {"pricePerUnit": {"USD": str(v)}}}}}
                                   for i, v in enumerate(usage.values())}}}


class PriceList(unittest.TestCase):
    """--rates live reads the five rates out of the AWS Price List; no network here."""
    PRICES = {"EU-Lambda-MicroVM-vCPU-Second-ARM": 0.00003, "EU-Lambda-MicroVM-vCPU-Second": 0.00004,
              "EU-Lambda-MicroVM-Memory-GB-Second-ARM": 0.000004, "EU-Lambda-MicroVM-Memory-GB-Second": 0.000005,
              "EU-Lambda-MicroVM-Snapshot-Write-GB": 0.004, "EU-Lambda-MicroVM-Snapshot-Read-GB": 0.0016,
              "EU-Lambda-MicroVM-Snapshot-Storage-GB-Hour": 0.0001, "EU-Lambda-GB-Second-ARM": 0.0000133334}

    def test_arm_and_x86(self):
        arm, x86 = rates_from_price_list(offer(self.PRICES), "arm"), rates_from_price_list(offer(self.PRICES), "x86")
        self.assertEqual((arm["vcpu_second"], arm["gb_second"]), (0.00003, 0.000004))
        self.assertEqual((x86["vcpu_second"], x86["gb_second"]), (0.00004, 0.000005))
        self.assertEqual((arm["snapshot_write_gb"], arm["snapshot_read_gb"]), (0.004, 0.0016))
        self.assertAlmostEqual(arm["storage_gb_month"], 0.0001 * 720)

    def test_missing_rate(self):
        prices = {k: v for k, v in self.PRICES.items() if "Snapshot-Read" not in k}
        with self.assertRaisesRegex(ValueError, "snapshot_read_gb"):
            rates_from_price_list(offer(prices))

    def test_us_east_1_matches_built_in(self):
        """The built-in price-list rates, in the shape of the us-east-1 file (no Region prefix)."""
        r = RATES["price-list"]
        got = rates_from_price_list(offer({
            "Lambda-MicroVM-vCPU-Second-ARM": r["vcpu_second"], "Lambda-MicroVM-Memory-GB-Second-ARM": r["gb_second"],
            "Lambda-MicroVM-Snapshot-Write-GB": r["snapshot_write_gb"], "Lambda-MicroVM-Snapshot-Read-GB": r["snapshot_read_gb"],
            "Lambda-MicroVM-Snapshot-Storage-GB-Hour": 0.0001111111}))
        for k in r:
            with self.subTest(k):
                self.assertAlmostEqual(got[k], r[k], places=6)


if __name__ == "__main__":
    unittest.main()
