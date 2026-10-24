"""Explicit role contracts and atomic model-origin candidates, real SQLite.

All model results are fixtures. No provider, Docker or private configuration is
used, and schema validity is never presented as independent verification.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
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
from loom.operator import OperatorService
from loom.store import Claim, Conflict, InvalidInput, NotFound, StaleClaim, Store, canonical, digest
from loom.workflow import WorkflowDefinition, WorkflowService


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
REQUIREMENTS = {"schema_version": "loom.requirements/v1", "summary": "Read-only requirements draft",
                "items": [{"id": "scope", "text": "Keep frozen acceptance unchanged."}], "uncertainties": []}
DESIGN = {"schema_version": "loom.design/v1", "overview": "Read-only design draft",
          "components": [{"id": "core", "responsibility": "Transform bounded input."}], "steps": [], "risks": []}


class RoleOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "role.db")
        self.workflow = WorkflowService(self.store)
        self.artifacts = ArtifactService(self.store)

    def prepare(self, *, kind="requirements", authorized=True, context=False):
        self.kind = kind
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        source = copy.deepcopy(definition["nodes"][0 if kind == "requirements" else 1])
        source.update(id="generate", depends=[], priority=70)
        if authorized:
            source["role_output"] = {"kind": kind, "schema_version": (REQUIREMENTS if kind == "requirements" else DESIGN)["schema_version"]}
        review = copy.deepcopy(definition["nodes"][-1])
        review.update(id="review", depends=["generate"])
        nodes = [source, review]
        if context:
            upstream = copy.deepcopy(definition["nodes"][0])
            upstream.update(id="input", depends=[], priority=90)
            source["input_artifacts"] = [{"source_node": "input", "kind": "requirements",
                                          "schema_version": "loom.requirements/v1", "required": True}]
            nodes.insert(0, upstream)
        definition["nodes"] = nodes
        goal = self.workflow.create_goal("a", "goal", title="Generate a bounded role draft",
                                         description="No verification is implied.", acceptance=["schema.valid"])
        self.run = goal["run_id"]
        saved = self.workflow.save_plan("a", self.run, "plan", definition, 0)
        approved = self.workflow.approve_plan("a", self.run, "approve", saved["revision"], saved["digest"])
        self.plan = saved["digest"]
        self.task_id, self.review_id = approved["task_ids"]["generate"], approved["task_ids"]["review"]
        if context:
            self.producer = self.store.claim_next("a", self.run, "upstream", lease_seconds=3600)
            self.input_artifact = self.artifacts.save_candidate(self.producer, "input", "requirements", REQUIREMENTS, actor="subject:upstream")
            self.store.submit_output(self.producer, "input-submit", {"artifact_id": self.input_artifact["artifact_id"]})
        self.claim = self.store.claim_next("a", self.run, "worker-one", lease_seconds=3600)
        self.assertEqual(self.claim.task_id, self.task_id)
        if context:
            self.artifacts.approve_input("a", self.run, self.task_id, self.input_artifact["artifact_id"], "input-grant",
                actor="subject:operator", expected_plan_digest=self.plan, expected_source_epoch=self.producer.epoch,
                expected_target_epoch=self.claim.epoch, expected_revision=1,
                expected_content_hash=self.input_artifact["content_hash"])
        self.snapshot = self.artifacts.capture_execution_context(self.claim)
        self.request = {"route": "deepseek-flash", "provider": "deepseek", "model": "deepseek-flash",
            "prompt_digest": digest(["fixture messages"]), "max_output_tokens": 128, "plan_digest": self.plan,
            "role_output": {"kind": kind, "schema_version": (REQUIREMENTS if kind == "requirements" else DESIGN)["schema_version"]},
            "response_contract": "role-artifact/v1"}
        self.reservation_id = f"python:{self.task_id}:{self.claim.epoch}"
        self.body = REQUIREMENTS if kind == "requirements" else DESIGN
        self.result = asdict(ModelResult(canonical(self.body), "deepseek-flash", "deepseek", "deepseek-flash",
                                        ModelUsage(12, 8, 20), "chatcmpl-role-fixture", "stop", "deepseek-flash"))

    def admit(self, request=None):
        result = self.artifacts.admit_execution_effect(self.claim, self.snapshot, reservation_id=self.reservation_id,
            max_cost=500, max_tokens=100, operation_id=f"model-generation:{self.claim.epoch}", request=request or self.request)
        self.call_id = result["call_id"]
        return result

    def known(self, *, result=None, request=None, cost=14, tokens=20):
        self.admit(request)
        self.store.finish_effect(self.claim, self.call_id, self.result if result is None else result)
        self.store.settle("a", self.reservation_id, actual_cost=cost, actual_tokens=tokens)

    def submit(self, key="role-submit", **changes):
        args = {"call_id": self.call_id, "reservation_id": self.reservation_id,
                "request": self.request, "snapshot": self.snapshot}
        args.update(changes)
        return self.artifacts.submit_role_output(self.claim, key, **args)

    def counts(self):
        with self.store.connect() as db:
            return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
                         ("artifact_candidates", "events", "outbox", "receipts", "effects", "reservations"))

    def test_requirements_model_origin_schema_and_submission_are_distinct_from_verification(self):
        self.prepare()
        self.known()
        result = self.submit()
        self.assertEqual(result["status"], "submitted")
        self.assertTrue(result["schema_validated"])
        self.assertTrue(result["model_source_recorded"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["producer_verified"])
        self.assertFalse(result["counts_as_success"])
        artifact = self.artifacts.get_artifact("a", self.run, result["artifact_id"])
        self.assertEqual(artifact, result["artifact"])
        self.assertEqual(artifact["origin"], "model-gateway-response")
        self.assertEqual(artifact["body"], REQUIREMENTS)
        provenance = artifact["model_provenance"]
        self.assertEqual((provenance["call_id"], provenance["plan_digest"], provenance["content_hash"]),
                         (self.call_id, self.plan, digest(REQUIREMENTS)))
        self.assertEqual(provenance["usage"]["total_tokens"], 20)
        self.assertNotIn("request_id", canonical(provenance))
        self.assertNotIn("content", provenance)
        self.assertNotIn("request", provenance)
        self.assertEqual(self.artifacts.list_artifacts("a", self.run)[0]["model_provenance"], provenance)
        self.assertIsNone(self.store.claim_next("a", self.run, "reviewer", channel="verification"))
        with self.store.connect() as db:
            output = json.loads(db.execute("SELECT output FROM tasks WHERE id=?", (self.task_id,)).fetchone()[0])
        self.assertEqual(output["artifact_content_hash"], artifact["content_hash"])
        self.assertFalse(output["producer_verified"])
        self.assertEqual(self.store.snapshot("a", self.run)["spent"], 14)
        self.assertTrue(self.store.verify_events(self.store.events("a", self.run)))

    def test_design_uses_guarded_auxiliary_context_and_safe_source_metadata(self):
        self.prepare(kind="design", context=True)
        self.known()
        result = self.submit()
        artifact = self.artifacts.get_artifact("a", self.run, result["artifact_id"])
        self.assertEqual(artifact["body"], DESIGN)
        self.assertEqual(artifact["model_provenance"]["context_digest"], self.snapshot["context_digest"])
        self.assertEqual(result["execution_context_digest"], self.snapshot["context_digest"])

    def test_role_without_explicit_output_cannot_claim_model_candidate(self):
        self.prepare(authorized=False)
        self.known()
        before = self.counts()
        with self.assertRaises(Conflict):
            self.submit()
        self.assertEqual(self.counts(), before)

    def test_known_response_and_settled_usage_are_both_mandatory(self):
        self.prepare()
        self.admit()
        before = self.counts()
        with self.assertRaises(Conflict):
            self.submit()
        self.assertEqual(self.counts(), before)
        self.store.finish_effect(self.claim, self.call_id, self.result)
        with self.assertRaises(Conflict):
            self.submit()
        self.store.settle("a", self.reservation_id, actual_cost=14, actual_tokens=21)
        with self.assertRaises(Conflict):
            self.submit()

    def test_call_reservation_request_and_plan_cannot_be_substituted(self):
        self.prepare()
        self.known()
        for changes in ({"call_id": "0" * 64}, {"reservation_id": "another-reservation"},
                        {"request": {**self.request, "plan_digest": "0" * 64}},
                        {"request": {**self.request, "prompt_digest": "0" * 64}},
                        {"request": {**self.request, "max_output_tokens": 129}}):
            with self.subTest(changes=list(changes)), self.assertRaises(Conflict):
                self.submit(**changes)
        self.assertEqual(self.artifacts.list_artifacts("a", self.run), [])

    def test_changed_route_metadata_matching_effect_hash_still_fails_frozen_contract(self):
        self.prepare()
        changed = {**self.request, "route": "different-route"}
        result = {**self.result, "route_alias": "different-route"}
        self.known(request=changed, result=result)
        with self.assertRaises(Conflict):
            self.submit(request=changed)

    def test_model_metadata_mismatch_and_gateway_event_tamper_are_rejected(self):
        self.prepare()
        result = {**self.result, "route_alias": "another-route"}
        self.known(result=result)
        with self.assertRaises(Conflict):
            self.submit()
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET hash=? WHERE json_extract(envelope,'$.type')='tool.done'", ("0" * 64,))
        with self.assertRaises(Conflict):
            self.submit()

    def test_invalid_json_duplicate_schema_and_truncated_leave_no_candidate_or_submission(self):
        self.prepare()
        self.admit()
        bad = {**self.result, "content": '{"schema_version":"loom.requirements/v1","summary":"a","summary":"b","items":[]}'}
        self.store.finish_effect(self.claim, self.call_id, bad)
        self.store.settle("a", self.reservation_id, actual_cost=14, actual_tokens=20)
        before = self.counts()
        with self.assertRaises(RoleOutputError) as caught:
            self.submit()
        self.assertEqual(str(caught.exception), "ROLE_OUTPUT_INVALID")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run)["tasks"][0]["status"], "running")
        self.assertEqual(self.store.snapshot("a", self.run)["spent"], 14)

    def test_schema_decoder_strict_errors_do_not_reflect_response(self):
        sentinel = "DO_NOT_ECHO_PRIVATE_SENTINEL"
        cases = [sentinel, "```json\n" + canonical(REQUIREMENTS) + "\n```",
                 canonical({**REQUIREMENTS, "producer_verified": True}),
                 '{"schema_version":"loom.requirements/v1","summary":"a","summary":"b","items":[]}',
                 canonical(REQUIREMENTS) + " " * 32768,
                 '{"schema_version":"loom.requirements/v1","summary":"a","items":[NaN]}',
                 '[' * 50 + '0' + ']' * 50, '\ud800']
        for value in cases:
            with self.subTest(index=cases.index(value)), self.assertRaises(RoleOutputError) as caught:
                decode_role_output("requirements", value)
            self.assertEqual(str(caught.exception), "ROLE_OUTPUT_INVALID")
        self.assertEqual(decode_role_output("requirements", canonical(REQUIREMENTS)), REQUIREMENTS)
        self.assertEqual(decode_role_output("design", canonical(DESIGN)), DESIGN)
        with self.assertRaises(RoleOutputError):
            decode_role_output("public-checks", "{}")

    def test_truncated_response_cannot_be_promoted_even_when_body_is_valid(self):
        self.prepare()
        self.known(result={**self.result, "finish_reason": "length"})
        before, state = self.counts(), self.store.snapshot("a", self.run)
        with self.assertRaises(RoleOutputError):
            self.submit()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run), state)
        self.assertEqual(state["spent"], 14)
        self.assertEqual(self.artifacts.list_artifacts("a", self.run), [])

    def test_generation_event_or_standard_submit_failure_rolls_back_candidate_and_status(self):
        self.prepare()
        self.known()
        original = self.store._event
        for fail_type in ("role.candidate_generated", "task.output_submitted"):
            before, state = self.counts(), self.store.snapshot("a", self.run)
            def fail(db, tenant, run_id, kind, *args):
                result = original(db, tenant, run_id, kind, *args)
                if kind == fail_type:
                    raise RuntimeError("injected final event failure")
                return result
            with patch.object(self.store, "_event", side_effect=fail), self.assertRaises(RuntimeError):
                self.submit("failed-" + fail_type)
            self.assertEqual(self.counts(), before)
            self.assertEqual(self.store.snapshot("a", self.run), state)
            self.assertEqual(self.store.snapshot("a", self.run)["spent"], 14)

    def test_receipt_failure_rolls_back_both_candidate_and_submission(self):
        self.prepare()
        self.known()
        before, state = self.counts(), self.store.snapshot("a", self.run)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON receipts WHEN NEW.key='bad-receipt' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit("bad-receipt")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run), state)

    def test_concurrent_same_key_is_one_candidate_event_and_submission(self):
        self.prepare()
        self.known()
        before = self.counts()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.submit(), range(6)))
        self.assertTrue(all(result == results[0] for result in results))
        after = self.counts()
        self.assertEqual(tuple(a-b for a,b in zip(after,before)), (1, 2, 2, 1, 0, 0))

    def test_cached_receipt_binds_whole_request_and_snapshot(self):
        self.prepare(kind="design", context=True)
        self.known()
        result = self.submit()
        before = self.counts()
        for request in ({**self.request, "prompt_digest": "0" * 64}, {**self.request, "max_output_tokens": 129}):
            with self.subTest(request=list(request)), self.assertRaises(Conflict):
                self.submit(request=request)
        for label in ("body", "flag", "bytes", "refs"):
            changed = copy.deepcopy(self.snapshot)
            if label == "body":
                changed["items"][0]["body"]["summary"] = "forged"
            elif label == "flag":
                changed["producer_verified"] = True
            elif label == "bytes":
                changed["bytes"] += 1
            else:
                changed["items"][0]["grant_id"] = "forged"
            with self.subTest(label=label), self.assertRaises(Conflict):
                self.submit(snapshot=changed)
        self.assertEqual(self.submit(), result)
        self.assertEqual(self.counts(), before)

    def test_pause_cancel_and_stale_claim_cannot_save_a_returned_body(self):
        self.prepare()
        self.known()
        controls = OperatorService(self.store)
        controls.pause("a", self.run, "pause", "subject:operator", "Pause before commit")
        before = self.counts()
        with self.assertRaises(Conflict):
            self.submit()
        self.assertEqual(self.counts(), before)
        controls.terminate("a", self.run, "terminate", "subject:operator", "Stop returned draft")
        with self.assertRaises(StaleClaim):
            self.submit()
        self.assertEqual(self.artifacts.list_artifacts("a", self.run), [])

    def test_context_source_invalidation_after_usage_cannot_save_partial_role_candidate(self):
        self.prepare(kind="design", context=True)
        self.known()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET epoch=epoch+1 WHERE id=?", (self.producer.task_id,))
        before = self.counts()
        with self.assertRaises(ExecutionContextError):
            self.submit()
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.artifacts.list_artifacts("a", self.run)), 1)

    def test_provenance_and_effect_result_tamper_fail_closed_on_read(self):
        self.prepare()
        self.known()
        saved = self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE effects SET result=? WHERE id=?", (canonical({**self.result, "content": "{}"}), self.call_id))
        with self.assertRaises(Conflict):
            self.artifacts.get_artifact("a", self.run, saved["artifact_id"])

    def test_cross_tenant_and_run_are_rejected(self):
        self.prepare()
        self.known()
        saved = self.submit()
        with self.assertRaises(NotFound):
            self.artifacts.get_artifact("another", self.run, saved["artifact_id"])
        with self.assertRaises(NotFound):
            self.artifacts.get_artifact("a", "other-run", saved["artifact_id"])

    def test_worker_origin_survives_nullable_column_migration_without_event_rewrite(self):
        self.prepare(authorized=False)
        saved = self.artifacts.save_candidate(self.claim, "worker", "requirements", REQUIREMENTS, actor="subject:worker-one")
        events = self.store.events("a", self.run)
        with self.store.transaction() as db:
            db.execute("ALTER TABLE artifact_candidates DROP COLUMN origin")
            db.execute("ALTER TABLE artifact_candidates DROP COLUMN provenance")
        migrated = ArtifactService(self.store)
        self.assertEqual(migrated.get_artifact("a", self.run, saved["artifact_id"]), saved)
        self.assertEqual(self.store.events("a", self.run), events)
        self.assertNotIn("model_provenance", saved)
        self.assertNotIn("model_source_recorded", saved)


class RoleOutputWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.definition = json.loads(PACK.read_text(encoding="utf-8"))

    def test_absent_or_null_role_output_keeps_complete_old_plan_digest(self):
        old = WorkflowDefinition.model_validate(self.definition)
        expected = copy.deepcopy(self.definition)
        for node in expected["nodes"]:
            node["evaluation_digest"] = None
        self.assertEqual(old.model_dump(mode="json"), expected)
        for node in self.definition["nodes"]:
            node["role_output"] = None
        self.assertEqual(WorkflowDefinition.model_validate(self.definition).execution_digest, digest(expected))

    def test_explicit_output_changes_digest_and_is_frozen_in_node(self):
        old = WorkflowDefinition.model_validate(self.definition)
        self.definition["nodes"][0]["role_output"] = {"kind": "requirements", "schema_version": "loom.requirements/v1"}
        changed = WorkflowDefinition.model_validate(self.definition)
        self.assertNotEqual(old.execution_digest, changed.execution_digest)
        self.assertEqual(changed.nodes[0].role_output.kind, "requirements")
        self.assertEqual([node.depends for node in old.nodes], [node.depends for node in changed.nodes])

    def test_mismatched_role_schema_unknown_fields_and_missing_write_reject(self):
        bad = [
            (0, {"kind": "design", "schema_version": "loom.design/v1"}),
            (1, {"kind": "requirements", "schema_version": "loom.requirements/v1"}),
            (2, {"kind": "requirements", "schema_version": "loom.requirements/v1"}),
            (3, {"kind": "requirements", "schema_version": "loom.requirements/v1"}),
            (0, {"kind": "requirements", "schema_version": "loom.requirements/v2"}),
            (0, {"kind": "requirements", "schema_version": "loom.requirements/v1", "verified": True}),
        ]
        for index, output in bad:
            source = copy.deepcopy(self.definition)
            source["nodes"][index]["role_output"] = output
            with self.subTest(index=index, output=output), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(source)
        self.definition["nodes"][0]["role_output"] = {"kind": "requirements", "schema_version": "loom.requirements/v1"}
        self.definition["roles"][0]["tools"].remove("artifacts.write")
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(self.definition)

    def test_readonly_output_does_not_silently_skip_a_frozen_code_evaluation(self):
        source = copy.deepcopy(self.definition)
        source["nodes"][0]["role_output"] = {"kind": "requirements", "schema_version": "loom.requirements/v1"}
        source["nodes"][0]["evaluation_digest"] = "a" * 64
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(source)


if __name__ == "__main__":
    unittest.main()
