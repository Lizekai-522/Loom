"""Public sentinels and local MockTransport only; no private file/provider."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from loom import __version__
from loom.auth import LocalIdentity
from loom.api import create_app
from loom.model_gateway import ModelRoute
from loom.store import Store


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("role_task_script", ROOT / "scripts/run_role_tasks.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class RoleScriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.args = SimpleNamespace(task_file=ROOT / "configs/role-task.example.json", approve=False,
            db=str(self.temp / "missing.db"), identities_file=self.temp / "missing-identities.json",
            secrets_file=self.temp / "missing-secrets.json", model_config=ROOT / "configs/models.example.json",
            tenant="first-round", base_url="http://127.0.0.1:8796", max_output_tokens=1024)

    async def test_preview_only_reads_public_job_and_pack(self):
        with patch.object(script, "load_identities", side_effect=AssertionError("private read")), \
             patch.object(script, "load_project_secrets", side_effect=AssertionError("private read")), \
             patch.object(script, "Store", side_effect=AssertionError("DB opened")), \
             patch.object(script.httpx, "Client", side_effect=AssertionError("HTTP opened")), \
             patch.object(script, "make_role_runner", side_effect=AssertionError("runner created")):
            report = await script.run(self.args, [])
        self.assertEqual(report["mode"], "plan-preview")
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["maximum_model_requests"], 2)
        self.assertFalse(report["code_or_tool_execution"])
        nodes = report["definition"]["nodes"]
        self.assertEqual([node["id"] for node in nodes], ["spec", "design", "independent_review"])
        self.assertEqual([node["depends"] for node in nodes[:2]], [[], []])
        self.assertEqual(nodes[1]["input_artifacts"], [{"source_node": "spec", "kind": "requirements", "schema_version": "loom.requirements/v1", "required": True}])
        self.assertEqual(nodes[2]["depends"], ["spec", "design"])
        self.assertEqual([node["role_output"]["kind"] for node in nodes[:2]], ["requirements", "design"])
        self.assertEqual(sum(node["limits"]["cost_limit_microusd"] for node in nodes), report["goal_cost_limit_microusd"])
        self.assertEqual(sum(node["limits"]["token_limit"] for node in nodes), report["goal_token_limit"])
        self.assertFalse(Path(self.args.db).exists())

    async def test_custom_non_csv_goal_does_not_receive_unrelated_csv_requirements(self):
        job={"title":"Meeting room availability planning", "description":
             "Plan a read-only meeting-room availability service over user-provided time intervals. "
             "Identify overlaps, list free windows, and explain time-zone and interval-boundary assumptions."}
        self.args.task_file=self.temp/"meeting-room-job.json"
        self.args.task_file.write_text(json.dumps(job),encoding="utf-8")
        with patch.object(script,"Store",side_effect=AssertionError("DB opened")), \
             patch.object(script,"load_identities",side_effect=AssertionError("private read")), \
             patch.object(script,"load_project_secrets",side_effect=AssertionError("private read")), \
             patch.object(script.httpx,"Client",side_effect=AssertionError("HTTP opened")), \
             patch.object(script,"make_role_runner",side_effect=AssertionError("runner created")):
            report=await script.run(self.args,[])
        nodes=report["definition"]["nodes"]
        for node in nodes[:2]:
            self.assertIn(job["title"],node["title"])
            self.assertIn("Goal",node["instruction"])
        contract_text=json.dumps(report["definition"]).lower()
        for unrelated in ("csv audit","row errors","decimal totals","human export approval"):
            self.assertNotIn(unrelated,contract_text)
        self.assertEqual(report["network_requests"],0)
        self.assertEqual(report["maximum_model_requests"],2)
        self.assertFalse(Path(self.args.db).exists())

    async def test_missing_database_rejects_before_private_reads_or_http(self):
        self.args.approve = True
        with patch.object(script, "load_identities", side_effect=AssertionError("private read")), \
             patch.object(script.httpx, "Client", side_effect=AssertionError("HTTP opened")):
            with self.assertRaises(script.CheckFailure) as caught:
                await script.run(self.args, [])
        self.assertEqual(caught.exception.code, "EXISTING_API_DATABASE_REQUIRED")

    async def test_capacity_mismatch_rejects_before_private_reads_or_http(self):
        self.args.approve = True
        Store(self.args.db, max_workers=5)
        with patch.object(script, "load_identities", side_effect=AssertionError("private read")), \
             patch.object(script.httpx, "Client", side_effect=AssertionError("HTTP opened")):
            with self.assertRaises(ValueError):
                await script.run(self.args, [])

    async def test_wrong_database_leaves_only_remote_draft(self):
        self.args.approve = True
        Store(self.args.db)
        calls=[]
        run_id="e72c0cf8-1c7e-4445-bf88-4f9fe3577d0e"
        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/health":
                return httpx.Response(200, json={"version": __version__})
            if request.method == "POST" and request.url.path == "/v1/goals":
                return httpx.Response(201, json={"run_id": run_id})
            raise AssertionError("unexpected save/approve/claim")
        client_type = httpx.Client
        identities = tuple(LocalIdentity("first-round", role, frozenset({role}), "role-script-public-sentinel-" + role + "-000000") for role in ("operator", "worker"))
        with patch.object(script, "load_identities", return_value=identities), \
             patch.object(script, "load_project_secrets", return_value={"DEEPSEEK_API_KEY": "role-script-public-test-sentinel-only"}), \
             patch.object(script, "load_model_config", return_value=((ModelRoute("deepseek-flash"),), {})), \
             patch.object(script, "make_role_runner") as runner, \
             patch.object(script.httpx, "Client", side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw)):
            progress={}
            with self.assertRaises(script.CheckFailure) as caught:
                await script.run(self.args, [], progress)
        self.assertEqual(caught.exception.code, "API_DATABASE_CONTRACT_MISMATCH")
        self.assertEqual(progress, {"run_id": run_id})
        self.assertEqual(calls, [("GET", "/health"), ("POST", "/v1/goals")])
        runner.return_value.run_role.assert_not_called()

    async def test_pre_dispatch_stop_reports_no_model_and_stops_before_design(self):
        self.args.approve = True
        store = Store(self.args.db)
        identities = tuple(LocalIdentity("first-round", role, frozenset({role}), "role-script-stop-sentinel-" + role + "-000000") for role in ("operator", "worker"))
        called=[]
        class StoppedRunner:
            async def run_role(self, claim, **kwargs):
                called.append(claim.task_id)
                return {"task_id": claim.task_id, "status": "awaiting_human", "reason": "ROLE_INPUT_MISSING"}
        client_type = httpx.Client
        with TestClient(create_app(store, {}, identities=identities)) as test_client:
            def handler(request):
                response = test_client.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
                return httpx.Response(response.status_code, json=response.json())
            with patch.object(script, "load_identities", return_value=identities), \
                 patch.object(script, "load_project_secrets", return_value={"DEEPSEEK_API_KEY": "role-script-public-test-sentinel-only"}), \
                 patch.object(script, "load_model_config", return_value=((ModelRoute("deepseek-flash"),), {})), \
                 patch.object(script, "make_role_runner", return_value=StoppedRunner()), \
                 patch.object(script.httpx, "Client", side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw)):
                result = await script.run(self.args, [])
        self.assertFalse(result["passed"])
        self.assertFalse(result["real_model_used"])
        self.assertEqual(result["model_dispatch_admissions"], 0)
        self.assertEqual(result["model_responses_received"], 0)
        self.assertEqual(result["model_dispatch_outcome"], "not_started")
        self.assertEqual(len(called), 1)
        self.assertFalse(result["independent_review_claim_attempted"])

    def test_safe_report_does_not_copy_body_artifact_prompt_or_extra_usage(self):
        result=script.safe_role_result({"task_id":"run:spec", "status":"submitted", "artifact_id":"candidate-id",
            "content_hash":"a"*64, "artifact_content_hash":"b"*64, "model_call_id":"c"*64,
            "artifact":{"body":"PRIVATE_BODY"}, "source":"PRIVATE_SOURCE", "prompt":"PRIVATE_PROMPT",
            "usage":{"prompt_tokens":25,"completion_tokens":10,"total_tokens":35,"key":"PRIVATE_KEY"},
            "usage_cost_upper_microusd":20, "producer_verified":False,"verified":False,"counts_as_success":False})
        self.assertNotIn("PRIVATE_", str(result))
        self.assertEqual(result["usage"]["total_tokens"],35)
        self.assertFalse(result["verified"])

    def test_intent_without_done_is_uncertain_not_response_or_free(self):
        report=script.model_evidence([{"type":"tool.intent","payload":{"call_id":"a"*64}}])
        self.assertEqual(report["model_dispatch_admissions"],1)
        self.assertEqual(report["model_responses_received"],0)
        self.assertFalse(report["real_model_used"])
        self.assertEqual(report["model_dispatch_outcome"],"uncertain")

    def test_duplicate_job_field_is_rejected_before_pack_read(self):
        path=self.temp/"duplicate.json"
        path.write_text('{"title":"a","title":"b","description":"c"}',encoding="utf-8")
        with self.assertRaises(script.CheckFailure) as caught:
            script.load_job(path)
        self.assertEqual(caught.exception.code,"ROLE_JOB_DUPLICATE_FIELD")

    async def run_with_mock_provider(self, mode="success"):
        """Real local API/store/runner, public test sentinels, Mock HTTP only."""
        self.args.approve=True
        store=Store(self.args.db)
        identities=tuple(LocalIdentity("first-round",role,frozenset({role}),"role-cli-public-fixture-"+role+"-000000") for role in ("operator","worker"))
        provider_requests=[]
        def provider(request):
            provider_requests.append(request)
            number=len(provider_requests)
            if number>2:
                raise AssertionError("unexpected retry or third model request")
            if mode=="second-timeout" and number==2:
                raise httpx.ReadTimeout("public fixture timeout",request=request)
            body={"schema_version":"loom.requirements/v1","summary":"Plan a CSV audit.","items":[{"id":"rows","text":"Keep original row numbers and sum valid amounts."}],"uncertainties":[]}
            if number==2:
                body={"schema_version":"loom.design/v1","overview":"Plan only.","components":[{"id":"audit","responsibility":"Validate CSV rows without exporting."}],"steps":["Read user text conceptually.","Report row errors.","Sum valid decimals.","Require human export approval."],"risks":["Malformed CSV.","Decimal precision."]}
            content="not JSON" if mode=="first-invalid" and number==1 else json.dumps(body)
            return httpx.Response(200,json={"id":"public-role-cli-response-"+str(number),"object":"chat.completion","created":1792800000,"model":"deepseek-flash","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":content}}],"usage":{"prompt_tokens":25,"completion_tokens":10,"total_tokens":35}})
        client_type=httpx.Client
        gateway_type=script.ModelGateway
        with TestClient(create_app(store,{},identities=identities)) as test_client:
            def api(request):
                response=test_client.request(request.method,request.url.path,content=request.content,headers=dict(request.headers))
                return httpx.Response(response.status_code,json=response.json())
            with patch.object(script,"load_identities",return_value=identities), \
                 patch.object(script,"load_project_secrets",return_value={"DEEPSEEK_API_KEY":"role-cli-public-fixture-test-key-only"}), \
                 patch.object(script,"load_model_config",return_value=((ModelRoute("deepseek-flash"),),{})), \
                 patch.object(script,"ModelGateway",side_effect=lambda routes,**kw:gateway_type(routes,transport=httpx.MockTransport(provider),**kw)), \
                 patch.object(script.httpx,"Client",side_effect=lambda **kw:client_type(transport=httpx.MockTransport(api),**kw)):
                result=await script.run(self.args,[])
        return result,store,provider_requests

    async def test_two_real_role_paths_use_two_mock_requests_and_keep_review_blocked(self):
        result,store,requests=await self.run_with_mock_provider()
        self.assertTrue(result["passed"])
        self.assertEqual(len(requests),2)
        self.assertEqual(result["model_dispatch_admissions"],2)
        self.assertEqual(result["model_responses_received"],2)
        self.assertTrue(result["independent_review_blocked"])
        self.assertFalse(result["full_workflow_completed"])
        self.assertFalse(result["human_independent_approval_verified"])
        self.assertFalse(result["counts_as_success"])
        self.assertEqual(result["accounted_tokens"],70)
        self.assertEqual(result["remaining_cost_reservation_microusd"],0)
        self.assertNotIn("Plan a CSV audit",str(result))
        chain=store.events(self.args.tenant,result["run_id"])
        self.assertEqual(sum(event["type"]=="role.candidate_generated" for event in chain),2)
        grants=[event["payload"] for event in chain if event["type"]=="artifact.input_approved"]
        self.assertEqual(len(grants),1)
        self.assertEqual(grants[0]["target_epoch"],1)
        state=store.snapshot(self.args.tenant,result["run_id"])
        tasks={task["id"]:task for task in state["tasks"]}
        self.assertEqual([tasks[result["run_id"]+":"+node]["status"] for node in ("spec","design","independent_review")],["submitted","submitted","pending"])

    async def test_second_unknown_response_preserves_reservation_and_never_retries(self):
        result,store,requests=await self.run_with_mock_provider("second-timeout")
        self.assertFalse(result["passed"])
        self.assertEqual(len(requests),2)
        self.assertEqual(result["model_responses_received"],1)
        self.assertEqual(result["model_dispatch_admissions"],2)
        self.assertEqual(result["model_dispatch_outcome"],"uncertain")
        self.assertGreater(result["remaining_cost_reservation_microusd"],0)
        self.assertGreater(result["accounted_cost_upper_microusd"],0)
        self.assertFalse(result["independent_review_claim_attempted"])
        self.assertEqual(result["role_results"]["spec"]["status"],"submitted")

    async def test_first_invalid_typed_response_keeps_usage_and_never_starts_design(self):
        result,store,requests=await self.run_with_mock_provider("first-invalid")
        self.assertFalse(result["passed"])
        self.assertEqual(len(requests),1)
        self.assertEqual(result["model_responses_received"],1)
        self.assertEqual(result["model_dispatch_admissions"],1)
        self.assertGreater(result["accounted_cost_upper_microusd"],0)
        self.assertNotIn("design",result["role_results"])
        self.assertFalse(result["independent_review_claim_attempted"])
