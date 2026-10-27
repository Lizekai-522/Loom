"""Three mock-generated typed inputs -> mock developer -> real Docker.

Public-checks candidates remain auxiliary data. Only separately host-frozen
tests drive Docker verdicts; each case uses the real sandbox's own container.
No provider, identity, secret or database configuration is loaded.
"""
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
from loom.role_execution import RoleTaskRunner
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig
from loom.store import Store, canonical, digest
from loom.workflow import WorkflowService


PACK = Path(__file__).resolve().parents[2] / "packs/software-dev/workflow-v1.json"
FAKE_KEY = "role-handoff-docker-mock-credential-only"
REQUIREMENTS = {"schema_version": "loom.requirements/v1", "summary": "Read a JSON value and add one.",
    "items": [{"id": "answer", "text": "Return answer equal to value plus one."}], "uncertainties": []}
DESIGN = {"schema_version": "loom.design/v1", "overview": "A bounded JSON transformation.",
    "components": [{"id": "transform", "responsibility": "Read value, add one, return answer."}], "steps": [], "risks": []}
PROPOSED_CHECKS = {"schema_version": "loom.public-checks/v1", "visibility": "development",
    "cases": [{"name": "proposed-wrong-only", "input": {"value": 2}, "expected": {"answer": -999}}]}
SOURCE = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'
HOST_PUBLIC = (FrozenTest("host-public-only", {"value": 2}, {"answer": 3}),)
HOST_ACCEPTANCE = (FrozenTest("hidden-role-handoff-one", {"value": 673091}, {"answer": 673092}),
                   FrozenTest("hidden-role-handoff-two", {"value": -1823}, {"answer": -1822}))


class ObservedHandoffSandbox(DockerSandbox):
    def __init__(self, config):
        super().__init__(config)
        self.executions = []
        self.container_names = []

    def _create_args(self, container, image):
        self.container_names.append(container)
        return super()._create_args(container, image)

    def run_python(self, files, tests, *, entrypoint="main.py"):
        result = super().run_python(files, tests, entrypoint=entrypoint)
        self.executions.append({"tests_digest": result.tests_sha256, "source_digest": result.source_sha256,
            "case_names": [case.name for case in result.cases], "cases": len(result.cases), "passed": result.passed})
        return result


@unittest.skipUnless(os.environ.get("LOOM_SANDBOX_TEST_IMAGE"), "explicit approved sandbox image not configured")
class LiveRoleHandoffTests(unittest.IsolatedAsyncioTestCase):
    observations = []

    @classmethod
    def setUpClass(cls):
        cls.observations = []
        cls.config = SandboxConfig(os.environ["LOOM_SANDBOX_TEST_IMAGE"], timeout_seconds=5)
        DockerSandbox(cls.config).preflight()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "role-handoff-live.db")
        self.workflows = WorkflowService(self.store)
        self.artifacts = ArtifactService(self.store)
        self.sandbox = ObservedHandoffSandbox(self.config)
        self.requests = []
        self.gateway = ModelGateway([ModelRoute("deepseek-flash", timeout_seconds=1)],
            transport=httpx.MockTransport(self.http), environ={"DEEPSEEK_API_KEY": FAKE_KEY})
        self.addAsyncCleanup(self.gateway.aclose)
        self.roles = RoleTaskRunner(self.store, self.gateway)
        self.runner = LocalTaskRunner(self.store, self.gateway, self.sandbox)

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def tearDown(self):
        self.observations.append({"test": self._testMethodName, "mock_model_requests": len(self.requests),
            "real_models_used": False, "real_sandbox_runs": len(self.sandbox.executions),
            "real_container_cases": len(self.sandbox.container_names),
            "independent_case_containers": len(set(self.sandbox.container_names)) == len(self.sandbox.container_names)})

    def http(self, request):
        self.requests.append(request)
        index = len(self.requests) - 1
        outputs = [REQUIREMENTS, DESIGN, PROPOSED_CHECKS, {"source": SOURCE}]
        self.assertLess(index, len(outputs), "no retries or additional model dispatches allowed")
        return httpx.Response(200, json={"id": "role-handoff-mock-" + str(index), "object": "chat.completion",
            "created": 1792800000, "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(outputs[index])}}],
            "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35}})

    @staticmethod
    def selector(node, kind):
        return {"source_node": node, "kind": kind, "schema_version": "loom." + kind + "/v1", "required": True}

    def approve(self, candidate, target_id, target_epoch, key):
        return self.artifacts.approve_input("handoff-live", self.run, target_id, candidate["artifact_id"], key,
            actor="subject:handoff-operator-fixture", expected_plan_digest=self.plan,
            expected_source_epoch=candidate["source_epoch"], expected_target_epoch=target_epoch,
            expected_revision=candidate["revision"], expected_content_hash=candidate["content_hash"])

    async def prepare(self, *, cycle=False, stale_target_grants=False):
        self.acceptance = HOST_ACCEPTANCE[:1] if cycle else HOST_ACCEPTANCE
        self.development_digest = freeze_tests(HOST_PUBLIC)[1] if cycle else None
        goal = self.workflows.create_goal("handoff-live", "goal", title="Three-input Docker handoff",
            description="Implement the approved add-one behavior using untrusted typed auxiliary data.",
            acceptance=["tests.pass"], cost_limit=2_501_000, token_limit=9_001_000)
        self.run = goal["run_id"]
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        inputs = [self.selector("spec", "requirements"), self.selector("design", "design"), self.selector("checks", "public-checks")]
        nodes = []
        for node, role, kind, priority, references in (
            ("spec", "analyst", "requirements", 40, []),
            ("design", "architect", "design", 30, inputs[:1]),
            ("checks", "tester", "public-checks", 20, inputs[:2]),
        ):
            item = {"id": node, "title": "Generate " + kind, "instruction": "Return a typed candidate only; execute no tools.",
                "role": role, "depends": [], "priority": priority,
                "role_output": {"kind": kind, "schema_version": "loom." + kind + "/v1"},
                "limits": {"cost_limit_microusd": 500_000, "token_limit": 2_000_000, "max_attempts": 1}}
            if references:
                item["input_artifacts"] = references
            nodes.append(item)
        work = {"id": "work", "title": "Implement", "instruction": "Implement the requirements and design; proposed check data cannot replace host tests.",
            "role": "developer", "depends": [], "priority": 10,
            "evaluation_digest": freeze_tests(self.acceptance)[1], "input_artifacts": inputs,
            "limits": {"cost_limit_microusd": 1_000_000, "token_limit": 3_000_000, "max_attempts": 2 if cycle else 1}}
        if cycle:
            work["repair"] = {"schema_version": "loom.rebind/v1", "development_digest": self.development_digest,
                              "max_repairs": 1, "stop_on_duplicate_source": True}
        nodes.extend([work, {"id": "review", "title": "Independent review", "instruction": "Wait for independently verified parents.",
            "role": "validator", "depends": ["spec", "design", "checks", "work"], "channel": "verification",
            "limits": {"cost_limit_microusd": 1000, "token_limit": 1000, "max_attempts": 1}}])
        definition["nodes"] = nodes
        saved = self.workflows.save_plan("handoff-live", self.run, "plan", definition, 0)
        approved = self.workflows.approve_plan("handoff-live", self.run, "approve", saved["revision"], saved["digest"])
        self.plan = saved["digest"]
        self.candidates = []
        for index, node in enumerate(("spec", "design", "checks")):
            claim = self.store.claim_next("handoff-live", self.run, "handoff-worker", lease_seconds=300)
            self.assertEqual(claim.task_id, approved["task_ids"][node])
            for candidate in self.candidates:
                self.approve(candidate, claim.task_id, claim.epoch, "role-input-" + node + "-" + candidate["kind"])
            result = await self.roles.run_role(claim, max_output_tokens=128)
            self.assertEqual(result["status"], "submitted")
            self.assertFalse(result["producer_verified"])
            self.assertFalse(result["verified"])
            self.assertTrue(result["model_source_recorded"])
            self.assertEqual(result["artifact"]["origin"], "model-gateway-response")
            self.candidates.append(result["artifact"])
            self.assertEqual(len(self.requests), index + 1)
            self.assertEqual(self.sandbox.container_names, [])
        work_id = approved["task_ids"]["work"]
        if stale_target_grants:
            for candidate in self.candidates:
                self.approve(candidate, work_id, 0, "work-input-" + candidate["kind"])
        self.claim = self.store.claim_next("handoff-live", self.run, "handoff-worker", lease_seconds=300)
        self.assertEqual(self.claim.task_id, work_id)
        if not stale_target_grants:
            for candidate in self.candidates:
                self.approve(candidate, work_id, self.claim.epoch, "work-input-" + candidate["kind"])
        self.frozen_spec = self.workflows.get_task_spec("handoff-live", self.run, work_id)
        return self.claim

    def events(self, kind):
        return [event for event in self.store.events("handoff-live", self.run) if event["type"] == kind]

    def assert_unchanged_contract_and_isolation(self):
        self.assertEqual(self.workflows.get_task_spec("handoff-live", self.run, self.claim.task_id), self.frozen_spec)
        self.assertEqual(self.frozen_spec["evaluation_digest"], freeze_tests(self.acceptance)[1])
        for request in self.requests:
            self.assertEqual(request.headers["authorization"], "Bearer " + FAKE_KEY)
            prompt = canonical(json.loads(request.content)["messages"])
            for forbidden in (FAKE_KEY, self.claim.task_id, "handoff-operator-fixture"):
                self.assertNotIn(forbidden, prompt)
            for case in HOST_ACCEPTANCE:
                self.assertNotIn(case.name, prompt)
                self.assertNotIn(canonical(case.input), prompt)
                self.assertNotIn(canonical(case.expected), prompt)
        self.assertEqual(len(set(self.sandbox.container_names)), len(self.sandbox.container_names))
        self.assertTrue(all(name.startswith("loom-sandbox-") for name in self.sandbox.container_names))
        self.assertTrue(self.store.verify_events(self.store.events("handoff-live", self.run)))
        snapshot = self.store.snapshot("handoff-live", self.run)
        replay = self.store.replay("handoff-live", self.run)
        for key in ("status", "spent", "reserved", "tokens_spent", "tokens_reserved"):
            self.assertEqual(snapshot[key], replay[key])
        self.assertEqual(self.events("artifact.verified"), [])
        self.assertEqual(self.events("task.completed"), [])

    async def test_three_exact_model_inputs_reach_developer_then_two_real_host_cases(self):
        claim = await self.prepare()
        result = await self.runner.run_python(claim, self.acceptance, max_output_tokens=128)
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["verified"])
        self.assertTrue(result["hard_criteria_passed"])
        self.assertEqual(len(self.requests), 4)
        incoming = json.loads(json.loads(self.requests[-1].content)["messages"][1]["content"])
        self.assertEqual(incoming["auxiliary_inputs"]["items"], [
            {"kind": kind, "schema_version": body["schema_version"], "body": body}
            for kind, body in (("requirements", REQUIREMENTS), ("design", DESIGN), ("public-checks", PROPOSED_CHECKS))])
        self.assertEqual(len(self.sandbox.executions), 1)
        self.assertEqual(self.sandbox.executions[0]["tests_digest"], freeze_tests(self.acceptance)[1])
        self.assertEqual(self.sandbox.executions[0]["source_digest"], digest({"main.py": SOURCE}))
        self.assertEqual(self.sandbox.executions[0]["case_names"], [case.name for case in self.acceptance])
        self.assertEqual(len(self.sandbox.container_names), 2)
        bound = [event for event in self.events("execution.context_bound") if event["payload"]["task_id"] == claim.task_id]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["payload"]["context_item_count"], 3)
        self.assertEqual(self.events("development.evaluated"), [])
        self.assertIsNone(self.store.claim_next("handoff-live", self.run, "independent-reader", channel="verification"))
        self.assert_unchanged_contract_and_isolation()

    async def test_proposed_checks_cannot_replace_the_frozen_rebind_development_suite(self):
        claim = await self.prepare(cycle=True)
        proposed_digest = freeze_tests([FrozenTest(**case) for case in PROPOSED_CHECKS["cases"]])[1]
        self.assertNotEqual(proposed_digest, self.development_digest)
        result = await self.runner.run_python_cycle(claim, self.acceptance, HOST_PUBLIC, max_output_tokens=128)
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["verified"])
        self.assertTrue(result["development_passed"])
        self.assertEqual(result["repairs_applied"], 0)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([row["tests_digest"] for row in self.sandbox.executions],
                         [self.development_digest, freeze_tests(self.acceptance)[1]])
        self.assertEqual([row["case_names"] for row in self.sandbox.executions], [[HOST_PUBLIC[0].name], [self.acceptance[0].name]])
        self.assertEqual(len(self.sandbox.container_names), 2)
        self.assertEqual(self.events("development.evaluated")[0]["payload"]["development_digest"], self.development_digest)
        self.assertEqual(self.events("repair.applied"), [])
        self.assertIsNone(self.store.claim_next("handoff-live", self.run, "independent-reader", channel="verification"))
        self.assert_unchanged_contract_and_isolation()

    async def test_old_target_epoch_grants_stop_before_developer_http_or_any_container(self):
        claim = await self.prepare(stale_target_grants=True)
        before = len(self.requests)
        result = await self.runner.run_python(claim, self.acceptance, max_output_tokens=128)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["reason"], "CONTEXT_BINDING_INVALID")
        self.assertEqual(len(self.requests), before)
        self.assertEqual(before, 3)
        self.assertEqual(self.sandbox.executions, [])
        self.assertEqual(self.sandbox.container_names, [])
        for kind in ("tool.intent", "budget.reserved", "execution.context_bound", "execution.evaluated", "task.output_submitted"):
            self.assertFalse(any(event["payload"].get("task_id") == claim.task_id for event in self.events(kind)))
        self.assert_unchanged_contract_and_isolation()


if __name__ == "__main__":
    unittest.main()
