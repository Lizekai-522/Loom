"""Only named operators may freeze reviewed suites; no execution HTTP."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.artifacts import ArtifactService
from loom.auth import LocalIdentity
from loom.store import Store
from loom.workflow import WorkflowService


TOKENS = {role: "suite-api-public-fixture-only-"+role+"-0000000000000000"
          for role in ("operator", "worker", "viewer", "legacy", "other", "operator2")}


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "suite-api.db")
    workflow = WorkflowService(store)
    definition = json.loads((Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
    definition["nodes"] = [{"id": "checks", "title": "Public cases", "instruction": "Data only", "role": "tester",
        "depends": [], "limits": {"cost_limit_microusd": 1000, "token_limit": 1000, "max_attempts": 1}}]
    run = workflow.create_goal("a", "goal", title="Review cases", description="Only record review",
                               acceptance=["tests.pass"])["run_id"]
    saved = workflow.save_plan("a", run, "plan", definition, 0)
    workflow.approve_plan("a", run, "approve", saved["revision"], saved["digest"])
    claim = store.claim_next("a", run, "worker", lease_seconds=3600)
    body = {"schema_version": "loom.public-checks/v1", "visibility": "development", "cases": [
        {"name": "first", "input": 9007199254740993, "expected": True},
        {"name": "second", "input": False, "expected": [1.0, None]}]}
    artifacts = ArtifactService(store)
    candidate = artifacts.save_candidate(claim, "candidate", "public-checks", body, actor="subject:worker")
    identities = tuple(LocalIdentity("b" if role == "other" else "a", role,
                       frozenset({"viewer" if role == "other" else "operator" if role == "operator2" else role}), TOKENS[role])
                       for role in ("operator", "worker", "viewer", "other", "operator2"))
    with TestClient(create_app(store, {"a": TOKENS["legacy"]}, identities=identities)) as client:
        root = f"/v1/runs/{run}/artifacts/{candidate['artifact_id']}"
        def request(method, path, data=None, role="operator", key="freeze"):
            headers = {"Authorization": "Bearer "+TOKENS[role]} if role else {}
            if key is not None:
                headers["Idempotency-Key"] = key
            return client.request(method, path, json=data, headers=headers)
        yield store, workflow, artifacts, request, run, claim, candidate, body, root


def review(api, suffix=""):
    return api[3]("GET", api[8]+"/development-preview"+suffix).json()


def frozen(api, *, data=None, role="operator", key="freeze"):
    data = {"expected_review_digest": review(api)["review_digest"]} if data is None else data
    return api[3]("POST", api[8]+"/development-suites", data, role=role, key=key)


@pytest.mark.parametrize("role,code", [("worker",403), ("viewer",403), ("legacy",403), (None,401)])
def test_freeze_requires_named_operator_permission(api, role, code):
    store, _, _, _, _, _, _, _, _ = api
    before = store.snapshot("a", api[4])
    assert frozen(api, role=role).status_code == code
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM development_suites").fetchone()[0] == 0
    assert store.snapshot("a", api[4]) == before


@pytest.mark.parametrize("role", ["viewer", "worker", "operator", "legacy"])
def test_authenticated_read_preview_is_not_approval_and_has_no_body(api, role):
    store, _, _, request, run, _, _, _, root = api
    before = store.events("a", run)
    response = request("GET", root+"/development-preview", role=role)
    assert response.status_code == 200
    assert response.json()["frozen"] is False and response.json()["execution_connected"] is False
    assert "body" not in response.json() and "cases" not in response.json()
    assert store.events("a", run) == before


def test_operator_freeze_derives_actor_and_keeps_plan_task_and_budget(api):
    store, workflow, _, request, run, _, _, body, _ = api
    before, plan = store.snapshot("a", run), workflow.get_plan("a", run)
    response = frozen(api)
    assert response.status_code == 201
    result = response.json()
    assert result["actor"] == "subject:operator" and result["frozen"] is True
    assert all(result[k] is False for k in ("execution_connected","confers_authority","independently_verified","counts_as_success"))
    path = f"/v1/runs/{run}/development-suites/{result['suite_id']}"
    assert "body" not in request("GET", path, role="viewer").json()
    detail = request("GET", path+"?include_body=true", role="viewer")
    assert detail.status_code == 200 and detail.json()["body"] == body
    assert type(detail.json()["body"]["cases"][0]["input"]) is int
    assert detail.json()["body"]["cases"][0]["input"] == 9007199254740993
    assert store.snapshot("a", run) == before and workflow.get_plan("a", run) == plan


def test_subset_preview_and_exact_post_selection_are_bound(api):
    store, _, _, request, _, _, _, body, root = api
    selected = review(api, "?case=second")
    assert selected["selected_case_names"] == ["second"] and selected["case_count"] == 1
    mismatch = frozen(api, data={"expected_review_digest": selected["review_digest"]})
    assert mismatch.status_code == 409
    response = frozen(api, data={"expected_review_digest": selected["review_digest"], "selected_case_names":["second"]})
    assert response.status_code == 201 and response.json()["case_count"] == 1
    for query in ("?case=second&case=first", "?case=missing", "?case=first&case=first"):
        assert request("GET", root+"/development-preview"+query).status_code == 422


def test_idempotency_header_required_and_same_request_returns_original(api):
    assert frozen(api, key=None).status_code == 422
    first = frozen(api)
    assert first.status_code == 201 and frozen(api).json() == first.json()
    assert frozen(api, role="operator2").status_code == 409
    assert frozen(api, data={"expected_review_digest":"0"*64}).status_code == 409


@pytest.mark.parametrize("extra", [{"actor":"subject:operator2"}, {"tenant_id":"b"}, {"verified":True},
                                   {"execute":True}, {"acceptance":True}, {"body":{"secret-marker":True}}])
def test_untrusted_authority_or_body_fields_are_rejected(api, extra):
    response = frozen(api, data={"expected_review_digest":review(api)["review_digest"], **extra})
    assert response.status_code == 422 and "secret-marker" not in response.text


def test_other_tenant_cannot_read_preview_freeze_or_load_suite(api):
    _, _, _, request, run, _, _, _, root = api
    result = frozen(api).json()
    assert request("GET", root+"/development-preview", role="other").status_code == 404
    path = f"/v1/runs/{run}/development-suites/{result['suite_id']}"
    assert request("GET", path, role="other").status_code == 404
    assert request("POST", root+"/development-suites",
                   {"expected_review_digest":review(api)["review_digest"]}, role="other").status_code == 403


def test_old_preview_stops_after_new_source_revision(api):
    store, _, artifacts, request, _, claim, candidate, body, root = api
    original = review(api)
    body = {**body, "cases":[body["cases"][1]]}
    artifacts.save_candidate(claim, "new", "public-checks", body, actor="subject:worker")
    assert request("GET", root+"/development-preview").status_code == 409
    assert frozen(api, data={"expected_review_digest": original["review_digest"]}).status_code == 409
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM development_suites").fetchone()[0] == 0


def test_freeze_permission_is_visible_only_to_operator(api):
    request = api[3]
    for role in ("operator","worker","viewer","legacy"):
        capabilities = request("GET", "/v1/me", role=role).json()["capabilities"]
        assert ("suite.approve" in capabilities) is (role == "operator")
