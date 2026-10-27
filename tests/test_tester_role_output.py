"""Tester data-only candidates: real SQLite, synthetic known gateway results.

No provider, sandbox, frozen-suite promotion or private configuration is used.
"""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from loom.artifact_schemas import RoleOutputError, decode_role_output
from loom.artifacts import ArtifactService, ExecutionContextError
from loom.model_gateway import ModelResult, ModelUsage
from loom.store import Conflict, NotFound, Store, canonical, digest
from loom.workflow import WorkflowDefinition, WorkflowService


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
CHECKS = {"schema_version": "loom.public-checks/v1", "visibility": "development",
          "cases": [{"name": "empty", "input": {"values": []}, "expected": 0}]}
REQUIREMENTS = {"schema_version": "loom.requirements/v1", "summary": "Keep frozen limits",
                "items": [{"id": "bounds", "text": "Generated checks remain untrusted data."}], "uncertainties": []}
DESIGN = {"schema_version": "loom.design/v1", "overview": "Keep existing contracts",
          "components": [{"id": "core", "responsibility": "Respect explicit authorization."}], "steps": [], "risks": []}


class TesterRoleOutputTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "tester.db")
        self.workflow = WorkflowService(self.store)
        self.artifacts = ArtifactService(self.store)

    def prepare(self, *, kind="public-checks", authorized=True, context=False):
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        index, self.body = {"requirements": (0, REQUIREMENTS), "design": (1, DESIGN), "public-checks": (2, CHECKS)}[kind]
        source = copy.deepcopy(definition["nodes"][index])
        source.update(id="generate", depends=[], priority=80)
        output = {"kind": kind, "schema_version": self.body["schema_version"]}
        if authorized:
            source["role_output"] = output
        developer = copy.deepcopy(definition["nodes"][3])
        developer.update(id="implement", depends=["generate"], evaluation_digest="a" * 64,
                         repair={"schema_version": "loom.rebind/v1", "development_digest": "b" * 64,
                                 "max_repairs": 1, "stop_on_duplicate_source": True})
        review = copy.deepcopy(definition["nodes"][-1])
        review.update(id="review", depends=["implement"])
        nodes = [source, developer, review]
        if context:
            upstream = copy.deepcopy(definition["nodes"][0])
            upstream.update(id="input", depends=[], priority=90)
            source["input_artifacts"] = [{"source_node": "input", "kind": "requirements",
                                          "schema_version": "loom.requirements/v1", "required": True}]
            nodes.insert(0, upstream)
        definition["nodes"] = nodes
        self.run = self.workflow.create_goal("a", "goal", title="Bounded tester candidate",
            description="Do not execute or promote generated cases.", acceptance=["schema.valid"])["run_id"]
        saved = self.workflow.save_plan("a", self.run, "plan", definition, 0)
        approved = self.workflow.approve_plan("a", self.run, "approve", saved["revision"], saved["digest"])
        self.plan = saved["digest"]
        self.task_id = approved["task_ids"]["generate"]
        self.developer_id = approved["task_ids"]["implement"]
        self.developer_contract = self.workflow.get_task_spec("a", self.run, self.developer_id)
        if context:
            self.upstream = self.store.claim_next("a", self.run, "upstream", lease_seconds=3600)
            self.input_artifact = self.artifacts.save_candidate(self.upstream, "input", "requirements", REQUIREMENTS,
                                                               actor="subject:upstream")
            self.store.submit_output(self.upstream, "input-submit", {"artifact_id": self.input_artifact["artifact_id"]})
        self.claim = self.store.claim_next("a", self.run, "tester-worker", lease_seconds=3600)
        self.assertEqual(self.claim.task_id, self.task_id)
        if context:
            self.artifacts.approve_input("a", self.run, self.task_id, self.input_artifact["artifact_id"], "input-grant",
                actor="subject:operator", expected_plan_digest=self.plan, expected_source_epoch=self.upstream.epoch,
                expected_target_epoch=self.claim.epoch, expected_revision=1,
                expected_content_hash=self.input_artifact["content_hash"])
        self.snapshot = self.artifacts.capture_execution_context(self.claim)
        self.request = {"route": "deepseek-flash", "provider": "deepseek", "model": "deepseek-flash",
                        "prompt_digest": digest(["bounded tester fixture"]), "max_output_tokens": 128,
                        "plan_digest": self.plan, "role_output": output, "response_contract": "role-artifact/v1"}
        self.reservation_id = f"python:{self.task_id}:{self.claim.epoch}"
        self.result = asdict(ModelResult(canonical(self.body), "deepseek-flash", "deepseek", "deepseek-flash",
                                        ModelUsage(12, 8, 20), "chatcmpl-tester-fixture", "stop", "deepseek-flash"))

    def known(self, *, content=None):
        grant = self.artifacts.admit_execution_effect(self.claim, self.snapshot, reservation_id=self.reservation_id,
            max_cost=500, max_tokens=100, operation_id=f"model-generation:{self.claim.epoch}", request=self.request)
        self.call_id = grant["call_id"]
        result = self.result if content is None else {**self.result, "content": content}
        self.store.finish_effect(self.claim, self.call_id, result)
        self.store.settle("a", self.reservation_id, actual_cost=14, actual_tokens=20)

    def submit(self, key="role-submit", **changes):
        args = {"call_id": self.call_id, "reservation_id": self.reservation_id,
                "request": self.request, "snapshot": self.snapshot}
        args.update(changes)
        return self.artifacts.submit_role_output(self.claim, key, **args)

    def counts(self):
        with self.store.connect() as db:
            return tuple(db.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in
                         ("artifact_candidates", "events", "outbox", "receipts", "effects", "reservations"))

    def test_tester_source_is_bound_but_cases_are_not_executed_or_verified(self):
        self.prepare()
        self.known()
        saved = self.submit()
        candidate = self.artifacts.get_artifact("a", self.run, saved["artifact_id"])
        self.assertEqual(saved["status"], "submitted")
        self.assertTrue(saved["schema_validated"])
        self.assertTrue(saved["model_source_recorded"])
        self.assertEqual(candidate["body"], CHECKS)
        self.assertEqual(candidate["origin"], "model-gateway-response")
        self.assertEqual(candidate["model_provenance"]["role"], "tester")
        self.assertEqual(candidate["model_provenance"]["content_hash"], digest(CHECKS))
        self.assertEqual(candidate["model_provenance"]["call_id"], self.call_id)
        self.assertEqual(candidate["model_provenance"]["usage_cost_upper_microusd"], 14)
        self.assertFalse(saved["verified"])
        self.assertFalse(candidate["producer_verified"])
        self.assertFalse(candidate["counts_as_success"])
        events = self.store.events("a", self.run)
        self.assertEqual(sum(e["type"] == "role.candidate_generated" for e in events), 1)
        self.assertFalse(any(e["type"] in {"execution.evaluated", "execution.development_evaluated", "task.completed"} for e in events))
        self.assertEqual(len([e for e in events if e["type"] == "tool.intent"]), 1)
        self.assertIsNone(self.store.claim_next("a", self.run, "developer"))
        self.assertIsNone(self.store.claim_next("a", self.run, "validator", channel="verification"))
        self.assertEqual(self.workflow.get_task_spec("a", self.run, self.developer_id), self.developer_contract)
        self.assertEqual(self.developer_contract["evaluation_digest"], "a" * 64)
        self.assertEqual(self.developer_contract["repair"]["development_digest"], "b" * 64)
        self.assertTrue(self.store.verify_events(events))

    def test_decoder_strict_public_schema_and_static_errors(self):
        bad = ["secret-sentinel", "```json\n" + canonical(CHECKS) + "\n```",
               canonical({**CHECKS, "visibility": "hidden"}), canonical({**CHECKS, "visibility": "acceptance"}),
               canonical({**CHECKS, "producer_verified": True}),
               canonical({**CHECKS, "cases": CHECKS["cases"] * 2}),
               canonical({**CHECKS, "cases": [{**CHECKS["cases"][0], "passed": True}]}),
               '{"schema_version":"loom.public-checks/v1","visibility":"development","visibility":"development","cases":[]}',
               '{"schema_version":"loom.public-checks/v1","visibility":"development","cases":[{"name":"bad","input":{"x":1,"x":2},"expected":0}]}',
               '{"schema_version":"loom.public-checks/v1","visibility":"development","cases":[{"name":"bad","input":NaN,"expected":0}]}',
               '{"schema_version":"loom.public-checks/v1","visibility":"development","cases":[{"name":"bad","input":0,"expected":Infinity}]}',
               canonical(CHECKS) + " " * 32768, "[" * 50 + "0" + "]" * 50, "\ud800",
               canonical({**CHECKS, "cases": [{"name": "large", "input": "测" * 11000, "expected": 0}]}),
               canonical({**CHECKS, "cases": [{"name": "deep", "input": [[[[[[[[[[[[[[0]]]]]]]]]]]]]], "expected": 0}]})]
        for index, content in enumerate(bad):
            with self.subTest(index=index), self.assertRaises(RoleOutputError) as caught:
                decode_role_output("public-checks", content)
            self.assertEqual(str(caught.exception), "ROLE_OUTPUT_INVALID")
        self.assertEqual(decode_role_output("public-checks", canonical(CHECKS)), CHECKS)

    def test_invalid_returned_checks_keep_known_usage_but_save_nothing(self):
        self.prepare()
        self.known(content=canonical({**CHECKS, "visibility": "hidden"}))
        before, state = self.counts(), self.store.snapshot("a", self.run)
        with self.assertRaises(RoleOutputError):
            self.submit()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run), state)
        self.assertEqual(state["spent"], 14)
        self.assertEqual(self.artifacts.list_artifacts("a", self.run), [])

    def test_role_name_without_explicit_output_does_not_authorize_generation(self):
        self.prepare(authorized=False)
        self.known()
        before = self.counts()
        with self.assertRaises(Conflict):
            self.submit()
        self.assertEqual(self.counts(), before)

    def test_tester_context_is_exact_at_commit_and_cached_receipt(self):
        self.prepare(context=True)
        self.known()
        saved = self.submit()
        self.assertEqual(saved["execution_context_digest"], self.snapshot["context_digest"])
        for field in ("body", "flag", "bytes", "reference"):
            snapshot = copy.deepcopy(self.snapshot)
            if field == "body":
                snapshot["items"][0]["body"]["summary"] = "different"
            elif field == "flag":
                snapshot["producer_verified"] = True
            elif field == "bytes":
                snapshot["bytes"] += 1
            else:
                snapshot["items"][0]["grant_id"] = "different"
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.submit(snapshot=snapshot)
        self.assertEqual(self.submit(), saved)

    def test_source_replaced_before_publication_rejects_all_candidate_writes(self):
        self.prepare(context=True)
        self.known()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET epoch=epoch+1 WHERE id=?", (self.upstream.task_id,))
        before = self.counts()
        with self.assertRaises(ExecutionContextError):
            self.submit()
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.artifacts.list_artifacts("a", self.run)), 1)

    def test_candidate_or_submit_event_failure_rolls_back_together(self):
        self.prepare()
        self.known()
        original = self.store._event
        for fail_type in ("role.candidate_generated", "task.output_submitted"):
            before, state = self.counts(), self.store.snapshot("a", self.run)
            def fail(db, tenant, run_id, kind, *args):
                event = original(db, tenant, run_id, kind, *args)
                if kind == fail_type:
                    raise RuntimeError("injected tester event failure")
                return event
            with self.subTest(event=fail_type), patch.object(self.store, "_event", side_effect=fail), self.assertRaises(RuntimeError):
                self.submit("failure-" + fail_type)
            self.assertEqual(self.counts(), before)
            self.assertEqual(self.store.snapshot("a", self.run), state)
            self.assertEqual(state["spent"], 14)

    def test_receipt_failure_rolls_back_tester_candidate_and_submission(self):
        self.prepare()
        self.known()
        before, state = self.counts(), self.store.snapshot("a", self.run)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER tester_receipt_fail BEFORE INSERT ON receipts WHEN NEW.key='bad' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit("bad")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run), state)

    def test_tester_source_is_tenant_and_run_scoped(self):
        self.prepare()
        self.known()
        saved = self.submit()
        with self.assertRaises(NotFound):
            self.artifacts.get_artifact("other", self.run, saved["artifact_id"])
        with self.assertRaises(NotFound):
            self.artifacts.get_artifact("a", "other-run", saved["artifact_id"])

    def test_worker_tester_candidate_remains_worker_origin_after_nullable_migration(self):
        self.prepare(authorized=False)
        saved = self.artifacts.save_candidate(self.claim, "worker", "public-checks", CHECKS, actor="subject:tester-worker")
        events = self.store.events("a", self.run)
        with self.store.transaction() as db:
            db.execute("ALTER TABLE artifact_candidates DROP COLUMN origin")
            db.execute("ALTER TABLE artifact_candidates DROP COLUMN provenance")
        migrated = ArtifactService(self.store)
        self.assertEqual(migrated.get_artifact("a", self.run, saved["artifact_id"]), saved)
        self.assertEqual(self.store.events("a", self.run), events)
        self.assertEqual(saved["origin"], "authenticated-worker-submission")
        self.assertNotIn("model_source_recorded", saved)
        self.assertNotIn("model_provenance", saved)

    def test_requirements_and_design_keep_the_same_gateway_protocol(self):
        for kind in ("requirements", "design"):
            with self.subTest(kind=kind):
                # Fresh fixture for each existing output kind; no inherited state.
                self.setUp()
                self.prepare(kind=kind)
                self.known()
                saved = self.submit()
                self.assertEqual(saved["artifact"]["body"], self.body)
                self.assertEqual(saved["artifact"]["model_provenance"]["role_output"], self.request["role_output"])
                self.assertEqual(saved["status"], "submitted")
                self.assertFalse(saved["producer_verified"])


class TesterWorkflowContractTests(unittest.TestCase):
    def definition(self):
        return json.loads(PACK.read_text(encoding="utf-8"))

    def test_explicit_tester_output_changes_digest_but_not_any_dependency(self):
        definition = self.definition()
        old = WorkflowDefinition.model_validate(definition)
        definition["nodes"][2]["role_output"] = {"kind": "public-checks", "schema_version": "loom.public-checks/v1"}
        changed = WorkflowDefinition.model_validate(definition)
        self.assertNotEqual(changed.execution_digest, old.execution_digest)
        self.assertEqual(changed.nodes[2].role_output.kind, "public-checks")
        self.assertEqual([n.depends for n in changed.nodes], [n.depends for n in old.nodes])
        self.assertIsNone(changed.nodes[2].evaluation_digest)
        self.assertIsNone(changed.nodes[2].repair)

    def test_wrong_role_schema_permission_and_execution_authority_reject(self):
        output = {"kind": "public-checks", "schema_version": "loom.public-checks/v1"}
        cases = []
        for index in (0, 1, 3, 4, 5, 6):
            definition = self.definition()
            definition["nodes"][index]["role_output"] = output
            cases.append(definition)
        for changes in ({"kind": "public-checks", "schema_version": "loom.public-checks/v2"},
                        {**output, "tests_run": True}):
            definition = self.definition()
            definition["nodes"][2]["role_output"] = changes
            cases.append(definition)
        definition = self.definition()
        definition["nodes"][2].update(role_output=output, evaluation_digest="a" * 64)
        cases.append(definition)
        definition = self.definition()
        definition["nodes"][2].update(role_output=output, repair={"schema_version": "loom.rebind/v1",
            "development_digest": "b" * 64, "max_repairs": 1, "stop_on_duplicate_source": True})
        cases.append(definition)
        definition = self.definition()
        definition["nodes"][2]["role_output"] = output
        definition["roles"][2]["tools"].remove("artifacts.write")
        cases.append(definition)
        for index, definition in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(definition)

    def test_new_kind_does_not_change_absent_or_null_old_plan_digest(self):
        definition = self.definition()
        expected = copy.deepcopy(definition)
        for node in expected["nodes"]:
            node["evaluation_digest"] = None
        for node in definition["nodes"]:
            node["role_output"] = None
        validated = WorkflowDefinition.model_validate(definition)
        self.assertEqual(validated.model_dump(mode="json"), expected)
        self.assertEqual(validated.execution_digest, digest(expected))


if __name__ == "__main__":
    unittest.main()
