"""Model source labels require core evidence and exact output bindings."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.artifacts import ArtifactService
from loom.auth import LocalIdentity
from loom.model_gateway import ModelResult, ModelUsage
from loom.role_summary import model_source_summary
from loom.store import Store, canonical, digest
from loom.workflow import WorkflowService

PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
BODY = {"schema_version": "loom.requirements/v1", "summary": "PRIVATE_ROLE_BODY_SENTINEL",
        "items": [{"id": "scope", "text": "PRIVATE_ITEM_SENTINEL"}], "uncertainties": []}


@pytest.fixture
def role_api(tmp_path):
    store = Store(tmp_path / "role-source.db")
    workflow = WorkflowService(store)
    artifacts = ArtifactService(store)
    plan = json.loads(PACK.read_text(encoding="utf-8"))
    source = deepcopy(plan["nodes"][0])
    source.update(id="generate", depends=[], priority=90,
        role_output={"kind": "requirements", "schema_version": "loom.requirements/v1"})
    review = deepcopy(plan["nodes"][-1])
    review.update(id="review", depends=["generate"])
    plan["nodes"] = [source, review]
    run = workflow.create_goal("a", "goal", title="Read-only model source",
        description="Private goal sentinel", acceptance=["schema.valid"])["run_id"]
    saved = workflow.save_plan("a", run, "plan", plan, 0)
    workflow.approve_plan("a", run, "approve", saved["revision"], saved["digest"])
    claim = store.claim_next("a", run, "fixture-worker", lease_seconds=3600)
    request = {"route": "deepseek-flash", "provider": "deepseek", "model": "deepseek-flash",
        "prompt_digest": digest(["PRIVATE_PROMPT_SENTINEL"]), "max_output_tokens": 128,
        "plan_digest": saved["digest"], "role_output": source["role_output"], "response_contract": "role-artifact/v1"}
    reservation = f"python:{claim.task_id}:{claim.epoch}"
    admitted = artifacts.admit_execution_effect(claim, None, reservation_id=reservation,
        max_cost=500, max_tokens=100, operation_id=f"model-generation:{claim.epoch}", request=request)
    result = ModelResult(canonical(BODY), "deepseek-flash", "deepseek", "deepseek-flash", ModelUsage(12, 8, 20),
                        "PRIVATE_PROVIDER_REQUEST_SENTINEL", "stop", "deepseek-flash")
    store.finish_effect(claim, admitted["call_id"], asdict(result))
    store.settle("a", reservation, actual_cost=14, actual_tokens=20)
    submitted = artifacts.submit_role_output(claim, "submit-role", call_id=admitted["call_id"],
        reservation_id=reservation, request=request, snapshot=None)
    tokens = {role: "role-source-public-fixture-" + role + "-000000000" for role in ("viewer", "worker", "other")}
    identities = tuple(LocalIdentity("b" if role == "other" else "a", role,
        frozenset({"viewer" if role == "other" else role}), token) for role, token in tokens.items())
    headers = {role: {"Authorization": "Bearer " + token} for role, token in tokens.items()}
    with TestClient(create_app(store, {}, identities=identities)) as client:
        yield client, store, artifacts, run, claim, submitted["artifact"], headers


def get_review(fixture):
    client, _, _, run, claim, _, headers = fixture
    response = client.get(f"/v1/runs/{run}/review", headers=headers["viewer"])
    assert response.status_code == 200
    task = next(item for item in response.json()["tasks"] if item["id"] == claim.task_id)
    return response, task


def test_review_and_attempts_show_recorded_source_without_verifying_content(role_api):
    client, store, _, run, _, candidate, headers = role_api
    before = store.events("a", run)
    response, task = get_review(role_api)
    artifact = task["artifact"]
    assert task["role_output"] == {"kind": "requirements", "schema_version": "loom.requirements/v1"}
    assert artifact["model_source_recorded"] is True and artifact["model_source_status"] == "recorded"
    assert artifact["trusted_evaluation"] is False and artifact["hard_criteria_passed"] is None
    assert artifact["verification"] == "awaiting_independent_models" and task["status"] == "submitted"
    source = artifact["model_provenance"]
    assert source["artifact_id"] == candidate["artifact_id"] and source["total_tokens"] == 20
    assert source["producer_verified"] is source["counts_as_success"] is False
    history = client.get(f"/v1/runs/{run}/attempts", headers=headers["viewer"])
    item = history.json()["items"][0]
    assert item["event_type"] == "role.candidate_generated" and item["call_id"] == source["call_id"]
    assert response.json()["attempts"] == history.json()["items"]
    assert "PRIVATE_" not in response.text + history.text and store.events("a", run) == before
    assert store.claim_next("a", run, "reviewer", channel="verification") is None


@pytest.mark.parametrize("field,value", [("artifact_id", "0" * 36), ("artifact_content_hash", "0" * 64),
    ("artifact_kind", "design"), ("artifact_schema_version", "loom.design/v1"), ("model_call_id", "0" * 64),
    ("plan_digest", "0" * 64)])
def test_worker_references_cannot_substitute_recorded_source(role_api, field, value):
    _, store, _, _, claim, _, _ = role_api
    with store.transaction() as db:
        output = json.loads(db.execute("SELECT output FROM tasks WHERE id=?", (claim.task_id,)).fetchone()[0])
        output.update({field: value, "model_source_recorded": True, "producer_verified": True,
                       "model_provenance": {"private": "PRIVATE_WORKER_CLAIM_SENTINEL"}})
        db.execute("UPDATE tasks SET output=? WHERE id=?", (canonical(output), claim.task_id))
    response, task = get_review(role_api)
    assert task["artifact"]["model_source_recorded"] is False
    assert "model_provenance" not in task["artifact"] and "PRIVATE_" not in response.text


def test_stale_candidate_remains_history_only(role_api):
    _, store, _, _, claim, _, _ = role_api
    with store.transaction() as db:
        db.execute("UPDATE tasks SET epoch=epoch+1 WHERE id=?", (claim.task_id,))
    response, task = get_review(role_api)
    assert task["artifact"]["model_source_recorded"] is False
    assert task["artifact"]["model_source_status"] == "version_mismatch"
    assert any(item["event_type"] == "role.candidate_generated" and item["epoch"] == claim.epoch
               for item in response.json()["attempts"])


def test_replayed_generated_event_cannot_copy_a_real_candidate_source(role_api):
    client, store, _, run, _, _, headers = role_api
    event = next(event for event in store.events("a", run) if event["type"] == "role.candidate_generated")
    with store.transaction() as db:
        for actor in ("local-executor", "subject:fixture-worker"):
            store._event(db, "a", run, event["type"], event["payload"], actor)
    history = client.get(f"/v1/runs/{run}/attempts", headers=headers["viewer"]).json()["items"]
    assert len(history) == 1 and history[0]["event_hash"] == event["hash"]


def test_missing_gateway_proof_never_yields_a_recorded_label(role_api):
    _, store, _, _, _, _, _ = role_api
    with store.transaction() as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET hash=? WHERE json_extract(envelope,'$.type')='tool.done'", ("0" * 64,))
    response, task = get_review(role_api)
    assert task["artifact"]["model_source_recorded"] is False and response.json()["attempts"] == []


def test_model_source_reads_enforce_permissions_and_have_no_write_route(role_api):
    client, store, _, run, _, _, headers = role_api
    before = store.events("a", run)
    for suffix in ("review", "attempts", "artifacts"):
        path = f"/v1/runs/{run}/{suffix}"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers["other"]).status_code == 404
        assert client.get(path, headers=headers["worker"]).status_code == 200
        assert client.post(path, headers=headers["viewer"], json={}).status_code == 405
    assert store.events("a", run) == before


@pytest.mark.parametrize("change", [ {"kind": []}, {"source_epoch": True}, {"artifact_id": "PRIVATE_ID"},
    {"model_provenance": {"usage": {"total_tokens": "PRIVATE_USAGE"}}}])
def test_unsafe_candidate_scalars_do_not_enter_summary(role_api, change):
    candidate = deepcopy(role_api[5])
    candidate.update(change)
    assert model_source_summary(candidate) is None
