import copy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from loom.store import digest
from loom.workflow import WorkflowDefinition


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"


class WorkflowArtifactTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads(PACK.read_text(encoding="utf-8"))

    def test_absent_and_null_new_field_preserve_exact_legacy_plan_digest(self):
        baseline = WorkflowDefinition.model_validate(self.source).model_dump(mode="json")
        self.assertTrue(all("input_artifacts" not in node for node in baseline["nodes"]))
        self.assertTrue(all("evaluation_digest" in node and node["evaluation_digest"] is None for node in baseline["nodes"]))
        # This is the complete legacy 0.5 canonical node shape, including the
        # older null field rather than a broad exclude_none transformation.
        expected = copy.deepcopy(self.source)
        for node in expected["nodes"]:
            node["evaluation_digest"] = None
        self.assertEqual(baseline, expected)
        self.assertEqual(WorkflowDefinition.model_validate(self.source).execution_digest, digest(expected))
        for node in self.source["nodes"]:
            node["input_artifacts"] = None
        self.assertEqual(WorkflowDefinition.model_validate(self.source).execution_digest, digest(expected))

    def test_selector_changes_digest_but_never_changes_control_dependencies(self):
        old = WorkflowDefinition.model_validate(self.source)
        self.source["nodes"][1]["input_artifacts"] = [{"source_node": "spec", "kind": "requirements",
                                                      "schema_version": "loom.requirements/v1", "required": True}]
        new = WorkflowDefinition.model_validate(self.source)
        self.assertNotEqual(old.execution_digest, new.execution_digest)
        self.assertEqual([node.depends for node in old.nodes], [node.depends for node in new.nodes])

    def test_selector_rejects_unknown_self_mismatched_duplicate_and_authority_fields(self):
        base = {"source_node": "spec", "kind": "requirements", "schema_version": "loom.requirements/v1"}
        changes = ({"source_node": "missing"}, {"source_node": "design"}, {"kind": "design", "schema_version": "loom.design/v1"},
                   {"schema_version": "loom.requirements/v2"}, {"required": 1}, {"verified": True})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                source = copy.deepcopy(self.source)
                source["nodes"][1]["input_artifacts"] = [{**base, **change}]
                WorkflowDefinition.model_validate(source)
        for selectors in ([base, base], [base] * 9):
            with self.subTest(selectors=len(selectors)), self.assertRaises(ValidationError):
                source = copy.deepcopy(self.source)
                source["nodes"][1]["input_artifacts"] = selectors
                WorkflowDefinition.model_validate(source)


if __name__ == "__main__":
    unittest.main()
