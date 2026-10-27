"""Real Docker, simulated model HTTP: execution-time auxiliary context fences."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

import httpx

from loom.artifacts import ArtifactService
from loom.execution import LocalTaskRunner, freeze_tests
from loom.model_gateway import ModelGateway, ModelRoute
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig
from loom.store import Store, canonical
from loom.workflow import WorkflowService

PACK = Path(__file__).resolve().parents[2] / "packs/software-dev/workflow-v1.json"
REQUIREMENTS = {"schema_version": "loom.requirements/v1", "summary": "Untrusted local fixture requirements.",
                "items": [{"id": "one", "text": "Read value and return answer equal to value plus one."}], "uncertainties": []}
SOURCE = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'
BAD_SOURCE = 'import json\nprint(json.dumps({"answer":0}))\n'
PUBLIC = (FrozenTest("public-example", {"value": 2}, {"answer": 3}),)
HIDDEN = (FrozenTest("hidden-context-case-do-not-send", {"value": 673091}, {"answer": 673092}),)
FAKE_KEY = "context-docker-fixture-key-only"


class ObservedSandbox(DockerSandbox):
    def __init__(self, config):
        super().__init__(config)
        self.executions = []
        self.after_run = None

    def run_python(self, files, tests, *, entrypoint="main.py"):
        result = super().run_python(files, tests, entrypoint=entrypoint)
        self.executions.append({"cases": len(result.cases), "passed": result.passed})
        if self.after_run:
            self.after_run()
        return result


@unittest.skipUnless(os.environ.get("LOOM_SANDBOX_TEST_IMAGE"), "explicit approved sandbox image not configured")
class LiveContextTests(unittest.IsolatedAsyncioTestCase):
    observations = []

    @classmethod
    def setUpClass(cls):
        cls.observations = []
        cls.config = SandboxConfig(os.environ["LOOM_SANDBOX_TEST_IMAGE"], timeout_seconds=5)
        DockerSandbox(cls.config).preflight()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "context-live.db")
        self.workflows = WorkflowService(self.store)
        self.artifacts = ArtifactService(self.store)
        self.sandbox = ObservedSandbox(self.config)
        self.requests = []
        self.source = SOURCE
        self.gateway = ModelGateway([ModelRoute("deepseek-flash", timeout_seconds=1)],
            transport=httpx.MockTransport(self.http), environ={"DEEPSEEK_API_KEY": FAKE_KEY})
        self.addAsyncCleanup(self.gateway.aclose)
        self.runner = LocalTaskRunner(self.store, self.gateway, self.sandbox)

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def tearDown(self):
        self.observations.append({"test": self._testMethodName, "mock_http_requests": len(self.requests),
            "real_sandbox_runs": len(self.sandbox.executions),
            "real_container_cases": sum(row["cases"] for row in self.sandbox.executions)})

    def http(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"id": "context-live-fixture", "object": "chat.completion", "created": 1792700000,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"source": self.source})}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}})

    def make_claim(self, *, cycle=False, approve=True):
        goal = self.workflows.create_goal("context-live", "goal", title="Context Docker check", description="Use approved auxiliary requirements.",
            acceptance=["tests.pass"], cost_limit=1001000, token_limit=3001000)
        self.run = goal["run_id"]
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        limits = {"cost_limit_microusd": 100, "token_limit": 100, "max_attempts": 3}
        work = {"id": "work", "title": "Implement", "instruction": "Implement the approved auxiliary requirements.",
            "role": "developer", "depends": [], "priority": 0,
            "limits": {"cost_limit_microusd": 1000000, "token_limit": 3000000, "max_attempts": 2},
            "evaluation_digest": freeze_tests(HIDDEN)[1], "input_artifacts": [{"source_node": "spec", "kind": "requirements",
                "schema_version": "loom.requirements/v1", "required": True}]}
        if cycle:
            work["repair"] = {"schema_version": "loom.rebind/v1", "development_digest": freeze_tests(PUBLIC)[1],
                              "max_repairs": 1, "stop_on_duplicate_source": True}
        definition["nodes"] = [
            {"id": "spec", "title": "Requirements", "instruction": "Provide data.", "role": "analyst", "depends": [], "priority": 10, "limits": limits},
            work,
            {"id": "child", "title": "Review", "instruction": "Independent review.", "role": "reviewer", "depends": ["work"], "limits": limits}]
        saved = self.workflows.save_plan("context-live", self.run, "plan", definition, 0)
        approved = self.workflows.approve_plan("context-live", self.run, "approve", saved["revision"], saved["digest"])
        self.source_claim = self.store.claim_next("context-live", self.run, "context-source", lease_seconds=300)
        self.candidate = self.artifacts.save_candidate(self.source_claim, "candidate", "requirements", REQUIREMENTS, actor="subject:context-source")
        self.claim = self.store.claim_next("context-live", self.run, "context-code", lease_seconds=300)
        self.assertEqual(self.claim.task_id, approved["task_ids"]["work"])
        if approve:
            self.artifacts.approve_input("context-live", self.run, self.claim.task_id, self.candidate["artifact_id"], "input", actor="subject:operator",
                expected_plan_digest=saved["digest"], expected_source_epoch=self.source_claim.epoch,
                expected_target_epoch=self.claim.epoch, expected_revision=self.candidate["revision"], expected_content_hash=self.candidate["content_hash"])
        return self.claim

    def events(self, kind):
        return [event for event in self.store.events("context-live", self.run) if event["type"] == kind]

    async def test_approved_context_reaches_mock_http_then_real_docker_without_verification_bypass(self):
        claim = self.make_claim()
        result = await self.runner.run_python(claim, HIDDEN, max_output_tokens=128)
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["verified"])
        self.assertEqual(len(self.requests), 1)
        payload = json.loads(self.requests[0].content)
        incoming = json.loads(payload["messages"][1]["content"])
        self.assertEqual(incoming["auxiliary_inputs"]["items"][0]["body"], REQUIREMENTS)
        for forbidden in (FAKE_KEY, self.source_claim.worker_id, claim.task_id, HIDDEN[0].name, canonical(HIDDEN[0].input), canonical(HIDDEN[0].expected)):
            self.assertNotIn(forbidden, canonical(payload["messages"]))
        bound = self.events("execution.context_bound")
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["payload"]["epoch"], claim.epoch)
        self.assertNotIn("body", canonical(bound[0]["payload"]))
        self.assertEqual(len(self.sandbox.executions), 1)
        self.assertIsNone(self.store.claim_next("context-live", self.run, "child-reader"))
        self.assertTrue(self.store.verify_events(self.store.events("context-live", self.run)))

    async def test_context_revision_after_real_container_blocks_submission_but_keeps_usage(self):
        claim = self.make_claim()
        self.sandbox.after_run = lambda: self.artifacts.save_candidate(self.source_claim, "new-candidate", "requirements",
            {**REQUIREMENTS, "summary": "New revision after isolated execution."}, actor="subject:context-source")
        result = await self.runner.run_python(claim, HIDDEN, max_output_tokens=128)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.sandbox.executions), 1)
        self.assertEqual(self.store.snapshot("context-live", self.run)["tokens_spent"], 60)
        self.assertEqual(self.events("task.output_submitted"), [])

    async def test_rebind_new_epoch_requires_new_auxiliary_approval_and_never_sends_second_http(self):
        claim = self.make_claim(cycle=True)
        self.source = BAD_SOURCE
        result = await self.runner.run_python_cycle(claim, HIDDEN, PUBLIC, max_output_tokens=128)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["repairs_applied"], 1)
        self.assertEqual(result["attempts_completed"], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.sandbox.executions), 1)
        self.assertEqual(len(self.events("execution.context_bound")), 1)
        self.assertEqual(self.store.snapshot("context-live", self.run)["tokens_spent"], 60)
        self.assertEqual(self.events("task.output_submitted"), [])

    async def test_missing_required_input_never_creates_budget_intent_http_or_container(self):
        claim = self.make_claim(approve=False)
        result = await self.runner.run_python(claim, HIDDEN, max_output_tokens=128)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.sandbox.executions, [])
        self.assertEqual(self.events("tool.intent"), [])
        self.assertEqual(self.events("budget.reserved"), [])
