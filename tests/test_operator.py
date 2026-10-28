"""Durable operator control tests; no provider, credentials or code execution."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from loom.operator import OperatorService
from loom.store import Conflict, InvalidInput, NotFound, StaleClaim, Store, canonical, digest


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
ACTOR = "operator:test-alice"
EVIDENCE = {"reference": "provider-ledger://synthetic-fixture/entry-1",
            "summary": "Synthetic operator accounting evidence, not real provider data."}


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "operator.db")
        self.ops = OperatorService(self.store)
        self.sequence = 0

    def managed(self, *, tenant="tenant", max_attempts=3, node_cost=1000, node_tokens=10000,
                goal_cost=10000, goal_tokens=100000, child=False):
        self.sequence += 1
        prefix = "fixture:" + str(self.sequence)
        goal = self.ops.workflows.create_goal(tenant, prefix, title="Operator fixture",
            description="Test only; do not invoke a model.", acceptance=["tests.pass"],
            cost_limit=goal_cost, token_limit=goal_tokens)
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        definition["nodes"] = [{"id": "work", "title": "Work", "instruction": "Synthetic work.",
            "role": "developer", "limits": {"cost_limit_microusd": node_cost,
            "token_limit": node_tokens, "max_attempts": max_attempts}}]
        if child:
            definition["nodes"].append({"id": "review", "title": "Review", "instruction": "Synthetic review.",
                "role": "validator", "depends": ["work"], "channel": "verification",
                "limits": {"cost_limit_microusd": 100, "token_limit": 1000, "max_attempts": 3}})
        plan = self.ops.workflows.save_plan(tenant, goal["run_id"], prefix + ":plan", definition, 0)
        approved = self.ops.workflows.approve_plan(tenant, goal["run_id"], prefix + ":approve", plan["revision"], plan["digest"])
        return goal["run_id"], approved["task_ids"]

    def attempt(self, *, state="indeterminate", reserve=True, operation=None, **kwargs):
        run_id, ids = self.managed(**kwargs)
        tenant = kwargs.get("tenant", "tenant")
        claim = self.store.claim_next(tenant, run_id, "fixture-worker", lease_seconds=300)
        reservation_id = f"python:{claim.task_id}:{claim.epoch}"
        if reserve:
            self.store.reserve(claim, reservation_id, max_cost=100, max_tokens=100)
        grant = self.store.begin_effect(claim, operation or f"model-generation:{claim.epoch}",
                                        {"fixture": "not a real request"})
        if state != "intent":
            with self.store.transaction() as db:
                db.execute("UPDATE effects SET state=? WHERE tenant=? AND id=?", (state, tenant, grant["call_id"]))
                db.execute("UPDATE tasks SET status='awaiting_human',lease_until=NULL WHERE tenant=? AND id=?", (tenant, claim.task_id))
                db.execute("UPDATE runs SET status='awaiting_human' WHERE tenant=? AND id=?", (tenant, run_id))
                self.store._event(db, tenant, run_id, "tool.indeterminate", {"call_id": grant["call_id"], "state": state}, "test-fixture")
                self.store._event(db, tenant, run_id, "execution.blocked",
                                  {"task_id": claim.task_id, "reason": "SIMULATED", "run_status": "awaiting_human"}, "test-fixture")
        return claim, grant["call_id"], reservation_id

    def row(self, table, tenant, identifier):
        with self.store.connect() as db:
            return dict(db.execute("SELECT * FROM " + table + " WHERE tenant=? AND id=?", (tenant, identifier)).fetchone())

    def events(self, run_id, kind=None, tenant="tenant"):
        events = self.store.events(tenant, run_id)
        return events if kind is None else [e for e in events if e["type"] == kind]

    def reconcile(self, claim, call_id, *, decision="completed", cost=10, tokens=20, key="reconcile", actor=ACTOR, evidence=None):
        return self.ops.reconcile_effect(claim.tenant_id, claim.run_id, call_id, key, actor, decision,
                                         evidence or EVIDENCE, cost, tokens)

    def test_pause_retains_inflight_claim_effect_and_budget_without_refund(self):
        claim, call_id, reservation_id = self.attempt(state="intent")
        before = self.store.snapshot("tenant", claim.run_id)
        paused = self.ops.pause("tenant", claim.run_id, "pause", ACTOR, "Inspect in-flight work.")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["task_updates"], [])
        self.assertEqual(self.row("tasks", "tenant", claim.task_id)["epoch"], claim.epoch)
        self.assertEqual(self.row("effects", "tenant", call_id)["state"], "intent")
        self.assertEqual(self.row("reservations", "tenant", reservation_id)["state"], "reserved")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["reserved"], before["reserved"])
        with self.assertRaises(Conflict):
            self.store.claim_next("tenant", claim.run_id, "another-worker")

    def test_pause_and_resume_idle_approved_run(self):
        run_id, _ = self.managed()
        self.ops.pause("tenant", run_id, "pause", ACTOR, "Maintenance.")
        result = self.ops.resume("tenant", run_id, "resume", ACTOR, "Maintenance finished.")
        self.assertEqual(result["status"], "open")
        self.assertEqual(result["task_updates"], [])
        self.assertIsNotNone(self.store.claim_next("tenant", run_id, "fresh-worker"))
        self.assertFalse(result["counts_as_success"])

    def test_resume_preview_is_read_only_and_matches_recovery_gates(self):
        claim, call_id, _ = self.attempt()
        before = self.store.snapshot("tenant", claim.run_id)
        event_count = len(self.events(claim.run_id))
        blocked = self.ops.resume_preview("tenant", claim.run_id)
        self.assertFalse(blocked["ready"])
        self.assertEqual(blocked["reason"], "UNKNOWN_EFFECTS")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id), before)
        self.assertEqual(len(self.events(claim.run_id)), event_count)
        self.reconcile(claim, call_id, decision="not_sent", cost=0, tokens=0)
        preview = self.ops.resume_preview("tenant", claim.run_id)
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["retry_task_count"], 1)
        self.assertFalse(preview["counts_as_success"])
        self.assertEqual(self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Resume after accounting.")["status"], "open")
        with self.assertRaises(NotFound):
            self.ops.resume_preview("other", claim.run_id)

    def test_resume_rejects_changed_approved_projection_without_partial_updates(self):
        run_id, ids = self.managed(child=True)
        self.ops.pause("tenant", run_id, "pause", ACTOR, "Inspect.")
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET priority=priority+1 WHERE tenant=? AND id=?", ("tenant", ids["review"]))
        before = self.store.snapshot("tenant", run_id)
        self.assertEqual(self.ops.resume_preview("tenant", run_id)["reason"], "PLAN_BINDING_INVALID")
        with self.assertRaises(Conflict):
            self.ops.resume("tenant", run_id, "resume", ACTOR, "Do not resume modified plan.")
        self.assertEqual(self.store.snapshot("tenant", run_id), before)

    def test_resume_refuses_unknown_effects_and_preserves_receipt_atomicity(self):
        claim, _, _ = self.attempt()
        count = len(self.events(claim.run_id))
        with self.assertRaises(Conflict):
            self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Cannot bypass unknown call.")
        self.assertEqual(len(self.events(claim.run_id)), count)
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["status"], "awaiting_human")
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM receipts WHERE tenant='tenant' AND key='resume'").fetchone())

    def test_resume_refuses_unsettled_reservation_even_without_effect(self):
        run_id, _ = self.managed()
        claim = self.store.claim_next("tenant", run_id, "fixture-worker")
        self.store.reserve(claim, "unbound-reservation", max_cost=10, max_tokens=10)
        self.ops.pause("tenant", run_id, "pause", ACTOR, "Missing call evidence.")
        with self.assertRaises(Conflict):
            self.ops.resume("tenant", run_id, "resume", ACTOR, "No automatic refunds.")
        self.assertEqual(self.store.snapshot("tenant", run_id)["reserved"], 10)

    def test_submitted_output_and_dependency_barrier_survive_pause_resume(self):
        run_id, ids = self.managed(child=True)
        claim = self.store.claim_next("tenant", run_id, "fixture-worker")
        output = {"source": "FAKE SOURCE - NEVER EXECUTED", "verified": True}
        self.store.submit_output(claim, "submit", output)
        self.ops.pause("tenant", run_id, "pause", ACTOR, "Review pending.")
        self.ops.resume("tenant", run_id, "resume", ACTOR, "Resume without bypassing verification.")
        self.assertEqual(self.row("tasks", "tenant", ids["work"])["status"], "submitted")
        self.assertEqual(self.row("tasks", "tenant", ids["work"])["output"], canonical(output))
        self.assertIsNone(self.store.claim_next("tenant", run_id, "cannot-self-verify"))
        self.assertIsNone(self.store.claim_next("tenant", run_id, "validator", channel="verification"))
        self.assertEqual(self.events(run_id, "task.completed"), [])

    def test_draft_cannot_pause_or_resume_but_can_cancel(self):
        goal = self.ops.workflows.create_goal("tenant", "draft", title="Draft", description="Not approved.", acceptance=["tests.pass"])
        for action in (self.ops.pause, self.ops.resume):
            with self.subTest(action=action.__name__), self.assertRaises(Conflict):
                action("tenant", goal["run_id"], action.__name__, ACTOR, "No draft execution.")
        self.assertEqual(self.ops.terminate("tenant", goal["run_id"], "cancel", ACTOR, "Cancel draft.")["status"], "cancelled")

    def test_terminal_runs_cannot_be_reopened_and_cancel_is_idempotent(self):
        run_id, _ = self.managed()
        cancelled = self.ops.terminate("tenant", run_id, "cancel", ACTOR, "Stop project run.")
        self.assertEqual(self.ops.terminate("tenant", run_id, "cancel", ACTOR, "Stop project run."), cancelled)
        for action in (self.ops.pause, self.ops.resume, self.ops.terminate):
            with self.subTest(action=action.__name__), self.assertRaises(Conflict):
                action("tenant", run_id, action.__name__ + ":again", ACTOR, "Cannot reopen terminal.")

    def test_cancel_fences_owners_and_keeps_unknown_accounting(self):
        claim, call_id, reservation_id = self.attempt(state="intent")
        result = self.ops.terminate("tenant", claim.run_id, "cancel", ACTOR, "Terminate unknown external work.")
        task = self.row("tasks", "tenant", claim.task_id)
        self.assertEqual((task["status"], task["epoch"], task["worker"], task["lease_until"]), ("cancelled", claim.epoch + 1, None, None))
        self.assertEqual(self.row("effects", "tenant", call_id)["state"], "indeterminate")
        self.assertEqual(self.row("reservations", "tenant", reservation_id)["state"], "reserved")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["reserved"], 100)
        self.assertFalse(result["counts_as_success"])
        with self.assertRaises(StaleClaim):
            self.store.finish_effect(claim, call_id, {"old-owner-result": "rejected"})
        with self.assertRaises(StaleClaim):
            self.store.submit_output(claim, "stale-submit", {"verified": True})

    def test_cancel_preserves_submitted_content_without_counting_success(self):
        run_id, ids = self.managed()
        claim = self.store.claim_next("tenant", run_id, "fixture-worker")
        self.store.submit_output(claim, "submit", {"fixture": "retained evidence"})
        self.ops.terminate("tenant", run_id, "cancel", ACTOR, "Retain submitted evidence.")
        row = self.row("tasks", "tenant", ids["work"])
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(json.loads(row["output"]), {"fixture": "retained evidence"})
        self.assertEqual(self.events(run_id, "run.completed"), [])

    def test_queue_is_tenant_owned_and_omits_untrusted_private_bodies(self):
        claim, call_id, _ = self.attempt()
        other, _, _ = self.attempt(tenant="other")
        with self.store.transaction() as db:
            db.execute("UPDATE effects SET result=? WHERE tenant='tenant' AND id=?", (canonical({"secret-marker": "private-provider-body"}), call_id))
        queue = self.ops.list_interventions("tenant")
        self.assertEqual([entry["run_id"] for entry in queue], [claim.run_id])
        self.assertNotIn(other.run_id, canonical(queue))
        self.assertNotIn("private-provider-body", canonical(queue))
        self.assertFalse(queue[0]["counts_as_success"])
        self.assertEqual(queue[0]["unknown_effects"][0]["call_id"], call_id)

    def test_healthy_inflight_intent_is_not_an_intervention_until_paused_or_expired(self):
        claim, _, _ = self.attempt(state="intent")
        self.assertEqual(self.ops.list_interventions("tenant"), [])
        self.ops.pause("tenant", claim.run_id, "pause", ACTOR, "Inspect active request.")
        queue = self.ops.list_interventions("tenant")
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["unknown_effects"][0]["state"], "intent")

    def test_completed_reconciliation_settles_once_and_never_approves_task(self):
        claim, call_id, reservation_id = self.attempt()
        result = self.reconcile(claim, call_id)
        self.assertEqual(result["state"], "reconciled")
        self.assertEqual(self.row("effects", "tenant", call_id)["state"], "reconciled")
        self.assertEqual(self.row("reservations", "tenant", reservation_id)["state"], "settled")
        snap = self.store.snapshot("tenant", claim.run_id)
        self.assertEqual((snap["spent"], snap["reserved"], snap["tokens_spent"], snap["tokens_reserved"]), (10, 0, 20, 0))
        self.assertEqual(self.row("tasks", "tenant", claim.task_id)["status"], "awaiting_human")
        self.assertFalse(result["verified"])
        self.assertEqual(self.reconcile(claim, call_id), result)
        self.assertEqual(len(self.events(claim.run_id, "budget.settled")), 1)
        self.assertEqual(len(self.events(claim.run_id, "effect.reconciled")), 1)
        with self.assertRaises(Conflict):
            self.reconcile(claim, call_id, key="different-command")

    def test_not_sent_reconciliation_releases_budget_but_old_effect_stays_recorded(self):
        claim, call_id, reservation_id = self.attempt()
        result = self.reconcile(claim, call_id, decision="not_sent", cost=0, tokens=0)
        self.assertEqual(result["state"], "not_sent")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["reserved"], 0)
        self.assertEqual(self.row("reservations", "tenant", reservation_id)["actual_cost"], 0)
        resumed = self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Authorize a new bounded attempt.")
        self.assertEqual(resumed["task_updates"], [{"task_id": claim.task_id, "status": "pending", "epoch": claim.epoch}])
        second = self.store.claim_next("tenant", claim.run_id, "new-worker")
        self.assertEqual(second.epoch, claim.epoch + 1)
        with self.assertRaises(StaleClaim):
            self.store.begin_effect(claim, f"model-generation:{claim.epoch}", {"fixture": "not a real request"})
        self.assertEqual(self.row("effects", "tenant", call_id)["state"], "not_sent")

    def test_live_owner_prevents_even_not_sent_manual_resolution(self):
        claim, call_id, _ = self.attempt(state="intent")
        self.ops.pause("tenant", claim.run_id, "pause", ACTOR, "Call may still be in flight.")
        with self.assertRaises(Conflict):
            self.reconcile(claim, call_id, decision="not_sent", cost=0, tokens=0)
        self.assertEqual(self.row("effects", "tenant", call_id)["state"], "intent")

    def test_expired_owner_intent_can_be_manually_reconciled(self):
        claim, call_id, _ = self.attempt(state="intent")
        self.ops.pause("tenant", claim.run_id, "pause", ACTOR, "Wait for owner expiry.")
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET lease_until=-1 WHERE tenant='tenant' AND id=?", (claim.task_id,))
        result = self.reconcile(claim, call_id)
        self.assertEqual(result["state"], "reconciled")
        self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Retry after accounting.")
        self.assertEqual(self.row("tasks", "tenant", claim.task_id)["status"], "pending")

    def test_reconciliation_of_scheduler_expiry_requires_explicit_resume(self):
        claim, call_id, _ = self.attempt(state="intent")
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET lease_until=-1 WHERE tenant='tenant' AND id=?", (claim.task_id,))
        self.assertIsNone(self.store.claim_next("tenant", claim.run_id, "expiry-discovery"))
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["status"], "open")
        self.assertEqual(self.row("tasks", "tenant", claim.task_id)["status"], "indeterminate")
        result = self.reconcile(claim, call_id)
        self.assertEqual(result["run_status"], "awaiting_human")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["status"], "awaiting_human")
        self.assertEqual(self.ops.list_interventions("tenant")[0]["run_id"], claim.run_id)
        with self.assertRaises(Conflict):
            self.store.claim_next("tenant", claim.run_id, "no-implicit-resume")
        self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Explicitly authorize remaining attempt.")
        self.assertIsNotNone(self.store.claim_next("tenant", claim.run_id, "resumed-worker"))

    def test_unrelated_effect_or_missing_reservation_cannot_use_model_contract(self):
        claim, call_id, _ = self.attempt(operation="business-write:fixed")
        with self.assertRaises(Conflict):
            self.reconcile(claim, call_id)
        claim2, call2, _ = self.attempt(reserve=False)
        with self.assertRaises(Conflict):
            self.reconcile(claim2, call2)
        self.assertEqual(self.row("effects", "tenant", call_id)["state"], "indeterminate")

    def test_wrong_reservation_task_epoch_or_run_never_releases_budget(self):
        claim, call_id, reservation_id = self.attempt()
        with self.store.transaction() as db:
            db.execute("UPDATE reservations SET epoch=epoch+1 WHERE tenant='tenant' AND id=?", (reservation_id,))
        with self.assertRaises(Conflict):
            self.reconcile(claim, call_id)
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["reserved"], 100)

    def test_previously_settled_usage_cannot_be_rewritten_or_double_charged(self):
        claim, call_id, reservation_id = self.attempt()
        self.store.settle("tenant", reservation_id, actual_cost=10, actual_tokens=20)
        with self.assertRaises(Conflict):
            self.reconcile(claim, call_id, cost=9)
        self.reconcile(claim, call_id, cost=10, tokens=20)
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["spent"], 10)
        self.assertEqual(len(self.events(claim.run_id, "budget.settled")), 1)

    def test_partial_provider_result_is_preserved_and_never_replaced_by_human_content(self):
        claim, call_id, _ = self.attempt(state="partial")
        partial = canonical({"partial-provider-data": "unverified original evidence"})
        with self.store.transaction() as db:
            db.execute("UPDATE effects SET result=? WHERE tenant='tenant' AND id=?", (partial, call_id))
        self.reconcile(claim, call_id)
        effect = self.row("effects", "tenant", call_id)
        self.assertEqual(effect["result"], partial)
        self.assertEqual(effect["state"], "reconciled")
        self.assertEqual(self.events(claim.run_id, "artifact.verified"), [])

    def test_run_overrun_prevents_resume_and_keeps_true_accounting(self):
        claim, call_id, _ = self.attempt(node_cost=100, node_tokens=100, goal_cost=100, goal_tokens=100)
        result = self.reconcile(claim, call_id, cost=101, tokens=102)
        self.assertEqual(result["budget_state"], "overrun")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["spent"], 101)
        with self.assertRaises(Conflict):
            self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "No new budget authorization.")

    def test_task_overrun_prevents_resume_even_when_run_has_headroom(self):
        claim, call_id, _ = self.attempt(node_cost=100, node_tokens=100)
        self.reconcile(claim, call_id, cost=101, tokens=102)
        with self.assertRaises(Conflict):
            self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Goal headroom does not change task cap.")

    def test_already_submitted_task_does_not_hide_task_budget_overrun(self):
        claim, call_id, _ = self.attempt(node_cost=100, node_tokens=100)
        self.reconcile(claim, call_id, cost=101, tokens=102)
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='submitted' WHERE tenant='tenant' AND id=?", (claim.task_id,))
        with self.assertRaises(Conflict):
            self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Submission does not waive budget caps.")

    def test_cancelled_overrun_never_revives_run(self):
        claim, call_id, _ = self.attempt(state="intent")
        self.ops.terminate("tenant", claim.run_id, "cancel", ACTOR, "Cancel before manual billing.")
        result = self.reconcile(claim, call_id, cost=101, tokens=102)
        self.assertEqual(result["run_status"], "cancelled")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["status"], "cancelled")
        self.assertEqual(self.events(claim.run_id, "budget.overrun")[0]["payload"]["run_status"], "cancelled")

    def test_exhausted_attempts_prevent_resume_after_reconciliation(self):
        claim, call_id, _ = self.attempt(max_attempts=1)
        self.reconcile(claim, call_id)
        with self.assertRaises(Conflict):
            self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "A new goal is required.")
        self.assertEqual(self.row("tasks", "tenant", claim.task_id)["status"], "awaiting_human")

    def test_actor_is_part_of_idempotency_identity(self):
        run_id, _ = self.managed()
        original = self.ops.pause("tenant", run_id, "pause", ACTOR, "Audited reason.")
        self.assertEqual(self.ops.pause("tenant", run_id, "pause", ACTOR, "Audited reason."), original)
        with self.assertRaises(Conflict):
            self.ops.pause("tenant", run_id, "pause", "operator:test-bob", "Audited reason.")
        self.assertEqual(self.events(run_id, "run.paused")[0]["actor"], ACTOR)

    def test_cross_tenant_controls_and_reconciliation_are_not_found(self):
        claim, call_id, _ = self.attempt()
        for action in (self.ops.pause, self.ops.resume, self.ops.terminate):
            with self.subTest(action=action.__name__), self.assertRaises(NotFound):
                action("other", claim.run_id, action.__name__, ACTOR, "No access.")
        with self.assertRaises(NotFound):
            self.ops.reconcile_effect("other", claim.run_id, call_id, "reconcile", ACTOR, "completed", EVIDENCE, 10, 20)

    def test_concurrent_idempotent_reconciliation_charges_and_records_once(self):
        claim, call_id, _ = self.attempt()
        barrier = Barrier(2)
        def call():
            barrier.wait(timeout=5)
            return self.reconcile(claim, call_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: call(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["spent"], 10)
        self.assertEqual(len(self.events(claim.run_id, "effect.reconciled")), 1)

    def test_reconciliation_rolls_back_budget_if_audit_append_fails(self):
        claim, call_id, reservation_id = self.attempt()
        original = self.store._event
        def broken(db, tenant, run_id, kind, payload, actor, command_key=None):
            if kind == "effect.reconciled":
                raise RuntimeError("synthetic audit failure")
            return original(db, tenant, run_id, kind, payload, actor, command_key)
        with patch.object(self.store, "_event", side_effect=broken), self.assertRaises(RuntimeError):
            self.reconcile(claim, call_id)
        self.assertEqual(self.row("effects", "tenant", call_id)["state"], "indeterminate")
        self.assertEqual(self.row("reservations", "tenant", reservation_id)["state"], "reserved")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["spent"], 0)

    def test_invalid_evidence_and_usage_have_static_errors(self):
        claim, call_id, _ = self.attempt()
        cases = [
            {"evidence": {"reference": "x", "summary": "y", "secret-marker": "should not echo"}},
            {"evidence": {"reference": "", "summary": "x"}},
            {"evidence": {"reference": "x" * 501, "summary": "x"}},
            {"evidence": {"reference": "x", "summary": "x" * 2001}},
            {"actual_cost": True}, {"actual_tokens": False}, {"actual_cost": -1},
            {"actual_tokens": 10 ** 100}, {"decision": {}},
            {"decision": "not_sent", "actual_cost": 1, "actual_tokens": 0},
        ]
        for changes in cases:
            args = {"tenant": "tenant", "run_id": claim.run_id, "call_id": call_id, "key": "invalid",
                    "actor": ACTOR, "decision": "completed", "evidence": EVIDENCE, "actual_cost": 10, "actual_tokens": 20}
            args.update(changes)
            with self.subTest(changes=list(changes)), self.assertRaises(InvalidInput) as caught:
                self.ops.reconcile_effect(**args)
            self.assertNotIn("should not echo", str(caught.exception))

    def test_events_receipts_and_outbox_commit_atomically(self):
        run_id, _ = self.managed()
        self.ops.pause("tenant", run_id, "pause", ACTOR, "Audited pause.")
        events = self.events(run_id)
        self.assertTrue(self.store.verify_events(events))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE tenant='tenant' AND run_id=?", (run_id,)).fetchone()[0],
                             db.execute("SELECT count(*) FROM outbox o JOIN events e ON o.event_id=e.id WHERE e.tenant='tenant' AND e.run_id=?", (run_id,)).fetchone()[0])
            self.assertIsNotNone(db.execute("SELECT 1 FROM receipts WHERE tenant='tenant' AND key='pause'").fetchone())

    def test_replay_matches_pause_cancel_and_late_overrun_without_reviving_work(self):
        claim, call_id, _ = self.attempt(state="intent")
        self.ops.pause("tenant", claim.run_id, "pause", ACTOR, "Pause external work.")
        self.assertEqual(self.store.replay("tenant", claim.run_id)["status"], "paused")
        self.ops.terminate("tenant", claim.run_id, "cancel", ACTOR, "Cancel while billing is unknown.")
        self.reconcile(claim, call_id, cost=101, tokens=102)
        replay = self.store.replay("tenant", claim.run_id)
        snap = self.store.snapshot("tenant", claim.run_id)
        self.assertEqual(replay["status"], "cancelled")
        for key in ("spent", "reserved", "tokens_spent", "tokens_reserved"):
            self.assertEqual(replay[key], snap[key])
        task = self.row("tasks", "tenant", claim.task_id)
        self.assertEqual(replay["tasks"][claim.task_id]["status"], task["status"])
        self.assertEqual(replay["tasks"][claim.task_id]["epoch"], task["epoch"])
        self.assertEqual(replay["effects"][call_id], "reconciled")

    def test_replay_matches_explicit_resume_and_new_claim_epoch(self):
        claim, call_id, _ = self.attempt()
        self.reconcile(claim, call_id, decision="not_sent", cost=0, tokens=0)
        self.ops.resume("tenant", claim.run_id, "resume", ACTOR, "Authorize next bounded attempt.")
        replay = self.store.replay("tenant", claim.run_id)
        self.assertEqual(replay["status"], "open")
        self.assertEqual(replay["tasks"][claim.task_id]["status"], "pending")
        next_claim = self.store.claim_next("tenant", claim.run_id, "new-worker")
        replay = self.store.replay("tenant", claim.run_id)
        self.assertEqual(replay["tasks"][claim.task_id]["epoch"], next_claim.epoch)
        self.assertEqual(replay["tasks"][claim.task_id]["status"], "running")
        self.assertEqual(replay["effects"][call_id], "not_sent")


if __name__ == "__main__":
    unittest.main()
