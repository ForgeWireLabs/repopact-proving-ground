import unittest

from benchmarks.drift.harness import MUTATIONS
from benchmarks.harness.ac3_execution_manifest import build_manifest
from benchmarks.harness.ac3_preflight import _queue


class AC3PreflightTests(unittest.TestCase):
    def test_queue_preserves_the_frozen_543_cell_order_and_counts(self):
        manifest = build_manifest()
        entries, queue = _queue(manifest)
        self.assertEqual(len(entries), 543)
        self.assertEqual(manifest["counts"]["execution_slots"], 567)
        self.assertTrue(queue["passed"])
        self.assertEqual(len({entry["cell_id"] for entry in entries}), 543)
        self.assertEqual(len({entry["capture_path"] for entry in entries}), 543)
        self.assertEqual(len({entry["envelope_path"] for entry in entries}), 543)
        self.assertEqual(len({entry["result_path"] for entry in entries}), 543)

    def test_s5_uses_all_registered_mutations_in_order(self):
        self.assertEqual([mutation[0] for mutation in MUTATIONS], [f"M{number}" for number in range(1, 16)])
        self.assertEqual(len(MUTATIONS), 15)


if __name__ == "__main__":
    unittest.main()
