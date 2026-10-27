from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from loom.api import create_app
from loom.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "api.db")
        self.client = TestClient(create_app(self.store, {"tenant-a": "local-test-token-a-123", "tenant-b": "local-test-token-b-123"}))
        self.a = {"Authorization": "Bearer local-test-token-a-123", "Idempotency-Key": "create"}
        self.b = {"Authorization": "Bearer local-test-token-b-123", "Idempotency-Key": "create"}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def create_run(self):
        response = self.client.post("/v1/runs", headers=self.a, json={"acceptance": ["tests.pass"]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["run_id"]

    def test_openapi_and_health_are_usable(self):
        self.assertEqual(self.client.get("/health").json()["stage"], "S1-local")
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/v1/runs", schema["paths"])
        params = schema["paths"]["/v1/runs"]["post"]["parameters"]
        self.assertTrue(any(p["name"] == "Idempotency-Key" and p["in"] == "header" for p in params))

    def test_unauthenticated_commands_and_tenant_override_are_denied(self):
        self.assertEqual(self.client.post("/v1/runs", json={"acceptance": ["tests.pass"]}).status_code, 401)
        self.assertEqual(self.client.post("/v1/runs", headers=self.a, json={"acceptance": ["tests.pass"], "tenant_id": "tenant-b"}).status_code, 422)
        self.assertEqual(self.client.post("/v1/runs", headers=self.a, json={"acceptance": ["invented-predicate"]}).status_code, 422)

    def test_tenant_bound_reads_do_not_disclose_other_runs(self):
        run = self.create_run()
        for suffix in ("", "/events", "/replay", "/integrity"):
            self.assertEqual(self.client.get("/v1/runs/" + run + suffix, headers=self.b).status_code, 404)

    def test_idempotency_and_argument_binding(self):
        run = self.create_run()
        self.assertEqual(self.create_run(), run)
        response = self.client.post("/v1/runs", headers=self.a, json={"acceptance": ["schema.valid"]})
        self.assertEqual(response.status_code, 409)

    def test_strict_budgets_and_registered_ruleset(self):
        for value in (True, "100"):
            response = self.client.post("/v1/runs", headers=self.a, json={"acceptance": ["tests.pass"], "token_limit": value})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.post("/v1/runs", headers=self.a, json={"acceptance": ["tests.pass"], "ruleset": "invented"}).status_code, 422)

    def test_nested_nonfinite_artifact_has_safe_validation_response(self):
        response = self.client.post("/v1/runs/unused/tasks/unused/submit", headers=self.a,
                                    content='{"worker_id":"worker","epoch":1,"output":{"x":NaN}}',
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "INVALID_INPUT")
        self.assertNotIn('"input"', response.text)

    def test_worker_submission_does_not_bypass_verification_or_release_child(self):
        run = self.create_run()
        for task_id, depends in (("parent", []), ("child", ["parent"])):
            response = self.client.post(f"/v1/runs/{run}/tasks", headers={**self.a, "Idempotency-Key": task_id},
                                        json={"task_id": task_id, "title": task_id, "depends_on": depends})
            self.assertEqual(response.status_code, 201, response.text)
        claim = self.client.post(f"/v1/runs/{run}/claim", headers=self.a, json={"worker_id": "worker"}).json()["claim"]
        self.assertEqual(claim["task_id"], "parent")
        response = self.client.post(f"/v1/runs/{run}/tasks/parent/submit", headers={**self.a, "Idempotency-Key": "submit"},
                                    json={"worker_id": "worker", "epoch": claim["epoch"], "output": {"verified": True, "tests_pass": True}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "submitted")
        self.assertIsNone(self.client.post(f"/v1/runs/{run}/claim", headers=self.a, json={"worker_id": "second"}).json()["claim"])
        state = self.client.get(f"/v1/runs/{run}/replay", headers=self.a).json()
        self.assertEqual(state["tasks"]["parent"]["status"], "submitted")
        self.assertEqual(state["tasks"]["child"]["status"], "pending")
        self.assertEqual(self.client.post(f"/v1/runs/{run}/tasks/parent/complete", headers=self.a, json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
