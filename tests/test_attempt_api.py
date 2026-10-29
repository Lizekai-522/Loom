"""Attempt summaries preserve tenant ownership and exclude raw test/model data."""
import json

import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.store import Store


@pytest.fixture
def attempts(tmp_path):
    store = Store(tmp_path / "attempt-summary.db")
    created = store.create_run("a", "create-a", acceptance=["tests.pass"])
    run_id = created["run_id"]
    store.add_task("a", run_id, "add-a", task_id="work", title="Attempt fixture")
    with store.transaction() as db:
        for kind, payload in (
            ("execution.candidate", {"task_id": "work", "epoch": 1, "source_sha256": "1" * 64,
                                     "source": "RAW_SOURCE_SENTINEL", "input": "RAW_INPUT_SENTINEL"}),
            ("development.evaluated", {"task_id": "work", "epoch": 1, "passed": False,
                                       "cases": [{"stdout": "RAW_STDOUT_SENTINEL", "expected": "RAW_EXPECTED_SENTINEL"}]}),
            ("failure.classified", {"task_id": "work", "epoch": 1, "family": "other",
                                     "code": "DEVELOPMENT_TESTS_FAILED", "billing_uncertain": False}),
            ("repair.applied", {"task_id": "work", "from_epoch": 1, "to_epoch": 2, "primitive": "Rebind",
                                 "context": "RAW_FEEDBACK_SENTINEL", "future_field": "PRIVATE_FUTURE_SENTINEL"}),
        ):
            store._event(db, "a", run_id, kind, payload, "local-executor")
    tokens = {role: "local-fixture-" + role + "-no-remote-requests-00000000" for role in ("viewer", "worker", "operator", "other")}
    identities = tuple(LocalIdentity("b" if role == "other" else "a", role,
                                     frozenset({"viewer" if role == "other" else role}), token)
                       for role, token in tokens.items())
    with TestClient(create_app(store, {}, identities=identities)) as client:
        yield client, store, run_id, {role: {"Authorization": "Bearer " + token} for role, token in tokens.items()}


def test_summary_preserves_current_attempt_and_repair_provenance(attempts):
    client, _, run_id, headers = attempts
    response = client.get(f"/v1/runs/{run_id}/attempts", headers=headers["viewer"])
    assert response.status_code == 200
    body = response.json()
    assert [item["event_type"] for item in body["items"]] == [
        "execution.candidate", "development.evaluated", "failure.classified", "repair.applied"]
    assert body["items"][-1]["from_epoch"] == 1 and body["items"][-1]["to_epoch"] == 2
    assert all(len(item["event_hash"]) == 64 for item in body["items"])
    assert body["verification"] == "pending_configuration" and body["counts_as_success"] is False
    assert not any(word in response.text for word in ("RAW_", "PRIVATE_FUTURE_SENTINEL"))


def test_attempts_and_review_share_safe_summaries(attempts):
    client, _, run_id, headers = attempts
    history = client.get(f"/v1/runs/{run_id}/attempts", headers=headers["viewer"]).json()
    review = client.get(f"/v1/runs/{run_id}/review", headers=headers["viewer"])
    assert review.status_code == 200 and review.json()["attempts"] == history["items"]
    assert "RAW_" not in review.text


def test_no_unauthenticated_or_cross_tenant_attempt_access(attempts):
    client, _, run_id, headers = attempts
    assert client.get(f"/v1/runs/{run_id}/attempts").status_code == 401
    assert client.get(f"/v1/runs/{run_id}/attempts", headers=headers["other"]).status_code == 404
    assert client.get("/v1/runs/nonexistent/attempts", headers=headers["viewer"]).status_code == 404


def test_read_has_no_model_or_business_side_effects_and_cannot_write(attempts):
    client, store, run_id, headers = attempts
    before = store.events("a", run_id)
    for role in ("viewer", "worker", "operator"):
        assert client.get(f"/v1/runs/{run_id}/attempts", headers=headers[role]).status_code == 200
        assert client.post(f"/v1/runs/{run_id}/attempts", headers=headers[role], json={}).status_code == 405
    assert store.events("a", run_id) == before


def test_nested_future_payload_cannot_escape_scalar_allowlist(attempts):
    client, store, run_id, headers = attempts
    with store.transaction() as db:
        store._event(db, "a", run_id, "failure.classified", {"reason": {"secret": "NESTED_PRIVATE"},
                     "task_id": "work", "family": "network", "epoch": 2}, "local-executor")
    response = client.get(f"/v1/runs/{run_id}/attempts", headers=headers["viewer"])
    assert response.status_code == 200 and "NESTED_PRIVATE" not in response.text
    assert "reason" not in response.json()["items"][-1]


def test_host_diagnostics_are_trusted_bounded_and_do_not_leak_case_contents(attempts):
    client, store, run_id, headers = attempts
    valid = {"task_id": "work", "epoch": 1, "hard_criteria_passed": False,
             "host_cases_total": 3, "host_cases_passed": 1, "host_cases_mismatch": 0,
             "host_cases_timeout": 1, "host_cases_runtime_error": 1, "host_cases_other_failure": 0,
             "cases": [{"name": "HIDDEN_CASE", "stdout": "HIDDEN_OUTPUT"}]}
    with store.transaction() as db:
        store._event(db, "a", run_id, "execution.evaluated", valid, "local-executor")
        store._event(db, "a", run_id, "execution.evaluated", valid, "untrusted-worker")
        store._event(db, "a", run_id, "execution.evaluated", {**valid, "host_cases_timeout": True}, "local-executor")
        store._event(db, "a", run_id, "execution.evaluated", {**valid, "host_cases_total": 4}, "local-executor")
        store._event(db, "a", run_id, "execution.evaluated", {**valid, "hard_criteria_passed": True}, "local-executor")
    response = client.get(f"/v1/runs/{run_id}/attempts", headers=headers["viewer"])
    assert response.status_code == 200
    rows = response.json()["items"][-5:]
    assert rows[0]["host_cases_total"] == 3 and rows[0]["host_cases_timeout"] == 1
    assert all("host_cases_total" not in row for row in rows[1:])
    assert "HIDDEN_CASE" not in response.text and "HIDDEN_OUTPUT" not in response.text
