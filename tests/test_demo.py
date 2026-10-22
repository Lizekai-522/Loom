from pathlib import Path
import tempfile
import unittest

from loom.demo import run_demo
from loom.store import Store


class DemoTests(unittest.TestCase):
    def test_fixture_runs_parallel_branches_rejects_repairs_and_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "demo.db")
            manifest = Path(__file__).parents[1] / "packs/software-dev/manifest.json"
            report = run_demo(store, manifest)
            self.assertEqual(report["tasks"], 7)
            self.assertEqual(report["repair_count"], 1)
            self.assertEqual(report["gate_failure_count"], 1)
            self.assertGreaterEqual(report["max_concurrent_dispatched"], 2)
            self.assertTrue(report["chain_valid"])
            self.assertTrue(report["replay_matches_task_statuses"])
            self.assertFalse(report["real_models_used"])
            self.assertEqual(report["outcome"]["state"], "succeeded")
            reopened = Store(Path(directory) / "demo.db")
            replay = reopened.replay("demo", report["run_id"])
            snapshot = reopened.snapshot("demo", report["run_id"])
            for field in ("spent", "reserved", "tokens_spent", "tokens_reserved", "status"):
                self.assertEqual(replay[field], snapshot[field])


if __name__ == "__main__":
    unittest.main()
