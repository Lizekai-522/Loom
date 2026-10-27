"""Local API/SQLite/runner contracts, mock provider and simulated sandbox only."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from loom import __version__
from loom.api import create_app
from loom.auth import LocalIdentity
from loom.execution import freeze_tests
from loom.model_gateway import ModelRoute
from loom.sandbox import SandboxUnavailable
from loom.store import InvalidInput, Store
from tests.test_execution import FakeSandbox


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workflow_task_script", ROOT / "scripts/run_workflow_task.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)
SENTINEL = "workflow-public-mock-key-only-00000000"
REQUIREMENTS = {"schema_version": "loom.requirements/v1", "summary": "Add one.",
                "items": [{"id": "add", "text": "Add one to value."}], "uncertainties": []}
DESIGN = {"schema_version": "loom.design/v1", "overview": "JSON transformer.",
          "components": [{"id": "transform", "responsibility": "Read JSON, add one, print JSON."}], "steps": [], "risks": []}
CHECKS = {"schema_version": "loom.public-checks/v1", "visibility": "development",
          "cases": [{"name": "proposed-deliberately-wrong", "input": {"value": 2}, "expected": {"answer": -999}}]}
SOURCE = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'


class WorkflowScriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.job = {"title": "A custom JSON transformation", "description": "Return answer equal to value plus one.",
                    "tests": [{"name": "host-private-case-marker", "input": {"value": 987654321}, "expected": {"answer": 987654322}}]}
        task_file = self.temp / "job.json"
        task_file.write_text(json.dumps(self.job), encoding="utf-8")
        self.args = SimpleNamespace(task_file=task_file, approve=False, db=str(self.temp / "absent.db"),
            tenant="first-round", base_url="http://127.0.0.1:8796", sandbox_image="sha256:" + "1" * 64,
            identities_file=self.temp / "absent-identities.json", secrets_file=self.temp / "absent-secrets.json",
            model_config=ROOT / "configs/models.example.json", max_output_tokens=1024)

    async def test_preview_has_no_private_database_network_or_docker_side_effects(self):
        with patch.object(script, "Store", side_effect=AssertionError("DB opened")), \
             patch.object(script, "load_identities", side_effect=AssertionError("private read")), \
             patch.object(script, "load_project_secrets", side_effect=AssertionError("private read")), \
             patch.object(script, "DockerSandbox", side_effect=AssertionError("Docker opened")), \
             patch.object(script, "ModelGateway", side_effect=AssertionError("provider opened")), \
             patch.object(script.httpx, "Client", side_effect=AssertionError("HTTP opened")):
            result = await script.run(self.args, [])
        self.assertEqual(result["mode"], "plan-preview")
        self.assertEqual(result["network_requests"], 0)
        self.assertEqual(result["maximum_model_requests"], 4)
        self.assertEqual(result["maximum_host_docker_cases"], 8)
        self.assertFalse(result["public_checks_executed"])
        self.assertFalse(Path(self.args.db).exists())
        self.assertNotIn("98765432", json.dumps(result))

    def test_host_tests_are_frozen_before_generation_and_review_keeps_all_parents(self):
        _, suite, definition = script.load_job(self.args.task_file)
        nodes = definition.model_dump(mode="json")["nodes"]
        self.assertEqual([node["role"] for node in nodes], ["analyst", "architect", "tester", "developer", "validator"])
        self.assertEqual([node["depends"] for node in nodes[:4]], [[], [], [], []])
        self.assertEqual(nodes[4]["depends"], ["spec", "design", "checks", "implement"])
        self.assertEqual([len(node.get("input_artifacts", [])) for node in nodes[:4]], [0, 1, 2, 3])
        self.assertEqual(nodes[3]["evaluation_digest"], freeze_tests(suite)[1])
        self.assertIsNone(nodes[3].get("repair"))
        self.assertEqual(sum(node["limits"]["cost_limit_microusd"] for node in nodes), script.GOAL_COST)
        self.assertEqual(sum(node["limits"]["token_limit"] for node in nodes), script.GOAL_TOKENS)
        self.assertTrue(all(node["limits"]["max_attempts"] == 1 for node in nodes))

    async def test_missing_database_stops_before_preflight_or_private_config(self):
        self.args.approve = True
        with patch.object(script, "DockerSandbox", side_effect=AssertionError("Docker opened")), \
             patch.object(script, "load_identities", side_effect=AssertionError("private read")):
            with self.assertRaises(script.CheckFailure) as caught:
                await script.run(self.args, [])
        self.assertEqual(caught.exception.code, "EXISTING_API_DATABASE_REQUIRED")

    async def test_capacity_mismatch_stops_before_preflight(self):
        self.args.approve = True
        Store(self.args.db, max_workers=5)
        with patch.object(script, "DockerSandbox", side_effect=AssertionError("Docker opened")):
            with self.assertRaises(ValueError):
                await script.run(self.args, [])

    async def test_unavailable_docker_stops_before_private_config_or_remote_draft(self):
        self.args.approve = True
        Store(self.args.db)
        with patch.object(script, "DockerSandbox", return_value=FakeSandbox(preflight_error=True)), \
             patch.object(script, "load_identities", side_effect=AssertionError("private read")), \
             patch.object(script.httpx, "Client", side_effect=AssertionError("HTTP opened")):
            with self.assertRaises(SandboxUnavailable):
                await script.run(self.args, [])

    def test_duplicate_job_fields_are_rejected(self):
        self.args.task_file.write_text('{"title":"one","title":"two"}', encoding="utf-8")
        with self.assertRaises(script.CheckFailure) as caught:
            script.load_job(self.args.task_file)
        self.assertEqual(caught.exception.code, "WORKFLOW_JOB_DUPLICATE_FIELD")

    def test_ninth_host_case_rejected_before_any_execution(self):
        self.job["tests"] = [{"name": f"case-{index}", "input": index, "expected": index} for index in range(9)]
        self.args.task_file.write_text(json.dumps(self.job), encoding="utf-8")
        with self.assertRaises(script.CheckFailure) as caught:
            script.load_job(self.args.task_file)
        self.assertEqual(caught.exception.code, "WORKFLOW_JOB_INVALID")

    def test_nonfinite_host_expected_is_rejected(self):
        self.job["tests"][0]["expected"] = float("nan")
        self.args.task_file.write_text(json.dumps(self.job), encoding="utf-8")
        with self.assertRaises(InvalidInput):
            script.load_job(self.args.task_file)

    def test_report_whitelist_omits_source_body_prompt_and_provider_id(self):
        result = script.safe_result({"status": "submitted", "content_hash": "a" * 64,
            "source": "PRIVATE_SOURCE", "artifact": {"body": "PRIVATE_BODY"}, "prompt": "PRIVATE_PROMPT",
            "provider_request_id": "PRIVATE_REQUEST", "reason": "PRIVATE reason text",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "key": SENTINEL},
            "producer_verified": False, "verified": False, "counts_as_success": False})
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn(SENTINEL, json.dumps(result))
        self.assertEqual(result["usage"]["total_tokens"], 30)

    def test_intent_without_response_is_uncertain(self):
        result = script.model_evidence([{"type": "tool.intent", "payload": {"call_id": "a" * 64}}])
        self.assertFalse(result["model_response_received"])
        self.assertEqual(result["model_dispatch_outcome"], "uncertain")

    async def execute(self, mode="success", *, wrong_database=False):
        """Four actual local runner invocations, mock I/O, no source execution."""
        self.args.approve = True
        store = Store(self.args.db)
        remote = Store(self.temp / "other.db") if wrong_database else store
        identities = tuple(LocalIdentity("first-round", role, frozenset({role}), "workflow-fixture-only-" + role + "-00000000")
                           for role in ("operator", "worker"))
        calls, api_calls = [], []
        sandbox = FakeSandbox(passed=mode != "host-failure")
        bodies = (REQUIREMENTS, DESIGN, CHECKS, {"source": SOURCE})
        def provider(request):
            calls.append(request)
            number = len(calls)
            if number > 4:
                raise AssertionError("unexpected retry or fifth request")
            if mode == "tester-timeout" and number == 3:
                raise httpx.ReadTimeout("public mock timeout", request=request)
            content = "invalid" if mode == "tester-invalid" and number == 3 else json.dumps(bodies[number - 1])
            return httpx.Response(200, json={"id": f"workflow-public-response-{number}", "object": "chat.completion",
                "created": 1792800000, "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}], "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35}})
        client_type, gateway_type = httpx.Client, script.ModelGateway
        with TestClient(create_app(remote, {}, identities=identities)) as client:
            def api(request):
                api_calls.append((request.method, request.url.path))
                response = client.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
                return httpx.Response(response.status_code, json=response.json())
            with patch.object(script, "DockerSandbox", return_value=sandbox), \
                 patch.object(script, "load_identities", return_value=identities), \
                 patch.object(script, "load_project_secrets", return_value={"DEEPSEEK_API_KEY": SENTINEL}), \
                 patch.object(script, "load_model_config", return_value=((ModelRoute("deepseek-flash"),), {})), \
                 patch.object(script, "ModelGateway", side_effect=lambda routes, **kw: gateway_type(routes, transport=httpx.MockTransport(provider), **kw)), \
                 patch.object(script.httpx, "Client", side_effect=lambda **kw: client_type(transport=httpx.MockTransport(api), **kw)):
                progress = {}
                if wrong_database:
                    with self.assertRaises(script.CheckFailure) as caught:
                        await script.run(self.args, [], progress)
                    self.assertEqual(caught.exception.code, "API_DATABASE_CONTRACT_MISMATCH")
                    result = progress
                else:
                    result = await script.run(self.args, [], progress)
        return result, store, calls, sandbox, api_calls

    async def test_full_handoff_has_six_exact_grants_and_never_executes_candidate_cases(self):
        result, store, calls, sandbox, _ = await self.execute()
        self.assertTrue(result["passed"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["model_dispatch_admissions"], 4)
        self.assertEqual(result["model_responses_received"], 4)
        self.assertEqual(result["accounted_tokens"], 140)
        self.assertEqual(result["remaining_cost_reservation_microusd"], 0)
        self.assertTrue(result["independent_review_blocked"])
        self.assertFalse(result["public_checks_executed"])
        self.assertFalse(result["counts_as_success"])
        self.assertFalse(result["full_workflow_completed"])
        self.assertEqual(sandbox.execution_calls, 1)
        self.assertEqual([case.name for case in sandbox.inputs[0][1]], ["host-private-case-marker"])
        chain = store.events(self.args.tenant, result["run_id"])
        grants = [event["payload"] for event in chain if event["type"] == "artifact.input_approved"]
        self.assertEqual(len(grants), 6)
        self.assertTrue(all(grant["target_epoch"] == grant["source_epoch"] == 1 for grant in grants))
        self.assertEqual([event["payload"]["context_item_count"] for event in chain if event["type"] == "execution.context_bound"], [1, 2, 3])
        states = {task["id"].split(":")[-1]: task["status"] for task in store.snapshot(self.args.tenant, result["run_id"])["tasks"]}
        self.assertEqual(states, dict(spec="submitted", design="submitted", checks="submitted", implement="submitted", independent_review="pending"))
        for index, request in enumerate(calls):
            messages = json.loads(request.content)["messages"]
            text = json.dumps(messages)
            self.assertNotIn("host-private-case-marker", text)
            self.assertNotIn("98765432", text)
            self.assertNotIn(SENTINEL, text)
            data = json.loads(messages[1]["content"])
            self.assertEqual(len(data.get("auxiliary_inputs", {}).get("items", [])), index)
        self.assertNotIn("proposed-deliberately-wrong", json.dumps(result))

    async def test_tester_unknown_stops_before_developer_and_preserves_reservation(self):
        result, _, calls, sandbox, _ = await self.execute("tester-timeout")
        self.assertFalse(result["passed"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["model_responses_received"], 2)
        self.assertEqual(result["model_dispatch_outcome"], "uncertain")
        self.assertGreater(result["remaining_cost_reservation_microusd"], 0)
        self.assertNotIn("implement", result["role_results"])
        self.assertEqual(sandbox.execution_calls, 0)
        self.assertFalse(result["independent_review_claim_attempted"])

    async def test_invalid_tester_body_keeps_known_usage_without_developer(self):
        result, _, calls, sandbox, _ = await self.execute("tester-invalid")
        self.assertFalse(result["passed"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["model_responses_received"], 3)
        self.assertEqual(result["remaining_cost_reservation_microusd"], 0)
        self.assertEqual(result["accounted_tokens"], 105)
        self.assertEqual(sandbox.execution_calls, 0)

    async def test_host_failure_is_not_overridden_by_candidate_check_or_retried(self):
        result, _, calls, sandbox, _ = await self.execute("host-failure")
        self.assertFalse(result["passed"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(sandbox.execution_calls, 1)
        self.assertFalse(result["frozen_acceptance_cases"][0]["passed"])
        self.assertFalse(result["independent_review_claim_attempted"])

    async def test_wrong_database_can_only_leave_one_remote_draft(self):
        result, _, calls, sandbox, api_calls = await self.execute(wrong_database=True)
        self.assertIn("run_id", result)
        self.assertEqual(api_calls, [("GET", "/health"), ("POST", "/v1/goals")])
        self.assertEqual(calls, [])
        self.assertEqual(sandbox.execution_calls, 0)
