"""HTTP regression tests for human-reviewed, tenant-bound workflow plans.

Uses the real software-dev pack, temporary SQLite, and FastAPI TestClient.
No model, container, private configuration, or trusted-verifier fixture is used.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from loom.api import create_app
from loom.store import Store


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"


class GoalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "goals-api.db")
        self.client = TestClient(create_app(self.store, {
            "tenant-a": "goal-api-public-fixture-token-a",
            "tenant-b": "goal-api-public-fixture-token-b",
        }))
        self.definition = json.loads(PACK.read_text(encoding="utf-8"))
        self.body = {"title": "Fix arithmetic in a local workspace",
                     "description": "Private-to-tenant-a goal description; implement and verify the authorized change.",
                     "acceptance": ["schema.valid", "tests.pass", "report.traceable"],
                     "cost_limit_microusd": 1_000_000, "token_limit": 10000}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def headers(self, key=None, tenant="a"):
        headers = {"Authorization": "Bearer goal-api-public-fixture-token-" + tenant}
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    def create(self, key="goal", *, tenant="a", body=None):
        response = self.client.post("/v1/goals", headers=self.headers(key, tenant),
                                    json=self.body if body is None else body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def save(self, run, key="save", *, revision=0, definition=None):
        response = self.client.put(f"/v1/goals/{run}/plan", headers=self.headers(key),
                                   json={"definition": self.definition if definition is None else definition,
                                         "expected_revision": revision})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def approve(self, run, saved, key="approve"):
        response = self.client.post(f"/v1/goals/{run}/plan/approve", headers=self.headers(key),
                                    json={"expected_revision": saved["revision"], "expected_digest": saved["digest"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def get(self, path):
        response = self.client.get(path, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def events(self, run):
        return self.get(f"/v1/runs/{run}/events")["events"]

    def claim(self, run, worker="worker", channel="business"):
        return self.client.post(f"/v1/runs/{run}/claim", headers=self.headers(),
                                json={"worker_id": worker, "channel": channel})

    def test_create_read_list_and_plan_lifecycle_require_human_approval(self):
        created = self.create()
        run = created["run_id"]
        goal = self.get(f"/v1/goals/{run}")
        self.assertEqual((goal["status"], goal["plan_status"], goal["plan_revision"]), ("draft", "draft", 0))
        self.assertEqual(self.get("/v1/goals")["goals"], [goal])
        self.assertIsNone(self.get(f"/v1/goals/{run}/plan")["definition"])
        saved = self.save(run)
        self.assertEqual(self.get(f"/v1/runs/{run}")["tasks"], [])
        self.assertEqual(self.get(f"/v1/goals/{run}/plan"), saved)
        approved = self.approve(run, saved)
        self.assertEqual(approved["status"], "open")
        self.assertEqual(len(approved["tasks"]), 7)
        goal = self.get(f"/v1/goals/{run}")
        self.assertEqual((goal["status"], goal["plan_status"]), ("open", "approved"))
        plan = self.get(f"/v1/goals/{run}/plan")
        self.assertEqual((plan["digest"], plan["task_ids"]), (saved["digest"], approved["task_ids"]))
        self.assertEqual([event["type"] for event in self.events(run)].count("workflow.plan_approved"), 1)
        self.assertTrue(self.get(f"/v1/runs/{run}/integrity")["valid"])

    def test_goal_idempotency_binds_the_whole_user_request(self):
        created = self.create()
        before = self.events(created["run_id"])
        self.assertEqual(self.create(), created)
        changes = {"title": "Different goal", "description": "Different authorized request",
                   "acceptance": ["tests.pass"], "cost_limit_microusd": 999999, "token_limit": 9999}
        for field, value in changes.items():
            with self.subTest(field=field):
                body = {**self.body, field: value}
                response = self.client.post("/v1/goals", headers=self.headers("goal"), json=body)
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.events(created["run_id"]), before)
        self.assertEqual(len(self.get("/v1/goals")["goals"]), 1)

    def test_plan_idempotency_does_not_advance_revision_twice_or_reuse_changed_arguments(self):
        run = self.create()["run_id"]
        saved = self.save(run)
        before = self.events(run)
        self.assertEqual(self.save(run), saved)
        changed = copy.deepcopy(self.definition)
        changed["nodes"][0]["instruction"] += " Include zero input."
        for definition, revision in ((changed, 0), (self.definition, 1)):
            with self.subTest(revision=revision):
                response = self.client.put(f"/v1/goals/{run}/plan", headers=self.headers("save"),
                                           json={"definition": definition, "expected_revision": revision})
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.events(run), before)
        self.assertEqual(self.get(f"/v1/goals/{run}/plan"), saved)

    def test_approval_idempotency_cannot_duplicate_tasks_or_change_reviewed_identity(self):
        run = self.create()["run_id"]
        saved = self.save(run)
        approved = self.approve(run, saved)
        before = self.events(run)
        self.assertEqual(self.approve(run, saved), approved)
        for key, revision, digest in (("approve", 2, saved["digest"]),
                                      ("approve", 1, "0" * 64),
                                      ("different-approval-key", 1, saved["digest"])):
            with self.subTest(key=key, revision=revision):
                response = self.client.post(f"/v1/goals/{run}/plan/approve", headers=self.headers(key),
                                            json={"expected_revision": revision, "expected_digest": digest})
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.events(run), before)
        self.assertEqual(len(self.get(f"/v1/runs/{run}")["tasks"]), 7)

    def test_stale_revision_or_digest_never_approves_a_different_plan(self):
        run = self.create()["run_id"]
        first = self.save(run)
        definition = copy.deepcopy(self.definition)
        definition["nodes"][0]["instruction"] += " Also describe negative values."
        current = self.save(run, "save-second", revision=1, definition=definition)
        self.assertNotEqual(first["digest"], current["digest"])
        before = self.events(run)
        for index, (revision, digest) in enumerate(((first["revision"], first["digest"]),
                                                   (current["revision"], first["digest"]),
                                                   (first["revision"], current["digest"]))):
            with self.subTest(revision=revision, digest=digest):
                response = self.client.post(f"/v1/goals/{run}/plan/approve", headers=self.headers("stale-" + str(index)),
                                            json={"expected_revision": revision, "expected_digest": digest})
                self.assertEqual(response.status_code, 409, response.text)
        stale_save = self.client.put(f"/v1/goals/{run}/plan", headers=self.headers("stale-save"),
                                     json={"definition": definition, "expected_revision": 0})
        self.assertEqual(stale_save.status_code, 409)
        self.assertEqual(self.events(run), before)
        self.assertEqual(self.get(f"/v1/runs/{run}")["tasks"], [])
        self.assertEqual(self.get(f"/v1/goals/{run}")["status"], "draft")
        self.assertEqual(self.approve(run, current)["digest"], current["digest"])

    def test_draft_goal_has_no_claim_or_legacy_task_creation_escape(self):
        run = self.create()["run_id"]
        for saved in (False, True):
            with self.subTest(plan_saved=saved):
                if saved:
                    self.save(run)
                before = self.events(run)
                self.assertEqual(self.claim(run).status_code, 409)
                response = self.client.post(f"/v1/runs/{run}/tasks", headers=self.headers("inject-" + str(saved)),
                                            json={"task_id": "injected", "title": "Bypass plan approval"})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.get(f"/v1/runs/{run}")["tasks"], [])
                self.assertEqual(self.events(run), before)

    def test_approved_plan_rejects_edits_and_old_add_task_route(self):
        run = self.create()["run_id"]
        saved = self.save(run)
        self.approve(run, saved)
        before = self.events(run)
        definition = copy.deepcopy(self.definition)
        definition["roles"][0]["model_alias"] = "another-approved-alias"
        definition["nodes"][0]["instruction"] = "Changed after the human review."
        response = self.client.put(f"/v1/goals/{run}/plan", headers=self.headers("edit-approved"),
                                   json={"definition": definition, "expected_revision": saved["revision"]})
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.post(f"/v1/runs/{run}/tasks", headers=self.headers("inject-approved"),
                                    json={"task_id": "injected", "title": "Unreviewed work", "channel": "verification"})
        self.assertEqual(response.status_code, 409, response.text)
        plan = self.get(f"/v1/goals/{run}/plan")
        self.assertEqual((plan["definition"], plan["digest"], plan["revision"]),
                         (saved["definition"], saved["digest"], saved["revision"]))
        self.assertEqual(self.events(run), before)

    def test_approval_freezes_all_seven_roles_permissions_and_dependency_contracts(self):
        run = self.create()["run_id"]
        saved = self.save(run)
        approved = self.approve(run, saved)
        roles = {role["id"]: role for role in self.definition["roles"]}
        self.assertEqual(set(roles), {"analyst", "architect", "tester", "developer", "reviewer", "fixer", "validator"})
        contracts = {task["node_id"]: task for task in approved["tasks"]}
        self.assertEqual(set(contracts), {node["id"] for node in self.definition["nodes"]})
        anchor = self.get(f"/v1/runs/{run}")["anchor"]
        for node in self.definition["nodes"]:
            with self.subTest(role=node["role"]):
                contract, role = contracts[node["id"]], roles[node["role"]]
                self.assertEqual(contract["task_id"], run + ":" + node["id"])
                self.assertEqual(contract["plan_digest"], saved["digest"])
                self.assertEqual(contract["anchor_digest"], anchor)
                self.assertEqual((contract["role"], contract["tools"], contract["skills"], contract["model_alias"]),
                                 (node["role"], role["tools"], role["skills"], role["model_alias"]))
                self.assertEqual(contract["depends_on"], [approved["task_ids"][name] for name in node["depends"]])
                self.assertEqual((contract["instruction"], contract["limits"], contract["channel"]),
                                 (node["instruction"], node["limits"], node["channel"]))
        self.assertNotIn("workspace.write", contracts["review"]["tools"])
        self.assertNotIn("workspace.write", contracts["verify"]["tools"])
        self.assertEqual(contracts["verify"]["channel"], "verification")

    def test_submitted_parent_does_not_release_children_or_accept_self_reported_pass(self):
        run = self.create()["run_id"]
        approved = self.approve(run, self.save(run))
        response = self.claim(run)
        self.assertEqual(response.status_code, 200, response.text)
        claim = response.json()["claim"]
        self.assertEqual(claim["task_id"], approved["task_ids"]["spec"])
        response = self.client.post(f"/v1/runs/{run}/tasks/{claim['task_id']}/submit", headers=self.headers("submit-spec"),
                                    json={"worker_id": "worker", "epoch": claim["epoch"],
                                          "output": {"verified": True, "tests_pass": True, "status": "done"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "submitted")
        for channel in ("business", "verification"):
            with self.subTest(channel=channel):
                response = self.claim(run, "another-worker", channel)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(response.json()["claim"])
        replay = self.get(f"/v1/runs/{run}/replay")
        self.assertEqual(replay["tasks"][claim["task_id"]]["status"], "submitted")
        self.assertTrue(all(task["status"] == "pending" for task_id, task in replay["tasks"].items() if task_id != claim["task_id"]))
        self.assertFalse(any(event["type"] in ("artifact.verified", "task.completed") for event in self.events(run)))
        self.assertEqual(self.client.post(f"/v1/runs/{run}/tasks/{claim['task_id']}/complete",
                                          headers=self.headers("fake-complete"), json={}).status_code, 404)

    def test_cross_tenant_goal_plan_run_and_task_paths_do_not_disclose_or_mutate(self):
        run = self.create()["run_id"]
        saved = self.save(run)
        approved = self.approve(run, saved)
        claim = self.claim(run).json()["claim"]
        other = self.create(tenant="b", body={**self.body, "title": "Tenant B goal", "description": "Other tenant data"})
        before = self.events(run)
        cases = [("GET", f"/v1/goals/{run}", None), ("GET", f"/v1/goals/{run}/plan", None),
                 ("PUT", f"/v1/goals/{run}/plan", {"definition": self.definition, "expected_revision": 1}),
                 ("POST", f"/v1/goals/{run}/plan/approve", {"expected_revision": 1, "expected_digest": saved["digest"]}),
                 ("POST", f"/v1/runs/{run}/claim", {"worker_id": "tenant-b-worker"}),
                 ("POST", f"/v1/runs/{run}/tasks", {"task_id": "foreign", "title": "Foreign insertion"}),
                 ("POST", f"/v1/runs/{run}/tasks/{claim['task_id']}/submit", {"worker_id": "worker", "epoch": claim["epoch"], "output": {"x": 1}})]
        cases += [("GET", f"/v1/runs/{run}{suffix}", None) for suffix in ("", "/events", "/replay", "/integrity")]
        for index, (method, path, body) in enumerate(cases):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers=self.headers("foreign-" + str(index), "b"), json=body)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn(self.body["description"], response.text)
                self.assertNotIn(saved["digest"], response.text)
        listing = self.client.get("/v1/goals", headers=self.headers(tenant="b"))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["run_id"] for item in listing.json()["goals"]], [other["run_id"]])
        self.assertEqual(self.events(run), before)
        self.assertEqual(self.get(f"/v1/goals/{run}/plan")["task_ids"], approved["task_ids"])

    def test_all_goal_routes_require_tenant_authentication(self):
        run = self.create()["run_id"]
        saved = self.save(run)
        cases = [("POST", "/v1/goals", self.body), ("GET", "/v1/goals", None),
                 ("GET", f"/v1/goals/{run}", None), ("GET", f"/v1/goals/{run}/plan", None),
                 ("PUT", f"/v1/goals/{run}/plan", {"definition": self.definition, "expected_revision": 1}),
                 ("POST", f"/v1/goals/{run}/plan/approve", {"expected_revision": 1, "expected_digest": saved["digest"]})]
        before = self.events(run)
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers={"Idempotency-Key": "unauthorized"}, json=body)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.events(run), before)

    def test_invalid_roles_tools_or_graph_are_422_without_echoing_plan_input(self):
        run = self.create()["run_id"]
        marker = "DO_NOT_ECHO_PLAN_BODY_9a30"
        variants = []
        def variant(name, mutate):
            definition = copy.deepcopy(self.definition)
            definition["nodes"][0]["instruction"] = marker
            mutate(definition)
            variants.append((name, definition))
        variant("unknown-role", lambda value: value["nodes"][0].update(role=marker))
        variant("unknown-tool", lambda value: value["roles"][0]["tools"].append(marker))
        variant("tool-escalation", lambda value: value["roles"][0]["tools"].append("workspace.write"))
        variant("skill-escalation", lambda value: value["roles"][0]["skills"].append(marker))
        variant("cycle", lambda value: value["nodes"][0].update(depends=["verify"]))
        variant("self-dependency", lambda value: value["nodes"][0].update(depends=["spec"]))
        variant("missing-dependency", lambda value: value["nodes"][0].update(depends=["missing"]))
        variant("duplicate-role", lambda value: value["roles"][1].update(id="analyst"))
        variant("missing-role", lambda value: value["roles"].pop())
        variant("unauthorized-channel", lambda value: value["nodes"][0].update(channel="verification"))
        variant("arbitrary-scope", lambda value: value.update(scope=marker))
        variant("shell-command", lambda value: value["nodes"][0].update(command=marker))
        before = self.events(run)
        for name, definition in variants:
            with self.subTest(variant=name):
                response = self.client.put(f"/v1/goals/{run}/plan", headers=self.headers("invalid-" + name),
                                           json={"definition": definition, "expected_revision": 0})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "INVALID_INPUT")
                self.assertNotIn(marker, response.text)
                self.assertNotIn('"input"', response.text)
        self.assertEqual(self.events(run), before)
        self.assertEqual(self.get(f"/v1/goals/{run}/plan")["revision"], 0)
        self.assertEqual(self.get(f"/v1/runs/{run}")["tasks"], [])

    def test_invalid_goal_input_and_identity_overrides_leave_no_goal(self):
        marker = "DO_NOT_ECHO_GOAL_BODY_f391"
        variants = [("blank-title", {"title": " "}), ("blank-description", {"description": "\n"}),
                    ("unknown-criterion", {"acceptance": [marker]}), ("duplicate-criterion", {"acceptance": ["tests.pass", "tests.pass"]}),
                    ("negative-cost", {"cost_limit_microusd": -1}), ("boolean-cost", {"cost_limit_microusd": True}),
                    ("string-token-count", {"token_limit": "10000"}), ("excess-token-count", {"token_limit": 1000000001}),
                    ("tenant-override", {"tenant_id": "tenant-b"}), ("ruleset-override", {"ruleset": marker})]
        for name, changes in variants:
            with self.subTest(variant=name):
                response = self.client.post("/v1/goals", headers=self.headers("bad-" + name),
                                            json={**self.body, "description": marker, **changes})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "INVALID_INPUT")
                self.assertNotIn(marker, response.text)
                self.assertNotIn('"input"', response.text)
        self.assertEqual(self.get("/v1/goals")["goals"], [])

    def test_budget_oversubscription_cannot_partially_approve_or_create_tasks(self):
        for field, limit in (("cost_limit_microusd", 699999), ("token_limit", 6999)):
            with self.subTest(field=field):
                run = self.create("budget-goal-" + field, body={**self.body, field: limit})["run_id"]
                saved = self.save(run, "budget-save-" + field)
                before = self.events(run)
                response = self.client.post(f"/v1/goals/{run}/plan/approve", headers=self.headers("budget-approve-" + field),
                                            json={"expected_revision": saved["revision"], "expected_digest": saved["digest"]})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["code"], "BUDGET_EXCEEDED")
                self.assertEqual(self.events(run), before)
                self.assertEqual(self.get(f"/v1/runs/{run}")["tasks"], [])
                self.assertEqual(self.get(f"/v1/goals/{run}")["status"], "draft")


if __name__ == "__main__":
    unittest.main()
