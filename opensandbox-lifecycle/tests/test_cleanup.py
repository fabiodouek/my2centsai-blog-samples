"""Offline tests for 99_cleanup.py's scoping of snapshot images (no cluster). Needs the venv (common.py
imports the SDK) and ../opensandbox-eks/env.sh:

    uv run python -m unittest discover -s tests -v
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cleanup", HERE / "99_cleanup.py")
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)

OURS_GONE = "11111111-1111-4111-8111-111111111111"
OURS_PAUSED = "22222222-2222-4222-8222-222222222222"
OTHER_PART = "33333333-3333-4333-8333-333333333333"


def image(tag: str | None, digest: str) -> dict:
    return {"imageDigest": f"sha256:{digest * 64}"[:71], **({"imageTag": tag} if tag else {})}


class DeletableImagesTest(unittest.TestCase):
    images = [image(f"{OURS_GONE}-sandbox-snap-gen2", "a"), image(f"{OURS_GONE}-egress-snap-gen2", "b"),
              image(f"{OURS_PAUSED}-sandbox-snap-gen2", "c"), image(f"{OTHER_PART}-sandbox-snap-gen2", "d"),
              image(None, "e")]

    def test_only_our_images_of_deleted_sandboxes_go(self):
        delete, kept, others = cleanup.deletable_images(self.images, {OURS_GONE, OURS_PAUSED}, {OURS_PAUSED})
        self.assertEqual(sorted(d["imageTag"] for d in delete),
                         [f"{OURS_GONE}-egress-snap-gen2", f"{OURS_GONE}-sandbox-snap-gen2"])
        self.assertEqual((kept, others), (1, 2))

    def test_an_orphan_of_another_part_is_left_alone(self):
        delete, _, others = cleanup.deletable_images(self.images, {OURS_GONE}, set())
        self.assertNotIn(f"{OTHER_PART}-sandbox-snap-gen2", [d["imageTag"] for d in delete])
        self.assertEqual(others, 3)   # the other part's, our paused one we did not record, and the untagged one

    def test_a_paused_sandbox_keeps_its_images(self):
        delete, kept, _ = cleanup.deletable_images(self.images, {OURS_PAUSED}, {OURS_PAUSED})
        self.assertEqual((delete, kept), ([], 1))

    def test_no_records_no_deletions(self):
        self.assertEqual(cleanup.deletable_images(self.images, set(), set())[0], [])

    def test_ids_come_from_the_log_and_the_benchmark_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            results.mkdir()
            (results / "created-sandboxes.txt").write_text(f"{OURS_GONE}\n")
            trials = [{"type": "environment"}, {"type": "trial", "sandbox_id": OURS_PAUSED},
                      {"type": "trial", "sandbox_id": None, "run_id": "x"}, {"type": "summary"}]
            (results / "resume-uncontrolled-20261101T000000Z.jsonl").write_text(
                "".join(json.dumps(t) + "\n" for t in trials))
            with mock.patch.object(cleanup, "HERE", Path(tmp)), \
                    mock.patch.object(cleanup, "CREATED_LOG", results / "created-sandboxes.txt"):
                self.assertEqual(cleanup.our_sandbox_ids(), {OURS_GONE, OURS_PAUSED})


if __name__ == "__main__":
    unittest.main()
