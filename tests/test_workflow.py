import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from loom.store import BudgetExceeded, Conflict, InvalidInput, NotFound, Store
from loom.workflow import WorkflowDefinition, WorkflowService


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "workflow.db")
        self.service = WorkflowService(self.store)
        self.definition = json.loads(PACK.read_text(encoding="utf-8"))
        self.created = self.create()
        self.run = self.created["run_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, key="goal", **extra):
        return self.service.create_goal("a", key, title="Fix an arithmetic defect", description="Implement and verify the bounded change.",
                                        acceptance=["tests.pass"], **extra)

    def save(self, key="save", revision=0, definition=None):
        return self.service.save_plan("a", self.run, key, self.definition if definition is None else definition, revision)

    def approve(self, saved, key="approve"):
        return self.service.approve_plan("a", self.run, key, saved["revision"], saved["digest"])

    def counts(self):
        with self.store.connect() as db:
            return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                         for table in ("runs", "workflow_goals", "tasks", "workflow_tasks", "events", "outbox", "receipts"))

    def test_goal_creation_is_one_atomic_idempotent_draft_command(self):
        self.assertEqual(self.created["status"], "draft")
        self.assertEqual(self.create(), self.created)
        self.assertEqual(self.counts(), (1, 1, 0, 0, 3, 3, 1))
        self.assertEqual(self.store.snapshot("a", self.run)["status"], "draft")
        self.assertTrue(self.store.verify_events(self.store.events("a", self.run)))

    def test_creation_failure_rolls_back_root_events_projection_and_receipt(self):
        before = self.counts()
        original = self.store._event
        def fail(db, tenant, run_id, kind, *args):
            if kind == "intent.created":
                raise RuntimeError("injected final root-event failure")
            return original(db, tenant, run_id, kind, *args)
        self.store._event = fail
        with self.assertRaises(RuntimeError):
            self.create("atomic-failure")
        self.assertEqual(self.counts(), before)

    def test_revisions_and_reviewed_digest_bind_approval(self):
        first = self.save()
        changed = copy.deepcopy(self.definition)
        changed["nodes"][0]["instruction"] += " Include the new edge case."
        second = self.save("save-again", 1, changed)
        before = self.counts()
        with self.assertRaises(Conflict):
            self.approve(first)
        with self.assertRaises(Conflict):
            self.service.approve_plan("a", self.run, "wrong-digest", second["revision"], "0" * 64)
        with self.assertRaises(Conflict):
            self.save("stale-save", 0)
        self.assertEqual(self.counts(), before)
        self.assertNotEqual(first["digest"], second["digest"])
        self.assertEqual(self.approve(second)["status"], "open")

    def test_approval_creates_exact_namespaced_tasks_and_frozen_contracts(self):
        saved = self.save()
        approved = self.approve(saved)
        self.assertEqual(len(approved["tasks"]), 7)
        self.assertEqual(self.service.get_plan("a", self.run)["status"], "approved")
        task = self.service.get_task_spec("a", self.run, approved["task_ids"]["code"])
        self.assertEqual(task["depends_on"], [approved["task_ids"]["design"], approved["task_ids"]["case"]])
        self.assertEqual(task["instruction"], self.definition["nodes"][3]["instruction"])
        self.assertEqual(task["plan_digest"], saved["digest"])
        self.assertEqual(self.approve(saved), approved)
        with self.assertRaises(Conflict):
            self.save("change-frozen", 1)
        second = self.create("second-goal")
        plan = self.service.save_plan("a", second["run_id"], "second-save", self.definition, 0)
        another = self.service.approve_plan("a", second["run_id"], "second-approve", plan["revision"], plan["digest"])
        self.assertFalse(set(approved["task_ids"].values()) & set(another["task_ids"].values()))

    def test_concurrent_identical_approval_reuses_one_receipt(self):
        saved = self.save()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.approve(saved), range(12)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.counts(), (1, 1, 7, 7, 12, 12, 3))

    def test_competing_approvals_have_one_winner_without_duplicate_tasks(self):
        saved = self.save()
        def attempt(number):
            try:
                self.approve(saved, "approval-" + str(number))
                return True
            except Conflict:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            winners = list(pool.map(attempt, range(12)))
        self.assertEqual(sum(winners), 1)
        self.assertEqual(self.counts()[2:4], (7, 7))

    def test_mid_approval_failure_leaves_no_tasks_events_or_receipt(self):
        saved = self.save()
        before = self.counts()
        original = self.store._event
        def fail(db, tenant, run_id, kind, payload, *args):
            if kind == "task.created" and payload["node_id"] == "review":
                raise RuntimeError("injected middle-task failure")
            return original(db, tenant, run_id, kind, payload, *args)
        self.store._event = fail
        with self.assertRaises(RuntimeError):
            self.approve(saved)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.service.get_goal("a", self.run)["status"], "draft")
        self.store._event = original
        self.assertEqual(len(self.approve(saved)["tasks"]), 7)

    def test_task_cap_totals_cannot_exceed_frozen_goal_budgets(self):
        for name, kwargs in (("cost", {"cost_limit": 699999}), ("tokens", {"token_limit": 6999})):
            created = self.create(name, **kwargs)
            saved = self.service.save_plan("a", created["run_id"], name + "-save", self.definition, 0)
            before = self.counts()
            with self.assertRaises(BudgetExceeded):
                self.service.approve_plan("a", created["run_id"], name + "-approve", saved["revision"], saved["digest"])
            self.assertEqual(self.counts(), before)

    def test_unknown_criteria_and_invalid_budgets_are_rejected_without_writes(self):
        before = self.counts()
        for acceptance in (["model.says.pass"], ["tests.pass", "tests.pass"], [], [True]):
            with self.assertRaises(InvalidInput):
                self.service.create_goal("a", "bad", title="x", description="x", acceptance=acceptance)
        for kwargs in ({"cost_limit": True}, {"token_limit": -1}, {"token_limit": "100"}, {"cost_limit": 2 ** 100}):
            with self.assertRaises(InvalidInput):
                self.create("bad-budget", **kwargs)
        self.assertEqual(self.counts(), before)

    def test_managed_goal_cannot_bypass_plan_approval_through_legacy_store(self):
        with self.assertRaises(Conflict):
            self.store.add_task("a", self.run, "old-task-api", task_id="injected", title="Injected")
        with self.assertRaises(Conflict):
            self.store.claim_next("a", self.run, "before-approval")
        saved = self.save()
        approved = self.approve(saved)
        with self.assertRaises(Conflict):
            self.store.add_task("a", self.run, "old-task-api-after", task_id="injected", title="Injected")
        claim = self.store.claim_next("a", self.run, "approved-worker")
        self.assertEqual(claim.task_id, approved["task_ids"]["spec"])
        self.assertEqual(self.store.replay("a", self.run)["status"], "open")

    def test_draft_replay_and_typed_revisions_preserve_approval_boundary(self):
        self.assertEqual(self.store.replay("a", self.run)["status"], "draft")
        for revision in (True, -1, "0"):
            with self.assertRaises(InvalidInput):
                self.save(revision=revision)
        self.assertEqual(self.counts(), (1, 1, 0, 0, 3, 3, 1))

    def test_idempotency_key_cannot_be_reused_across_commands_or_arguments(self):
        with self.assertRaises(Conflict):
            self.save("goal")
        saved = self.save()
        with self.assertRaises(Conflict):
            self.approve(saved, "save")
        changed = copy.deepcopy(self.definition)
        changed["nodes"][0]["title"] = "A different title"
        with self.assertRaises(Conflict):
            self.save("save", definition=changed)

    def test_tenant_and_run_bind_all_read_and_write_paths(self):
        saved = self.save()
        for operation in (lambda: self.service.get_goal("b", self.run), lambda: self.service.get_plan("b", self.run),
                          lambda: self.service.save_plan("b", self.run, "save", self.definition, 1),
                          lambda: self.service.approve_plan("b", self.run, "approve", 1, saved["digest"])):
            with self.assertRaises(NotFound):
                operation()
        self.assertEqual(self.service.list_goals("b"), [])
        self.assertEqual([goal["run_id"] for goal in self.service.list_goals("a")], [self.run])
        approved = self.approve(saved)
        with self.assertRaises(NotFound):
            self.service.get_task_spec("b", self.run, approved["task_ids"]["spec"])

    def test_cycles_missing_edges_cross_scope_and_tool_escalation_rejected(self):
        variants = []
        def variant(change):
            value = copy.deepcopy(self.definition)
            change(value)
            variants.append(value)
        variant(lambda p: p["nodes"][0].update(depends=["spec"]))
        variant(lambda p: p["nodes"][0].update(depends=["verify"]))
        variant(lambda p: p["nodes"][0].update(depends=["other-run:task"]))
        variant(lambda p: p["nodes"][0].update(depends=["missing"]))
        variant(lambda p: p.update(scope="C:/Users/host"))
        variant(lambda p: p["roles"][0]["tools"].append("workspace.write"))
        variant(lambda p: p["roles"][0]["tools"].append("shell.exec"))
        variant(lambda p: p["roles"][0]["skills"].append("admin.override"))
        variant(lambda p: p["nodes"][0].update(role="admin"))
        variant(lambda p: p["nodes"][0].update(channel="verification"))
        variant(lambda p: p["nodes"][0].update(priority=True))
        variant(lambda p: p["nodes"][0].update(working_directory="C:/"))
        variant(lambda p: p["roles"][1].update(id="analyst"))
        variant(lambda p: p.update(schema_version="loom.workflow/v2"))
        before = self.counts()
        for value in variants:
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                self.save(definition=value)
        self.assertEqual(self.counts(), before)

    def test_digest_binds_instructions_routes_permissions_dependencies_and_limits(self):
        original = WorkflowDefinition.model_validate(self.definition).execution_digest
        changes = [lambda p: p["nodes"][0].update(instruction="Changed instruction"),
                   lambda p: p["roles"][0].update(model_alias="another-model"),
                   lambda p: p["roles"][0]["tools"].remove("artifacts.write"),
                   lambda p: p["nodes"][2].update(depends=["design"]),
                   lambda p: p["nodes"][0]["limits"].update(max_attempts=2),
                   lambda p: p["nodes"][0].update(evaluation_digest="a" * 64),
                   lambda p: p["nodes"][0].update(priority=1)]
        for change in changes:
            value = copy.deepcopy(self.definition)
            change(value)
            self.assertNotEqual(WorkflowDefinition.model_validate(value).execution_digest, original)

    def test_mutated_model_instance_is_revalidated_at_save_boundary(self):
        definition = WorkflowDefinition.model_validate(self.definition)
        definition.roles[0].tools.append("workspace.write")
        with self.assertRaises(InvalidInput):
            self.save(definition=definition)

    def test_tampered_persisted_plan_does_not_approve(self):
        saved = self.save()
        tampered = copy.deepcopy(self.definition)
        tampered["nodes"][0]["instruction"] = "Changed after review"
        with self.store.transaction() as db:
            db.execute("UPDATE workflow_goals SET definition=? WHERE tenant=? AND run_id=?",
                       (json.dumps(tampered), "a", self.run))
        before = self.counts()
        with self.assertRaises(Conflict):
            self.approve(saved)
        self.assertEqual(self.counts(), before)

    def test_evaluation_binding_is_reviewed_and_persisted_without_test_secrets(self):
        self.definition["nodes"][3]["evaluation_digest"] = "a" * 64
        saved = self.save()
        approved = self.approve(saved)
        task_id = approved["task_ids"]["code"]
        contract = self.service.get_task_spec("a", self.run, task_id)
        self.assertEqual(contract["evaluation_digest"], "a" * 64)
        self.assertIsNone(self.service.get_task_spec("a", self.run, approved["task_ids"]["spec"])["evaluation_digest"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT evaluation_digest FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()[0], "a" * 64)
        for invalid in ("a" * 63, "g" * 64, "A" * 64, True, 7):
            value = copy.deepcopy(self.definition)
            value["nodes"][3]["evaluation_digest"] = invalid
            with self.assertRaises(InvalidInput):
                self.service.save_plan("a", self.run, "invalid-binding", value, 1)
        with self.store.transaction() as db:
            db.execute("UPDATE workflow_tasks SET evaluation_digest=? WHERE tenant=? AND task_id=?",
                       ("b" * 64, "a", task_id))
        with self.assertRaises(Conflict):
            self.service.get_task_spec("a", self.run, task_id)

    def test_evaluation_digest_column_migrates_existing_local_schema_fail_closed(self):
        path = Path(self.tmp.name) / "before-evaluation.db"
        legacy_store = Store(path)
        with legacy_store.connect() as db:
            db.executescript("""CREATE TABLE workflow_tasks (
                tenant TEXT NOT NULL, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
                task_id TEXT NOT NULL, contract TEXT NOT NULL, plan_digest TEXT NOT NULL,
                PRIMARY KEY(tenant,run_id,node_id));""")
        WorkflowService(legacy_store)
        with legacy_store.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(workflow_tasks)")}
        self.assertIn("evaluation_digest", columns)

    def test_contract_cache_cannot_change_frozen_execution_fields(self):
        approved = self.approve(self.save())
        task_id = approved["task_ids"]["code"]
        original = self.service.get_task_spec("a", self.run, task_id)
        changes = [lambda c: c.update(instruction="Execute a substituted instruction"),
                   lambda c: c.update(model_alias="another-model"),
                   lambda c: c.update(tools=["shell.exec"]),
                   lambda c: c["limits"].update(max_attempts=10),
                   lambda c: c.update(depends_on=[]),
                   lambda c: c.update(scope="C:/host"),
                   lambda c: c.update(role="validator"),
                   lambda c: c.update(priority=50.0),
                   lambda c: c.update(unreviewed_option=True)]
        for change in changes:
            corrupted = copy.deepcopy(original)
            change(corrupted)
            with self.store.transaction() as db:
                db.execute("UPDATE workflow_tasks SET contract=? WHERE tenant=? AND task_id=?",
                           (json.dumps(corrupted), "a", task_id))
            with self.assertRaises(Conflict):
                self.service.get_task_spec("a", self.run, task_id)
        with self.store.transaction() as db:
            db.execute("UPDATE workflow_tasks SET contract=? WHERE tenant=? AND task_id=?",
                       (json.dumps({**original, "status": "done"}), "a", task_id))
        self.assertEqual(self.service.get_task_spec("a", self.run, task_id), original)
        mutated = copy.deepcopy(self.definition)
        mutated["nodes"][3]["instruction"] = "Changed approved definition"
        with self.store.transaction() as db:
            db.execute("UPDATE workflow_goals SET definition=? WHERE tenant=? AND run_id=?",
                       (json.dumps(mutated), "a", self.run))
        with self.assertRaises(Conflict):
            self.service.get_task_spec("a", self.run, task_id)

    def test_local_automation_events_do_not_claim_a_human_principal(self):
        self.approve(self.save())
        operator_events = {"run.created", "goal.created", "workflow.plan_saved", "workflow.plan_approved"}
        for event in self.store.events("a", self.run):
            if event["type"] in operator_events:
                self.assertEqual(event["actor"], "operator:local")


if __name__ == "__main__":
    unittest.main()
