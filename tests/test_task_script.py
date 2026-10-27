"""Operator CLI boundaries: previews cannot read secrets, pay or create a DB."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loom.store import digest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("loom_task_script_for_tests", ROOT / "scripts/run_local_task.py")
task_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_script)


class TaskScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def arguments(self, *, repairs=0, task_file=None):
        return SimpleNamespace(task_file=task_file or ROOT / "configs/python-task.repair.example.json",
                               max_repairs=repairs, approve=False, db=self.root / "absent.db",
                               model_config=self.root / "absent-model-config.json",
                               secrets_file=self.root / "private/absent-secret.json",
                               sandbox_image="sha256:" + "0" * 64, tenant="preview-test",
                               max_output_tokens=512)

    def preview(self, args):
        with patch.object(task_script, "load_project_secrets", side_effect=AssertionError("preview read secrets")), \
                patch.object(task_script, "DockerSandbox", side_effect=AssertionError("preview touched Docker")), \
                patch.object(task_script, "ModelGateway", side_effect=AssertionError("preview touched model")), \
                patch.object(task_script, "Store", side_effect=AssertionError("preview touched DB")):
            result = asyncio.run(task_script.run(args))
        self.assertFalse(Path(args.db).exists())
        self.assertEqual(result["mode"], "plan-preview")
        self.assertEqual(result["network_requests"], 0)
        return result

    def test_legacy_preview_preserves_no_repair_contract_and_one_attempt(self):
        args = self.arguments(task_file=ROOT / "configs/python-task.runs.example.json")
        result = self.preview(args)
        node = result["definition"]["nodes"][0]
        self.assertNotIn("repair", node)
        self.assertEqual(node["limits"]["max_attempts"], 1)

    def test_public_tests_do_not_enable_repairs_without_operator_choice(self):
        result = self.preview(self.arguments())
        self.assertNotIn("repair", result["definition"]["nodes"][0])

    def test_operator_repair_preview_binds_both_suites_and_attempt_limit(self):
        for repairs in (1, 2):
            with self.subTest(repairs=repairs):
                result = self.preview(self.arguments(repairs=repairs))
                node = result["definition"]["nodes"][0]
                self.assertEqual(node["repair"]["max_repairs"], repairs)
                self.assertEqual(node["repair"]["schema_version"], "loom.rebind/v1")
                self.assertEqual(node["limits"]["max_attempts"], repairs + 1)
                self.assertNotEqual(node["repair"]["development_digest"], node["evaluation_digest"])
                self.assertEqual(node["repair"]["development_digest"],
                                 digest(json.loads((ROOT / "configs/python-task.repair.example.json").read_text(encoding="utf-8"))["development_tests"]))

    def test_repair_choice_requires_explicit_development_suite(self):
        with self.assertRaisesRegex(ValueError, "PUBLIC_DEVELOPMENT_TESTS_REQUIRED"):
            self.preview(self.arguments(repairs=1, task_file=ROOT / "configs/python-task.example.json"))

    def test_duplicate_json_keys_cannot_change_approved_inputs(self):
        path = self.root / "duplicate.json"
        path.write_text('{"title":"one","title":"two","description":"x","tests":[]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "TASK_DUPLICATE_FIELD"):
            task_script.load_job(path)

    def test_same_suite_cannot_be_labelled_public_and_acceptance(self):
        data = json.loads((ROOT / "configs/python-task.example.json").read_text(encoding="utf-8"))
        data["development_tests"] = data["tests"]
        path = self.root / "overlapping.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DEVELOPMENT_AND_ACCEPTANCE_MUST_DIFFER"):
            task_script.load_job(path)

    def test_unrecognized_fields_cannot_authorize_more_capabilities(self):
        data = json.loads((ROOT / "configs/python-task.example.json").read_text(encoding="utf-8"))
        data["tools"] = ["host.exec"]
        path = self.root / "extra.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "TASK_INPUT_INVALID"):
            task_script.load_job(path)


if __name__ == "__main__":
    unittest.main()
