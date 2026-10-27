"""Independent adversarial regressions discovered while reviewing the local store."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from loom.policy import ArtifactSubmission, VerificationSignature
from loom.store import Conflict, NotFound, StaleClaim, Store, digest


class StoreReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "review.db")
        self.run = self.store.create_run(
            "tenant-a", "create-run", acceptance=["tests.pass"], cost_limit=100, token_limit=100
        )["run_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def add_and_claim(self, task_id):
        self.store.add_task("tenant-a", self.run, "add:" + task_id, task_id=task_id, title="Verify one output")
        claim = self.store.claim_next("tenant-a", self.run, "worker:" + task_id)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.task_id, task_id)
        return claim

    def verification(self, claim, output):
        evidence_revision = f"{claim.task_id}:{claim.epoch}"
        signatures = tuple(
            VerificationSignature(
                verifier_id=f"review-signer-{i}", model_id=f"review-model-{i}",
                artifact_hash=digest(output), ruleset_version="kernel-v1",
                evidence_revision=evidence_revision, passed=True, authenticated=True,
            )
            for i in range(2)
        )
        return ArtifactSubmission(
            content_hash=digest(output), ruleset_version="kernel-v1",
            evidence_revision=evidence_revision, evidence_ref="executor:evidence",
            evidence_trusted=True, hard_criteria_passed=True,
            dual_sign="signed", signatures=signatures,
        )

    def test_budget_overrun_freezes_new_external_grants_from_other_live_claims(self):
        first = self.add_and_claim("task-1")
        second = self.add_and_claim("task-2")
        self.store.reserve(first, "billing", max_cost=10, max_tokens=10)
        self.store.settle("tenant-a", "billing", actual_cost=11, actual_tokens=1)
        self.assertEqual(self.store.snapshot("tenant-a", self.run)["status"], "awaiting_human")
        with self.assertRaises(Conflict):
            self.store.begin_effect(second, "external-write", {"path": "/record/1"})

    def test_logical_effect_identity_is_unambiguous_when_ids_contain_separator(self):
        first = self.add_and_claim("task/part")
        second = self.add_and_claim("task")
        grant_one = self.store.begin_effect(first, "write", {"value": 1})
        grant_two = self.store.begin_effect(second, "part/write", {"value": 1})
        self.assertTrue(grant_one["dispatch"])
        self.assertTrue(grant_two["dispatch"])
        self.assertNotEqual(grant_one["call_id"], grant_two["call_id"])

    def test_reordered_request_cannot_obtain_second_grant(self):
        claim = self.add_and_claim("task-1")
        first = self.store.begin_effect(claim, "write", {"key": "a", "value": 1})
        second = self.store.begin_effect(claim, "write", {"value": 1, "key": "a"})
        self.assertTrue(first["dispatch"])
        self.assertFalse(second["dispatch"])
        self.assertEqual(first["call_id"], second["call_id"])
        with self.assertRaises(Conflict):
            self.store.begin_effect(claim, "write", {"value": 2, "key": "a"})

    def test_reservation_cannot_be_settled_from_another_tenant(self):
        claim = self.add_and_claim("task-1")
        self.store.reserve(claim, "billing", max_cost=10, max_tokens=10)
        with self.assertRaises(NotFound):
            self.store.settle("tenant-b", "billing", actual_cost=1, actual_tokens=1)
        snapshot = self.store.snapshot("tenant-a", self.run)
        self.assertEqual(snapshot["reserved"], 10)
        self.assertEqual(snapshot["spent"], 0)

    def test_consumer_failure_rolls_back_both_inbox_and_local_effect(self):
        event_id = self.store.events("tenant-a", self.run)[0]["event_id"]

        def fail(db):
            db.execute("INSERT INTO settings VALUES ('review-consumer-effect', 1)")
            raise RuntimeError("consumer crashed before commit")

        with self.assertRaises(RuntimeError):
            self.store.consume("review-consumer", event_id, fail)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='review-consumer-effect'").fetchone())

        def succeed(db):
            db.execute("INSERT INTO settings VALUES ('review-consumer-effect', 1)")

        self.assertTrue(self.store.consume("review-consumer", event_id, succeed))
        self.assertFalse(self.store.consume("review-consumer", event_id, succeed))

    def test_lease_reassignment_revokes_old_external_write_permission(self):
        old = self.add_and_claim("task-1")
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE tenant=? AND id=?", ("tenant-a", "task-1"))
        current = self.store.claim_next("tenant-a", self.run, "replacement-worker")
        self.assertEqual(current.epoch, old.epoch + 1)
        with self.assertRaises(StaleClaim):
            self.store.begin_effect(old, "write", {"value": 1})
        self.assertTrue(self.store.begin_effect(current, "write", {"value": 1})["dispatch"])

    def test_unresolved_effect_cannot_be_orphaned_by_worker_output_submission(self):
        claim = self.add_and_claim("task-1")
        grant = self.store.begin_effect(claim, "write", {"value": 1})
        with self.assertRaises(Conflict):
            self.store.submit_output(claim, "submit-output", {"result": "claimed done"})
        self.assertEqual(self.store.finish_effect(claim, grant["call_id"], {"done": True})["state"], "done")

    def test_complete_idempotency_key_binds_the_entire_verification_proof(self):
        claim = self.add_and_claim("task-1")
        output = {"result": "pass"}
        proof = self.verification(claim, output)
        self.store.complete(claim, "complete-output", output, verification=proof)
        for changed in (
            replace(proof, evidence_trusted=False),
            replace(proof, hard_criteria_passed=False),
            replace(proof, evidence_ref="different:evidence"),
            replace(proof, signatures=(replace(proof.signatures[0], model_id="other-model"), proof.signatures[1])),
        ):
            with self.subTest(proof=changed):
                with self.assertRaises(Conflict):
                    self.store.complete(claim, "complete-output", output, verification=changed)

    def test_verification_audit_record_preserves_who_signed_and_which_models(self):
        claim = self.add_and_claim("task-1")
        output = {"result": "pass"}
        self.store.complete(claim, "complete-output", output, verification=self.verification(claim, output))
        record = next(event for event in self.store.events("tenant-a", self.run) if event["type"] == "artifact.verified")
        recorded_payload = json.dumps(record["payload"])
        for identity in ("review-signer-0", "review-signer-1", "review-model-0", "review-model-1"):
            self.assertIn(identity, recorded_payload)


if __name__ == "__main__":
    unittest.main()
