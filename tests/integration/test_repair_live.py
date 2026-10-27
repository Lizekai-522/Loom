"""Real Docker repair-cycle checks with exclusively simulated model HTTP.

No candidate executes on the host. Each real executor call delegates to the
approved DockerSandbox, which owns and removes its exact container. Model
credentials are explicit test strings; no provider or identity config is read.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

import httpx

from loom.execution import FLASH_CONTEXT_LIMIT, LocalTaskRunner, cost_upper_microusd, freeze_tests
from loom.model_gateway import ModelGateway, ModelRoute
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig
from loom.store import Conflict, StaleClaim, Store, canonical, digest
from loom.workflow import WorkflowService


PACK = Path(__file__).resolve().parents[2] / "packs/software-dev/workflow-v1.json"
FAKE_CREDENTIAL = "repair-integration-simulated-credential-only"
BAD_SOURCE = 'import json\nprint(json.dumps({"answer":0}))\n'
GOOD_SOURCE = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'
DEVELOPMENT = (FrozenTest("public-add-one", {"value": 1}, {"answer": 2}),)
ACCEPTANCE = (FrozenTest("hidden-case-never-in-model-http", {"value": 928341}, {"answer": 928342}),)


class ObservedDockerSandbox(DockerSandbox):
    """Observe real execution without fabricating results or replacing Docker."""

    def __init__(self, config):
        super().__init__(config)
        self.executions = []

    def run_python(self, files, tests, *, entrypoint="main.py"):
        result = super().run_python(files, tests, entrypoint=entrypoint)
        self.executions.append({"source_digest": digest(files),
                                "tests_digest": freeze_tests(tests)[1],
                                "passed": result.passed, "cases": len(result.cases)})
        return result


@unittest.skipUnless(os.environ.get("LOOM_SANDBOX_TEST_IMAGE"), "explicit approved sandbox image not configured")
class LiveRepairTests(unittest.IsolatedAsyncioTestCase):
    observations = []

    @classmethod
    def setUpClass(cls):
        cls.observations = []
        cls.config = SandboxConfig(os.environ["LOOM_SANDBOX_TEST_IMAGE"], timeout_seconds=5)
        DockerSandbox(cls.config).preflight()  # Configured unavailable Docker must fail, never skip.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "live-repair.db")
        self.workflows = WorkflowService(self.store)
        self.sandbox = ObservedDockerSandbox(self.config)
        self.sources = [BAD_SOURCE, GOOD_SOURCE]
        self.requests = []
        self.provider_error = False
        self.gateway = ModelGateway([ModelRoute("deepseek-flash", timeout_seconds=1)],
                                    transport=httpx.MockTransport(self.model_http),
                                    environ={"DEEPSEEK_API_KEY": FAKE_CREDENTIAL})
        self.addAsyncCleanup(self.gateway.aclose)
        self.runner = LocalTaskRunner(self.store, self.gateway, self.sandbox)

    def tearDown(self):
        self.observations.append({"test": self._testMethodName,
                                  "mock_http_requests": len(self.requests),
                                  "real_sandbox_runs": len(self.sandbox.executions),
                                  "real_container_cases": sum(row["cases"] for row in self.sandbox.executions)})

    async def asyncSetUp(self):
        # unittest enables asyncio's slow-task diagnostic by default; its task
        # repr can include return values. Public reports contain summaries only.
        asyncio.get_running_loop().set_debug(False)

    def model_http(self, request):
        self.requests.append(request)
        if self.provider_error:
            raise httpx.ReadTimeout("simulated response uncertainty", request=request)
        index = len(self.requests) - 1
        if index >= len(self.sources):
            self.fail("unexpected additional simulated model request")
        return httpx.Response(200, json={
            "id": "simulated-repair-completion-" + str(index + 1),
            "object": "chat.completion", "created": 1792700000, "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"source": self.sources[index]})}}],
            "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35,
                      "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 25}})

    def make_claim(self, *, acceptance=ACCEPTANCE, max_repairs=1):
        created = self.workflows.create_goal(
            "live-repair", "goal", title="Real Docker repair-cycle check",
            description="Implement an integer JSON program for the approved public instruction.",
            acceptance=["tests.pass"], cost_limit=1_000_100, token_limit=4_000_100)
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        definition["nodes"] = [{
            "id": "work", "title": "Implement add one", "instruction": "Add one to input value; output answer.",
            "role": "developer", "depends": [], "priority": 10, "channel": "business",
            "limits": {"cost_limit_microusd": 1_000_000, "token_limit": 4_000_000,
                       "max_attempts": max_repairs + 1},
            "evaluation_digest": freeze_tests(acceptance)[1],
            "repair": {"schema_version": "loom.rebind/v1", "development_digest": freeze_tests(DEVELOPMENT)[1],
                       "max_repairs": max_repairs, "stop_on_duplicate_source": True}}, {
            "id": "child", "title": "Dependent review", "instruction": "Inspect the independently verified parent.",
            "role": "reviewer", "depends": ["work"], "channel": "business",
            "limits": {"cost_limit_microusd": 100, "token_limit": 100, "max_attempts": 1}}]
        run_id = created["run_id"]
        saved = self.workflows.save_plan("live-repair", run_id, "save", definition, 0)
        approved = self.workflows.approve_plan("live-repair", run_id, "approve", saved["revision"], saved["digest"])
        claim = self.store.claim_next("live-repair", run_id, "live-mock-worker", lease_seconds=90)
        self.assertEqual(claim.task_id, approved["task_ids"]["work"])
        return claim

    def rows(self, table, claim):
        assert table in {"effects", "reservations", "tasks"}
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE tenant=? AND run_id=? ORDER BY rowid",
                                                   (claim.tenant_id, claim.run_id))]

    def events(self, claim, kind):
        return [event for event in self.store.events(claim.tenant_id, claim.run_id) if event["type"] == kind]

    def assert_prompt_isolation(self, acceptance=ACCEPTANCE):
        for request in self.requests:
            self.assertEqual(request.headers["authorization"], "Bearer " + FAKE_CREDENTIAL)
            payload = json.loads(request.content)
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            prompt = canonical(payload["messages"])
            self.assertNotIn(FAKE_CREDENTIAL, prompt)
            for case in acceptance:
                self.assertNotIn(case.name, prompt)
                self.assertNotIn(canonical(case.input), prompt)
                self.assertNotIn(canonical(case.expected), prompt)

    def assert_replay(self, claim):
        events = self.store.events(claim.tenant_id, claim.run_id)
        self.assertTrue(self.store.verify_events(events), "event hash chain must verify")
        snapshot = self.store.snapshot(claim.tenant_id, claim.run_id)
        replay = self.store.replay(claim.tenant_id, claim.run_id)
        for key in ("status", "spent", "reserved", "tokens_spent", "tokens_reserved"):
            self.assertEqual(snapshot[key], replay[key], key)
        for task in snapshot["tasks"]:
            reconstructed = replay["tasks"][task["id"]]
            self.assertEqual((task["status"], task["epoch"]),
                             (reconstructed["status"], reconstructed["epoch"]))
        self.assertEqual(replay["effects"], {row["id"]: row["state"] for row in self.rows("effects", claim)})
        reservations = self.rows("reservations", claim)
        self.assertEqual(set(replay["reservations"]), {row["id"] for row in reservations})
        for row in reservations:
            reconstructed = replay["reservations"][row["id"]]
            for key in ("state", "cost", "tokens"):
                self.assertEqual(row[key], reconstructed[key], key)
            if row["state"] == "settled":
                self.assertEqual(row["actual_cost"], reconstructed["actual_cost"])
                self.assertEqual(row["actual_tokens"], reconstructed["actual_tokens"])
        self.assertEqual(snapshot["spent"], sum(row["actual_cost"] or 0 for row in reservations))
        self.assertEqual(snapshot["tokens_spent"], sum(row["actual_tokens"] or 0 for row in reservations))
        self.assertNotIn(FAKE_CREDENTIAL, canonical(events))
        self.assertEqual(replay["last_seq"], len(events))

    async def test_real_public_failure_rebind_then_hidden_pass_only_submits(self):
        claim = self.make_claim()
        result = await self.runner.run_python_cycle(claim, ACCEPTANCE, DEVELOPMENT, max_output_tokens=128)
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["verified"])
        self.assertTrue(result["hard_criteria_passed"])
        self.assertTrue(result["repair_enabled"])
        self.assertTrue(result["development_passed"])
        self.assertEqual((result["repairs_applied"], result["attempts_completed"]), (1, 2))
        self.assertIsNone(result["stop_reason"])
        self.assertTrue(result["usage_complete"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([(row["tests_digest"], row["passed"]) for row in self.sandbox.executions],
                         [(freeze_tests(DEVELOPMENT)[1], False), (freeze_tests(DEVELOPMENT)[1], True),
                          (freeze_tests(ACCEPTANCE)[1], True)])
        candidates = self.events(claim, "execution.candidate")
        self.assertEqual(len(candidates), 2)
        public = self.events(claim, "development.evaluated")
        repairs = self.events(claim, "repair.applied")
        hidden = self.events(claim, "execution.evaluated")
        self.assertEqual((len(public), len(repairs), len(hidden)), (2, 1, 1))
        self.assertEqual(repairs[0]["payload"]["primitive"], "Rebind")
        self.assertEqual((repairs[0]["payload"]["from_epoch"], repairs[0]["payload"]["to_epoch"]), (1, 2))
        self.assertEqual(repairs[0]["payload"]["candidate_event_hash"], candidates[0]["hash"])
        self.assertEqual(repairs[0]["payload"]["development_event_hash"], public[0]["hash"])
        second_message = json.loads(self.requests[1].content)["messages"][1]
        repair_context = json.loads(second_message["content"])["public_repair_context"]
        self.assertEqual(repairs[0]["payload"]["context_digest"], digest(repair_context))
        self.assertEqual(repair_context["previous_source"], BAD_SOURCE)
        self.assertEqual(repair_context["public_failures"][0]["name"], DEVELOPMENT[0].name)
        self.assertFalse(repair_context["public_failures"][0]["passed"])
        self.assertLess(public[0]["seq"], repairs[0]["seq"])
        self.assertLess(repairs[0]["seq"], public[1]["seq"])
        self.assertLess(public[1]["seq"], hidden[0]["seq"])
        self.assertEqual(self.events(claim, "artifact.verified"), [])
        self.assertIsNone(self.store.claim_next(claim.tenant_id, claim.run_id, "downstream-worker"))
        with self.assertRaises(StaleClaim):
            self.store.renew_claim(claim, lease_seconds=90)
        reservations = self.rows("reservations", claim)
        self.assertEqual(len(reservations), 2)
        self.assertEqual([row["epoch"] for row in reservations], [1, 2])
        self.assertTrue(all(row["state"] == "settled" and row["actual_tokens"] == 35 and
                            row["actual_cost"] == cost_upper_microusd(25, 10) for row in reservations))
        effects = self.rows("effects", claim)
        self.assertEqual(len(effects), 2)
        self.assertTrue(all(row["state"] == "done" for row in effects))
        self.assert_prompt_isolation()
        self.assert_replay(claim)

    async def test_hidden_acceptance_failure_is_not_a_repair_signal_or_model_feedback(self):
        self.sources = [GOOD_SOURCE]
        acceptance = (FrozenTest("hidden-failing-case-never-in-model-http", {"value": 928341}, {"answer": 783625}),)
        claim = self.make_claim(acceptance=acceptance)
        result = await self.runner.run_python_cycle(claim, acceptance, DEVELOPMENT, max_output_tokens=128)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["reason"], "ACCEPTANCE_TESTS_FAILED")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([(row["tests_digest"], row["passed"]) for row in self.sandbox.executions],
                         [(freeze_tests(DEVELOPMENT)[1], True), (freeze_tests(acceptance)[1], False)])
        self.assertEqual(self.events(claim, "repair.applied"), [])
        evaluated = self.events(claim, "execution.evaluated")
        self.assertEqual(len(evaluated), 1)
        candidate = self.events(claim, "execution.candidate")[0]
        self.assertEqual(evaluated[0]["payload"]["candidate_event_hash"], candidate["hash"])
        report = evaluated[0]["payload"]["acceptance_report"]
        self.assertFalse(report["passed"])
        self.assertEqual(report["source_sha256"], digest({"main.py": GOOD_SOURCE}))
        self.assertEqual(report["tests_sha256"], freeze_tests(acceptance)[1])
        self.assertEqual(len(report["cases"]), 1)
        self.assertEqual(report["cases"][0]["name"], acceptance[0].name)
        self.assertFalse(report["cases"][0]["passed"])
        self.assertEqual(report["cases"][0]["status"], "failed")
        self.assertEqual(json.loads(report["cases"][0]["stdout"]), {"answer": 928342})
        self.assertNotIn('"input":', canonical(report))
        self.assertNotIn('"expected":', canonical(report))
        for request in self.requests:
            prompt = canonical(json.loads(request.content)["messages"])
            self.assertNotIn(canonical(report), prompt)
            self.assertNotIn("928342", prompt)
        self.assertEqual(self.events(claim, "artifact.verified"), [])
        with self.assertRaises(Conflict):
            self.store.claim_next(claim.tenant_id, claim.run_id, "downstream-worker")
        self.assert_prompt_isolation(acceptance)
        self.assert_replay(claim)

    async def test_duplicate_candidate_stops_before_another_real_container(self):
        self.sources = [BAD_SOURCE, BAD_SOURCE]
        claim = self.make_claim(max_repairs=2)
        result = await self.runner.run_python_cycle(claim, ACCEPTANCE, DEVELOPMENT, max_output_tokens=128)
        self.assertEqual(result["reason"], "REPEATED_SOURCE")
        self.assertFalse(result["verified"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.sandbox.executions), 1)
        self.assertFalse(self.sandbox.executions[0]["passed"])
        self.assertEqual(self.events(claim, "execution.evaluated"), [])
        self.assertEqual(len(self.events(claim, "repair.applied")), 1)
        self.assertEqual(self.events(claim, "artifact.verified"), [])
        self.assert_prompt_isolation()
        self.assert_replay(claim)

    async def test_unknown_http_retains_budget_and_never_automatically_redispatches(self):
        self.provider_error = True
        claim = self.make_claim()
        result = await self.runner.run_python_cycle(claim, ACCEPTANCE, DEVELOPMENT, max_output_tokens=128)
        self.assertEqual(result["reason"], "MODEL_TIMEOUT")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertTrue(result["billing_uncertain"])
        self.assertFalse(result["usage_complete"])
        self.assertFalse(result["verified"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.sandbox.executions, [])
        reservations = self.rows("reservations", claim)
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["state"], "reserved")
        self.assertEqual(reservations[0]["tokens"], FLASH_CONTEXT_LIMIT + 128)
        self.assertEqual(reservations[0]["cost"], cost_upper_microusd(FLASH_CONTEXT_LIMIT, 128))
        self.assertIsNone(reservations[0]["actual_cost"])
        self.assertEqual([row["state"] for row in self.rows("effects", claim)], ["indeterminate"])
        self.assertEqual(self.events(claim, "repair.applied"), [])
        with self.assertRaises(Conflict):
            await self.runner.run_python_cycle(claim, ACCEPTANCE, DEVELOPMENT, max_output_tokens=128)
        self.assertEqual(len(self.requests), 1)
        self.assert_prompt_isolation()
        self.assert_replay(claim)


if __name__ == "__main__":
    unittest.main()
