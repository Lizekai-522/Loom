import copy
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from loom.store import BudgetExceeded, Claim, Conflict, InvalidInput, NotFound, StaleClaim, Store
from tests.helpers import trusted_verification


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "loom.db"
        self.store = Store(self.path)
        self.run = self.store.create_run("tenant-a", "run", acceptance=["tests.pass"], cost_limit=100, token_limit=100)
        self.run_id = self.run["run_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, name="one", **kwargs):
        return self.store.add_task("tenant-a", self.run_id, "add:" + name, task_id=name, title=name, **kwargs)

    def claim(self, worker="worker", **kwargs):
        return self.store.claim_next("tenant-a", self.run_id, worker, **kwargs)

    def complete(self, claim, key, output):
        return self.store.complete(claim, key, output, verification=trusted_verification(claim, output))

    def test_concurrent_idempotent_creation_returns_same_receipt(self):
        def command(_):
            return self.store.create_run("tenant-a", "same-key", acceptance=["tests.pass"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(command, range(16)))
        self.assertEqual(len({r["run_id"] for r in replies}), 1)
        self.assertEqual(len(self.store.events("tenant-a", replies[0]["run_id"])), 3)

    def test_key_binds_operation_and_parameters(self):
        self.task()
        with self.assertRaises(Conflict):
            self.store.add_task("tenant-a", self.run_id, "add:one", task_id="other", title="other")
        with self.assertRaises(Conflict):
            self.store.create_run("tenant-a", "add:one", acceptance=["tests.pass"])

    def test_outbox_survives_process_boundary_before_publish(self):
        self.task()
        restarted = Store(self.path)
        delivered = []
        self.assertEqual(restarted.drain_outbox(delivered.append), 4)
        self.assertEqual(restarted.drain_outbox(delivered.append), 0)
        self.assertEqual(len({e["event_id"] for e in delivered}), 4)

    def test_publish_failure_keeps_outbox_pending_and_inbox_deduplicates(self):
        sent = []
        def failed_broker(event):
            sent.append(event)
            raise ConnectionError("ack lost")
        with self.assertRaises(ConnectionError):
            self.store.drain_outbox(failed_broker)
        self.store.drain_outbox(sent.append)
        self.assertEqual(sent[0]["event_id"], sent[1]["event_id"])
        effects = []
        self.assertTrue(self.store.consume("projection", sent[0]["event_id"], lambda db: effects.append(1)))
        self.assertFalse(self.store.consume("projection", sent[0]["event_id"], lambda db: effects.append(1)))
        self.assertEqual(effects, [1])

    def test_concurrent_claim_has_one_winner(self):
        self.task()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.claim("worker-" + str(i)), range(8)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_business_cannot_consume_verify_reserve(self):
        for i in range(4):
            self.task(str(i))
        self.task("verify", channel="verification")
        self.assertTrue(all(self.claim("worker-" + str(i)) for i in range(3)))
        self.assertIsNone(self.claim("business-full"))
        self.assertIsNotNone(self.claim("validator", channel="verification"))
        self.assertIsNone(self.claim("verify-full", channel="verification"))

    def test_all_adapter_instances_use_shared_capacity(self):
        self.task()
        with self.assertRaises(ValueError):
            Store(self.path, max_workers=10, verify_reserve=1)

    def test_priority_applies_before_agent_choice_and_dependencies_block(self):
        self.task("low", priority=0)
        self.task("high", priority=9)
        self.task("blocked", priority=100, depends_on=["low"])
        self.assertEqual(self.claim().task_id, "high")
        self.assertEqual(self.claim("second").task_id, "low")
        self.assertIsNone(self.claim("third"))

    def test_dependencies_cannot_cross_run_or_tenant(self):
        self.task()
        other = self.store.create_run("tenant-a", "second-run", acceptance=["tests.pass"])
        with self.assertRaises(InvalidInput):
            self.store.add_task("tenant-a", other["run_id"], "bad", task_id="child", title="child", depends_on=["one"])
        with self.assertRaises(NotFound):
            self.store.snapshot("tenant-b", self.run_id)
        with self.assertRaises(NotFound):
            self.store.events("tenant-b", self.run_id)

    def test_expired_claim_cannot_submit_after_reassignment(self):
        self.task()
        old = self.claim(lease_seconds=0.03)
        time.sleep(0.06)
        current = self.claim("new")
        self.assertGreater(current.epoch, old.epoch)
        with self.assertRaises(StaleClaim):
            self.complete(old, "late", {"value": "old"})
        self.complete(current, "new", {"value": "new"})

    def test_concurrent_reservations_cannot_overspend_shared_run_budget(self):
        self.task()
        c = self.claim()
        def reserve(i):
            try:
                self.store.reserve(c, str(i), max_cost=60, max_tokens=10)
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(reserve, range(2))), 1)
        state = self.store.snapshot("tenant-a", self.run_id)
        self.assertEqual(state["reserved"], 60)

    def test_settlement_is_idempotent_and_records_actual_overrun(self):
        self.task()
        c = self.claim()
        self.store.reserve(c, "model-call", max_cost=20, max_tokens=20)
        self.assertEqual(self.store.settle("tenant-a", "model-call", actual_cost=25, actual_tokens=10)["state"], "overrun")
        self.store.settle("tenant-a", "model-call", actual_cost=25, actual_tokens=10)
        state = self.store.snapshot("tenant-a", self.run_id)
        self.assertEqual((state["spent"], state["reserved"], state["status"]), (25, 0, "awaiting_human"))
        with self.assertRaises(BudgetExceeded):
            self.store.reserve(c, "another", max_cost=1, max_tokens=1)

    def test_unknown_effect_is_never_granted_twice(self):
        self.task()
        c = self.claim()
        first = self.store.begin_effect(c, "fixed-operation", {"amount": 1})
        self.assertTrue(first["dispatch"])
        self.assertFalse(self.store.begin_effect(c, "fixed-operation", {"amount": 1})["dispatch"])
        with self.assertRaises(Conflict):
            self.complete(c, "premature", {"ok": True})
        # Expire only after the duplicate-admission assertions. A 50 ms wall
        # clock lease can expire during SQLite I/O on a busy Windows machine.
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE tenant=? AND id=?", (c.tenant_id, c.task_id))
        self.assertIsNone(self.claim("replacement"))
        snapshot = self.store.snapshot("tenant-a", self.run_id)
        self.assertEqual(snapshot["tasks"][0]["status"], "indeterminate")
        self.assertEqual(self.store.replay("tenant-a", self.run_id)["effects"][first["call_id"]], "indeterminate")
        with self.assertRaises(StaleClaim):
            self.store.begin_effect(c, "another-operation", {})

    def test_completed_effect_is_retrieved_without_dispatch(self):
        self.task()
        c = self.claim()
        effect = self.store.begin_effect(c, "fixed-operation", {"x": 1})
        self.store.finish_effect(c, effect["call_id"], {"external_id": "one"})
        repeated = self.store.begin_effect(c, "fixed-operation", {"x": 1})
        self.assertEqual((repeated["dispatch"], repeated["state"]), (False, "done"))
        self.complete(c, "complete", {"ok": True})

    def test_invalidation_marks_descendants_stale_and_revokes_claim(self):
        self.task("parent")
        p = self.claim()
        effect = self.store.begin_effect(p, "fixed-operation", {})
        self.store.finish_effect(p, effect["call_id"], {"id": 1})
        self.complete(p, "parent-done", {"ok": True})
        self.task("child", depends_on=["parent"])
        child = self.claim()
        self.assertEqual(self.store.invalidate("tenant-a", self.run_id, "parent", "evidence revoked"), ["child", "parent"])
        with self.assertRaises(StaleClaim):
            self.complete(child, "late-child", {"ok": True})
        self.assertTrue(any(e["type"] == "tool.compensation_required" for e in self.store.events("tenant-a", self.run_id)))
        self.assertEqual(self.store.replay("tenant-a", self.run_id)["effects"][effect["call_id"]], "done")

    def test_full_envelope_hash_and_checkpoint_detect_tamper_and_truncation(self):
        events = self.store.events("tenant-a", self.run_id)
        for field, replacement in {"type": "gate.passed", "actor": "human:other", "tenant_id": "other", "run_id": "other", "seq": 99}.items():
            altered = copy.deepcopy(events)
            altered[0][field] = replacement
            self.assertFalse(self.store.verify_events(altered), field)
        self.assertTrue(self.store.verify_events(events, events[-1]["hash"], len(events)))
        self.assertFalse(self.store.verify_events(events[:-1], events[-1]["hash"], len(events)))
        self.assertIsNone(events[1]["submission_key"])

    def test_append_only_triggers_and_replay_survive_reopen(self):
        self.task()
        c = self.claim()
        self.complete(c, "done", {"ok": True})
        with self.store.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE events SET hash='bad'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM events")
        rebuilt = Store(self.path).replay("tenant-a", self.run_id)
        self.assertEqual(rebuilt["tasks"]["one"]["status"], "done")
        self.assertEqual(rebuilt["last_seq"], len(self.store.events("tenant-a", self.run_id)))

    def test_non_success_dual_sign_states_cannot_open_dependency(self):
        self.task("parent")
        self.task("child", depends_on=["parent"])
        for state in ("pending", "disputed", "blocked", "single"):
            claim = self.claim()
            output = {"value": state}
            proof = replace(trusted_verification(claim, output), dual_sign=state)
            response = self.store.complete(claim, "reject:" + state, output, verification=proof)
            self.assertEqual(response["status"], "pending")
            self.assertEqual(self.store.snapshot("tenant-a", self.run_id)["tasks"][1]["status"], "pending")
        claim = self.claim()
        self.complete(claim, "correct-proof", {"value": "valid"})
        self.assertEqual(self.claim("next").task_id, "child")
        self.assertEqual(sum(e["type"] == "gate.failed" for e in self.store.events("tenant-a", self.run_id)), 4)

    def test_goal_acceptance_cannot_be_inferred_from_idle_or_execution(self):
        self.task()
        with self.assertRaises(Conflict):
            self.store.finish_run("tenant-a", self.run_id, "bad-finish", {"tests.pass": True})
        claim = self.claim()
        self.complete(claim, "valid-task", {"ok": True})
        with self.assertRaises(Conflict):
            self.store.finish_run("tenant-a", self.run_id, "bad-finish", {"tests.pass": False})
        final = self.store.finish_run("tenant-a", self.run_id, "good-finish", {"tests.pass": True})
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(self.store.replay("tenant-a", self.run_id)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
