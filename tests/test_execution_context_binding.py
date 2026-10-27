"""Real SQLite authority tests; no model, Docker, network or private files."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from loom.artifacts import ArtifactService, ExecutionContextError
from loom.operator import OperatorService
from loom.store import BudgetExceeded, Claim, Conflict, StaleClaim, Store, canonical, digest
from loom.workflow import WorkflowService


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
BODY = {"schema_version": "loom.requirements/v1", "summary": "Authenticated draft, not verified truth.",
        "items": [{"id": "scope", "text": "Keep frozen acceptance and authority unchanged."}], "uncertainties": []}


class ExecutionContextBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "context.db")
        self.workflow = WorkflowService(self.store)
        self.artifacts = ArtifactService(self.store)

    def prepare(self, *, selectors=True, required=True, grant=True, hard_dependency=False,
                target_read=True, pending_grant=False):
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        source, target = copy.deepcopy(definition["nodes"][0]), copy.deepcopy(definition["nodes"][3])
        source["id"] = "source"
        target.update(id="target", depends=["source"] if hard_dependency else [])
        if selectors:
            target["input_artifacts"] = [{"source_node": "source", "kind": "requirements",
                                          "schema_version": "loom.requirements/v1", "required": required}]
        if not target_read:
            next(role for role in definition["roles"] if role["id"] == "developer")["tools"].remove("artifacts.read")
        definition["nodes"] = [source, target]
        goal = self.workflow.create_goal("a", "goal", title="Bound exact auxiliary context",
                                         description="Never promote candidate text to authority.", acceptance=["tests.pass"])
        self.run = goal["run_id"]
        saved = self.workflow.save_plan("a", self.run, "plan", definition, 0)
        approved = self.workflow.approve_plan("a", self.run, "approve", saved["revision"], saved["digest"])
        self.plan = saved["digest"]
        self.source, self.target = approved["task_ids"]["source"], approved["task_ids"]["target"]
        self.producer = self.store.claim_next("a", self.run, "alice", lease_seconds=3600)
        self.candidate = self.save_source("candidate")
        if pending_grant:
            self.grant(self.candidate, "pending-grant", target_epoch=0)
        self.claim = self.store.claim_next("a", self.run, "bob", lease_seconds=3600)
        if grant and not pending_grant and self.claim is not None:
            self.grant(self.candidate)

    def save_source(self, key, body=None):
        return self.artifacts.save_candidate(self.producer, key, "requirements", copy.deepcopy(body or BODY), actor="subject:alice")

    def grant(self, candidate, key="grant", *, target_epoch=None):
        return self.artifacts.approve_input("a", self.run, self.target, candidate["artifact_id"], key,
            actor="subject:operator", expected_plan_digest=self.plan, expected_source_epoch=candidate["source_epoch"],
            expected_target_epoch=self.claim.epoch if target_epoch is None else target_epoch,
            expected_revision=candidate["revision"], expected_content_hash=candidate["content_hash"])

    def counts(self):
        with self.store.connect() as db:
            return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in
                         ("events", "outbox", "receipts", "reservations", "effects", "artifact_candidates", "artifact_input_grants"))

    def admit(self, snapshot, **changes):
        args = dict(reservation_id="reservation", max_cost=10, max_tokens=10,
                    operation_id="model-generation:1", request={"route": "deepseek-flash", "prompt_digest": digest(["final prompt"])})
        args.update(changes)
        return self.artifacts.admit_execution_effect(self.claim, snapshot, **args)

    def binding_events(self):
        return [event for event in self.store.events("a", self.run) if event["type"] == "execution.context_bound"]

    def test_capture_and_assert_are_read_only_exact_and_bounded(self):
        self.prepare()
        before, state = self.counts(), self.store.snapshot("a", self.run)
        snapshot = self.artifacts.capture_execution_context(self.claim)
        self.assertEqual(snapshot["schema_version"], "loom.execution-context/v1")
        self.assertEqual((snapshot["worker_id"], snapshot["target_epoch"]), ("bob", 1))
        self.assertTrue(snapshot["untrusted_data"])
        self.assertFalse(snapshot["producer_verified"])
        self.assertFalse(snapshot["counts_as_success"])
        self.assertEqual(snapshot["items"][0]["body"], BODY)
        self.assertNotIn("execution_connected", snapshot)
        self.assertEqual(snapshot["bytes"], len(canonical(snapshot).encode("utf-8")))
        unsigned = {key: value for key, value in snapshot.items() if key not in {"bytes", "context_digest"}}
        self.assertEqual(snapshot["context_digest"], digest(unsigned))
        self.assertEqual(self.artifacts.assert_execution_context(self.claim, snapshot), snapshot)
        with self.store.transaction() as db:
            self.assertEqual(self.artifacts.assert_execution_context_in(db, self.claim, snapshot), snapshot)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run), state)

    def test_missing_required_input_fails_before_any_reservation(self):
        self.prepare(grant=False)
        before = self.counts()
        with self.assertRaises(ExecutionContextError) as caught:
            self.artifacts.capture_execution_context(self.claim)
        self.assertEqual(caught.exception.reason, "CONTEXT_REQUIRED_INPUT_MISSING")
        self.assertEqual(self.counts(), before)

    def test_optional_missing_input_is_explicit_and_later_grant_changes_snapshot(self):
        self.prepare(grant=False, required=False)
        snapshot = self.artifacts.capture_execution_context(self.claim)
        self.assertEqual(snapshot["items"], [])
        self.assertFalse(snapshot["missing_inputs"][0]["required"])
        self.grant(self.candidate)
        before = self.counts()
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)
        self.assertEqual(self.counts(), before)

    def test_pending_epoch_zero_grant_does_not_follow_first_claim(self):
        self.prepare(pending_grant=True)
        self.assertEqual(self.claim.epoch, 1)
        with self.assertRaises(ExecutionContextError):
            self.artifacts.capture_execution_context(self.claim)
        self.grant(self.candidate, "current-epoch-grant")
        self.assertEqual(self.artifacts.capture_execution_context(self.claim)["target_epoch"], 1)

    def test_new_target_epoch_cannot_inherit_snapshot_or_grant(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        old = self.claim
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE tenant='a' AND id=?", (self.target,))
        self.claim = self.store.claim_next("a", self.run, "bob", lease_seconds=3600)
        self.assertEqual(self.claim.epoch, 2)
        with self.assertRaises(StaleClaim):
            self.artifacts.assert_execution_context(old, snapshot)
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)
        self.grant(self.candidate, "epoch-two")
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)
        fresh = self.artifacts.capture_execution_context(self.claim)
        self.assertEqual(fresh["target_epoch"], 2)

    def test_exact_admission_audit_contains_refs_and_no_candidate_text(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        grant = self.admit(snapshot)
        self.assertTrue(grant["dispatch"])
        self.assertEqual(grant["context_digest"], snapshot["context_digest"])
        event = self.binding_events()[0]
        payload = event["payload"]
        self.assertEqual((payload["call_id"], payload["reservation_id"]), (grant["call_id"], "reservation"))
        self.assertEqual(payload["refs"][0]["content_hash"], self.candidate["content_hash"])
        self.assertEqual(payload["context_item_count"], 1)
        self.assertEqual(payload["context_bytes"], snapshot["bytes"])
        self.assertNotIn("body", canonical(payload))
        self.assertNotIn(BODY["summary"], canonical(payload))
        with self.store.connect() as db:
            effect = db.execute("SELECT * FROM effects WHERE id=?", (grant["call_id"],)).fetchone()
        self.assertEqual(effect["request_hash"], digest({"route": "deepseek-flash", "prompt_digest": digest(["final prompt"]),
                                                        "context_digest": snapshot["context_digest"]}))
        self.assertTrue(self.store.verify_events(self.store.events("a", self.run)))

    def test_concurrent_identical_admission_dispatches_once_and_records_once(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.admit(copy.deepcopy(snapshot)), range(6)))
        self.assertEqual(sum(item["dispatch"] for item in results), 1)
        self.assertEqual(len({item["call_id"] for item in results}), 1)
        self.assertEqual(len(self.binding_events()), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM effects").fetchone()[0], 1)

    def test_context_event_failure_rolls_back_budget_intent_outbox_and_event(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        before, state = self.counts(), self.store.snapshot("a", self.run)
        original = self.store._event
        def fail(db, tenant, run_id, kind, *args):
            result = original(db, tenant, run_id, kind, *args)
            if kind == "execution.context_bound":
                raise RuntimeError("injected binding event failure")
            return result
        with patch.object(self.store, "_event", side_effect=fail), self.assertRaises(RuntimeError):
            self.admit(snapshot)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run), state)

    def test_budget_and_changed_prompt_fail_without_a_second_admission(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        before = self.counts()
        with self.assertRaises(BudgetExceeded):
            self.admit(snapshot, max_cost=10**12)
        self.assertEqual(self.counts(), before)
        self.admit(snapshot)
        before = self.counts()
        with self.assertRaises(Conflict):
            self.admit(snapshot, request={"route": "deepseek-flash", "prompt_digest": digest("other prompt")})
        self.assertEqual(self.counts(), before)

    def test_new_revision_or_new_grant_requires_recapture(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        # A new grant for the identical candidate is still a new approval ref.
        self.grant(self.candidate, "second-grant")
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)
        snapshot = self.artifacts.capture_execution_context(self.claim)
        newer = self.save_source("same-content-new-revision")
        self.assertEqual(newer["content_hash"], self.candidate["content_hash"])
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)
        self.grant(newer, "new-revision-grant")
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)

    def test_source_expiry_invalidation_and_epoch_change_are_rejected(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE tenant='a' AND id=?", (self.source,))
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)
        self.producer = self.store.claim_next("a", self.run, "alice", lease_seconds=3600)
        self.assertEqual(self.producer.epoch, 2)
        with self.assertRaises(ExecutionContextError):
            self.artifacts.assert_execution_context(self.claim, snapshot)

    def test_forged_body_hash_refs_identity_and_trust_flags_fail_exact_comparison(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        mutations = (lambda item: item.update(worker_id="another-worker"),
                     lambda item: item.update(tenant_id="another-tenant"),
                     lambda item: item.update(run_id="another-run"),
                     lambda item: item.update(plan_digest="0" * 64),
                     lambda item: item.update(producer_verified=True),
                     lambda item: item["items"][0].update(grant_id="forged"),
                     lambda item: item["items"][0]["body"].update(summary="forged body"),
                     lambda item: item["items"][0].update(content_hash="0" * 64))
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = copy.deepcopy(snapshot)
                mutate(changed)
                # A caller can recalculate a plain hash; that must not help.
                changed["context_digest"] = digest({k: v for k, v in changed.items() if k not in {"bytes", "context_digest"}})
                before = self.counts()
                with self.assertRaises(ExecutionContextError):
                    self.admit(changed)
                self.assertEqual(self.counts(), before)

    def test_bad_provenance_events_content_and_frozen_contract_fail_closed(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER artifact_candidates_no_update")
            db.execute("UPDATE artifact_candidates SET source_event_hash=? WHERE id=?", ("0" * 64, self.candidate["artifact_id"]))
        with self.assertRaises(ExecutionContextError):
            self.admit(snapshot)
        with self.store.transaction() as db:
            db.execute("UPDATE artifact_candidates SET source_event_hash=? WHERE id=?", (self.candidate["source_event_hash"], self.candidate["artifact_id"]))
            raw = json.loads(db.execute("SELECT contract FROM workflow_tasks WHERE task_id=?", (self.target,)).fetchone()[0])
            raw["tools"].append("host.shell")
            db.execute("UPDATE workflow_tasks SET contract=? WHERE task_id=?", (canonical(raw), self.target))
        with self.assertRaises(ExecutionContextError):
            self.artifacts.assert_execution_context(self.claim, snapshot)

    def test_missing_read_capability_and_cross_worker_claim_fail(self):
        self.prepare(grant=False, target_read=False)
        with self.assertRaises(ExecutionContextError):
            self.artifacts.capture_execution_context(self.claim)
        wrong = Claim("a", self.run, self.target, "alice", self.claim.epoch, self.claim.lease_until)
        with self.assertRaises(StaleClaim):
            self.artifacts.capture_execution_context(wrong)

    def test_pause_and_termination_do_not_mint_context_dispatch(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        controls = OperatorService(self.store)
        controls.pause("a", self.run, "pause", "subject:operator", "Pause before admission")
        before = self.counts()
        with self.assertRaises(Conflict):
            self.admit(snapshot)
        self.assertEqual(self.counts(), before)
        controls.terminate("a", self.run, "terminate", "subject:operator", "Stop without dispatch")
        with self.assertRaises(StaleClaim):
            self.admit(snapshot)
        self.assertEqual(len(self.binding_events()), 0)

    def test_submission_rechecks_current_context_and_is_standard_submitted_only(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        output = {"source": "a candidate", "verified": True}
        result = self.artifacts.submit_execution_output(self.claim, "submit", output, snapshot)
        self.assertEqual(result, {"task_id": self.target, "status": "submitted", "content_hash": digest(output)})
        self.assertEqual(self.artifacts.submit_execution_output(self.claim, "submit", output, snapshot), result)
        self.assertEqual(self.store.snapshot("a", self.run)["tasks"][1]["status"], "submitted")
        changed = copy.deepcopy(snapshot)
        changed["context_digest"] = "0" * 64
        with self.assertRaises(Conflict):
            self.artifacts.submit_execution_output(self.claim, "submit", output, changed)

    def test_cached_submit_binds_body_flags_bytes_and_provenance_not_only_claimed_digest(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        output = {"source": "fixture"}
        result = self.artifacts.submit_execution_output(self.claim, "cached-submit", output, snapshot)
        before, state = self.counts(), self.store.snapshot("a", self.run)
        mutations = {
            "body": lambda item: item["items"][0]["body"].update(summary="changed after submission"),
            "flag": lambda item: item.update(producer_verified=True),
            "bytes": lambda item: item.update(bytes=item["bytes"] + 1),
            "refs": lambda item: item["items"][0].update(grant_id="changed-grant-ref"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(snapshot)
                mutate(changed)
                self.assertEqual(changed["context_digest"], snapshot["context_digest"])
                with self.assertRaises(Conflict):
                    self.artifacts.submit_execution_output(self.claim, "cached-submit", output, changed)
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.store.snapshot("a", self.run), state)
        # The completed task no longer has a running claim; an exact historical
        # retry must still read the original receipt without new writes.
        self.assertEqual(self.artifacts.submit_execution_output(
            self.claim, "cached-submit", output, copy.deepcopy(snapshot)), result)
        self.assertEqual(self.counts(), before)

    def test_submit_receipt_binding_bounds_bad_structures_before_receipt_lookup(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        self.artifacts.submit_execution_output(self.claim, "cached-submit", {}, snapshot)
        before = self.counts()
        cycle = {"context_digest": snapshot["context_digest"]}
        cycle["cycle"] = cycle
        oversized = {"context_digest": snapshot["context_digest"], "text": "x" * 65537}
        invalid = {"context_digest": snapshot["context_digest"], "object": object()}
        nonfinite = {"context_digest": snapshot["context_digest"], "number": float("nan")}
        for label, changed in (("cycle", cycle), ("oversized", oversized),
                               ("non-json", invalid), ("nonfinite", nonfinite)):
            with self.subTest(label=label), self.assertRaises(ExecutionContextError):
                self.artifacts.submit_execution_output(self.claim, "cached-submit", {}, changed)
            self.assertEqual(self.counts(), before)

    def test_revision_changed_after_model_admission_prevents_submission(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        dispatch = self.admit(snapshot)
        # Model response is a local fixture; settle normally, then invalidate
        # context. An external request admitted earlier is not claimed undone.
        self.store.finish_effect(self.claim, dispatch["call_id"], {"simulated": True})
        self.store.settle("a", "reservation", actual_cost=3, actual_tokens=4)
        self.save_source("new-revision")
        before = self.counts()
        with self.assertRaises(ExecutionContextError):
            self.artifacts.submit_execution_output(self.claim, "late-submit", {"source": "old result"}, snapshot)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run)["tasks"][1]["status"], "running")
        self.assertEqual(self.store.snapshot("a", self.run)["spent"], 3)

    def test_submit_event_and_receipt_failure_roll_back_state_with_output(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        before, state = self.counts(), self.store.snapshot("a", self.run)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_submit_receipt BEFORE INSERT ON receipts WHEN NEW.key='failed-submit' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.artifacts.submit_execution_output(self.claim, "failed-submit", {"source": "fixture"}, snapshot)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.snapshot("a", self.run), state)

    def test_no_selector_none_preserves_effect_request_and_submission_receipt_shape(self):
        self.prepare(selectors=False, grant=False)
        self.assertIsNone(self.artifacts.capture_execution_context(self.claim))
        request = {"route": "legacy", "prompt_digest": digest("legacy")}
        grant = self.admit(None, request=request)
        self.assertNotIn("context_digest", grant)
        self.assertEqual(self.binding_events(), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT request_hash FROM effects WHERE id=?", (grant["call_id"],)).fetchone()[0], digest(request))
        self.store.finish_effect(self.claim, grant["call_id"], {"fixture": True})
        self.store.settle("a", "reservation", actual_cost=0, actual_tokens=0)
        output = {"source": "legacy"}
        submitted = self.artifacts.submit_execution_output(self.claim, "submit-legacy", output, None)
        self.assertEqual(self.store.submit_output(self.claim, "submit-legacy", output), submitted)
        expected_args = {"run_id": self.run, "task_id": self.target, "epoch": self.claim.epoch,
                         "worker": "bob", "output": output}
        with self.store.connect() as db:
            receipt = db.execute("SELECT request_hash FROM receipts WHERE key='submit-legacy'").fetchone()[0]
        self.assertEqual(receipt, digest({"operation": "submit_output", "args": expected_args}))

    def test_none_cannot_skip_declared_inputs_or_reuse_legacy_receipt(self):
        self.prepare()
        for operation in (lambda: self.admit(None),
                          lambda: self.artifacts.submit_execution_output(self.claim, "none-submit", {}, None)):
            before = self.counts()
            with self.assertRaises(ExecutionContextError):
                operation()
            self.assertEqual(self.counts(), before)

    def test_auxiliary_data_cannot_let_a_blocked_target_capture_or_claim(self):
        self.prepare(hard_dependency=True)
        self.assertIsNone(self.claim)
        self.grant(self.candidate, "pending-target", target_epoch=0)
        forged = Claim("a", self.run, self.target, "bob", 0, 0)
        with self.assertRaises(StaleClaim):
            self.artifacts.capture_execution_context(forged)
        self.store.submit_output(self.producer, "source-submit", {})
        self.assertIsNone(self.store.claim_next("a", self.run, "bob"))

    def test_snapshot_renewal_is_stable_and_size_overflow_has_static_reason(self):
        self.prepare()
        snapshot = self.artifacts.capture_execution_context(self.claim)
        self.claim = self.store.renew_claim(self.claim, lease_seconds=3600)
        self.assertEqual(self.artifacts.capture_execution_context(self.claim), snapshot)
        with patch("loom.artifacts.MAX_CONTEXT_BYTES", snapshot["bytes"] - 100), self.assertRaises(ExecutionContextError) as caught:
            self.artifacts.capture_execution_context(self.claim)
        self.assertEqual(caught.exception.reason, "CONTEXT_TOO_LARGE")
        self.assertNotIn(BODY["summary"], str(caught.exception))


if __name__ == "__main__":
    unittest.main()
