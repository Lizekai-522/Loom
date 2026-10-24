"""Tester provenance never becomes trusted tests or completion evidence."""
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.role_summary import model_source_summary
from loom.store import canonical
import tests.test_tester_role_output as core_fixture


@pytest.fixture
def tester_source():
    case = core_fixture.TesterRoleOutputTests("runTest")
    case.setUp()
    try:
        case.prepare()
        case.body = {**case.body, "cases": [{"name": "untrusted", "input": "PRIVATE_CHECK_INPUT", "expected": "PRIVATE_EXPECTED"}]}
        case.result["content"] = canonical(case.body)
        case.known()
        saved = case.submit()
        identities = tuple(LocalIdentity(tenant, role, frozenset({"viewer"}), "tester-source-public-fixture-" + role + "-000000000")
                           for tenant, role in (("a", "reader"), ("b", "other")))
        headers = {item.subject_id: {"Authorization": "Bearer " + item.token} for item in identities}
        with TestClient(create_app(case.store, {}, identities=identities)) as client:
            yield case, client, saved["artifact"], headers
    finally:
        case.doCleanups()


def review(fixture):
    case, client, _, headers = fixture
    response = client.get(f"/v1/runs/{case.run}/review", headers=headers["reader"])
    assert response.status_code == 200 and "PRIVATE_" not in response.text
    task = next(item for item in response.json()["tasks"] if item["id"] == case.task_id)
    return response.json(), task


def test_tester_has_source_but_no_executed_or_passed_cases(tester_source):
    case, client, candidate, headers = tester_source
    before = case.counts(), case.store.events("a", case.run), case.developer_contract
    response, task = review(tester_source)
    artifact = task["artifact"]
    assert artifact["model_source_recorded"] is True and artifact["model_source_status"] == "recorded"
    source = artifact["model_provenance"]
    assert source["role"] == "tester" and source["kind"] == "public-checks" and source["public_checks_executed"] is False
    assert source["producer_verified"] is source["counts_as_success"] is False
    assert task["status"] == "submitted" and artifact["trusted_evaluation"] is False
    assert artifact["cases"] == [] and artifact["hard_criteria_passed"] is None
    history = client.get(f"/v1/runs/{case.run}/attempts", headers=headers["reader"])
    assert "PRIVATE_" not in history.text and history.json()["items"] == response["attempts"]
    assert history.json()["items"][0]["public_checks_executed"] is False
    assert case.counts() == before[0] and case.store.events("a", case.run) == before[1]
    assert case.workflow.get_task_spec("a", case.run, case.developer_id) == before[2]


def test_worker_passed_flags_do_not_become_trusted_tester_evidence(tester_source):
    case, _, _, _ = tester_source
    with case.store.transaction() as db:
        value = json.loads(db.execute("SELECT output FROM tasks WHERE id=?", (case.task_id,)).fetchone()[0])
        value.update(tests={"passed": True, "cases": [{"name": "PRIVATE_PASSED", "passed": True}]},
                     hard_criteria_passed=True, public_checks_executed=True, verified=True)
        db.execute("UPDATE tasks SET output=? WHERE id=?", (canonical(value), case.task_id))
    _, task = review(tester_source)
    assert task["artifact"]["trusted_evaluation"] is False and task["artifact"]["cases"] == []
    assert task["artifact"]["model_provenance"]["public_checks_executed"] is False


@pytest.mark.parametrize("mutation", [{"model_call_id": "0" * 64}, {"artifact_content_hash": "0" * 64}])
def test_tester_output_cannot_substitute_candidate_binding(tester_source, mutation):
    case, _, _, _ = tester_source
    with case.store.transaction() as db:
        value = json.loads(db.execute("SELECT output FROM tasks WHERE id=?", (case.task_id,)).fetchone()[0])
        value.update(mutation)
        db.execute("UPDATE tasks SET output=? WHERE id=?", (canonical(value), case.task_id))
    _, task = review(tester_source)
    assert task["artifact"]["model_source_recorded"] is False and "model_provenance" not in task["artifact"]


def test_tester_history_does_not_promote_stale_epoch(tester_source):
    case, _, _, _ = tester_source
    with case.store.transaction() as db:
        db.execute("UPDATE tasks SET epoch=epoch+1 WHERE id=?", (case.task_id,))
    body, task = review(tester_source)
    assert task["artifact"]["model_source_recorded"] is False
    assert body["attempts"][0]["epoch"] == 1 and body["attempts"][0]["public_checks_executed"] is False


def test_wrong_role_does_not_gain_tester_label(tester_source):
    candidate = deepcopy(tester_source[2])
    candidate["model_provenance"]["role"] = "architect"
    candidate["model_provenance"]["public_checks_executed"] = True
    assert model_source_summary(candidate) is None


def test_tester_provenance_is_tenant_scoped_read_only(tester_source):
    case, client, _, headers = tester_source
    for suffix in ("review", "attempts", "artifacts"):
        path = f"/v1/runs/{case.run}/{suffix}"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers["other"]).status_code == 404
        assert client.post(path, headers=headers["reader"], json={}).status_code == 405
