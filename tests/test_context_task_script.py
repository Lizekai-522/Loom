"""Preview and local DB mismatch checks never use a provider or private file."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from loom import __version__
from loom.api import create_app
from loom.auth import LocalIdentity
from loom.model_gateway import ModelRoute
from loom.store import Store


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("context_task_script", ROOT / "scripts/run_context_task.py")
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class ContextScriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.args = SimpleNamespace(task_file=ROOT / "configs/python-context-task.example.json", approve=False,
            db=str(self.temp / "missing.db"), identities_file=self.temp / "missing-identities.json",
            secrets_file=self.temp / "missing-secrets.json", model_config=ROOT / "configs/models.example.json",
            tenant="first-round", sandbox_image="sha256:" + "1" * 64, base_url="http://127.0.0.1:8796", max_output_tokens=512)

    async def test_preview_never_reads_credentials_database_or_opens_http_or_docker(self):
        with patch.object(script, "load_identities", side_effect=AssertionError("private read")), \
             patch.object(script, "load_project_secrets", side_effect=AssertionError("private read")), \
             patch.object(script, "Store", side_effect=AssertionError("DB touched")), \
             patch.object(script.httpx, "Client", side_effect=AssertionError("HTTP touched")), \
             patch.object(script, "DockerSandbox", side_effect=AssertionError("Docker touched")):
            report = await script.run(self.args, [])
        assert report["mode"] == "plan-preview" and report["network_requests"] == 0
        work = report["definition"]["nodes"][1]
        assert work["depends"] == [] and work["input_artifacts"][0]["required"] is True
        assert report["definition"]["nodes"][2]["depends"] == ["implement"]
        assert not Path(self.args.db).exists()

    async def test_missing_database_rejects_before_private_read_or_http_write(self):
        self.args.approve = True
        with patch.object(script, "load_identities", side_effect=AssertionError("private read")), \
             patch.object(script.httpx, "Client", side_effect=AssertionError("HTTP touched")):
            with self.assertRaises(script.CheckFailure) as caught:
                await script.run(self.args, [])
        assert caught.exception.code == "EXISTING_API_DATABASE_REQUIRED"

    async def test_wrong_existing_database_leaves_only_draft_no_claim_or_model(self):
        self.args.approve = True
        Store(self.args.db)
        calls = []
        run = "a833a205-045f-4553-bc0f-09ba46bb86b2"
        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/health":
                return httpx.Response(200, json={"version": __version__})
            if request.method == "POST" and request.url.path == "/v1/goals":
                return httpx.Response(201, json={"run_id": run})
            raise AssertionError("unexpected approval/claim/network call")
        client_type = httpx.Client
        identities = tuple(LocalIdentity("first-round", role, frozenset({role}), "context-script-fixture-" + role + "-00000000")
                           for role in ("operator", "worker"))
        with patch.object(script, "load_identities", return_value=identities), \
             patch.object(script, "load_project_secrets", return_value={"DEEPSEEK_API_KEY": "context-script-test-key-only"}), \
             patch.object(script, "load_model_config", return_value=((ModelRoute("deepseek-flash"),), {})), \
             patch.object(script, "DockerSandbox") as sandbox, \
             patch.object(script.httpx, "Client", side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs)), \
             patch.object(script.ModelGateway, "generate", side_effect=AssertionError("provider called")):
            sandbox.return_value.preflight.return_value = {}
            progress = {}
            with self.assertRaises(script.CheckFailure) as caught:
                await script.run(self.args, [], progress)
        assert caught.exception.code == "API_DATABASE_CONTRACT_MISMATCH"
        assert progress == {"run_id": run}
        assert calls == [("GET", "/health"), ("POST", "/v1/goals")]

    async def test_pre_dispatch_stop_is_not_reported_as_real_model_use(self):
        self.args.approve = True
        store = Store(self.args.db)
        identities = tuple(LocalIdentity("first-round", role, frozenset({role}), "context-script-stop-fixture-" + role + "-00000000")
                           for role in ("operator", "worker"))
        client_type = httpx.Client
        async def stopped(runner, claim, *args, **kwargs):
            return runner._block(claim, "CONTEXT_BINDING_INVALID")
        with TestClient(create_app(store, {}, identities=identities)) as test_client:
            def handler(request):
                response = test_client.request(request.method, request.url.path, content=request.content,
                                               headers=dict(request.headers))
                return httpx.Response(response.status_code, json=response.json())
            with patch.object(script, "load_identities", return_value=identities), \
                 patch.object(script, "load_project_secrets", return_value={"DEEPSEEK_API_KEY": "context-script-test-key-only"}), \
                 patch.object(script, "load_model_config", return_value=((ModelRoute("deepseek-flash"),), {})), \
                 patch.object(script, "DockerSandbox") as sandbox, \
                 patch.object(script.httpx, "Client", side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs)), \
                 patch.object(script.ModelGateway, "generate", side_effect=AssertionError("provider called")), \
                 patch.object(script.LocalTaskRunner, "run_python", new=stopped):
                sandbox.return_value.preflight.return_value = {}
                result = await script.run(self.args, [])
        assert result["passed"] is False and result["execution"]["status"] == "awaiting_human"
        assert result["real_model_used"] is result["model_response_received"] is False
        assert result["model_dispatch_admissions"] == result["model_responses_received"] == 0
        assert result["model_dispatch_outcome"] == "not_started"
