"""Safe binding summaries are read-only and never forward context contents."""
import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.store import Store


@pytest.fixture
def context_history(tmp_path):
    store = Store(tmp_path / "context-history.db")
    run = store.create_run("a", "goal", acceptance=["tests.pass"])["run_id"]
    store.add_task("a", run, "task", task_id="work", title="Safe binding history")
    token = "context-history-fixture-viewer-only-00000000"
    identities = (LocalIdentity("a", "viewer", frozenset({"viewer"}), token),)
    with TestClient(create_app(store, {}, identities=identities)) as client:
        yield client, store, run, {"Authorization": "Bearer " + token}


def test_context_binding_summary_strips_body_refs_and_private_payload(context_history):
    client, store, run, headers = context_history
    with store.transaction() as db:
        store._event(db, "a", run, "execution.context_bound", {
            "task_id": "work", "epoch": 1, "plan_digest": "1" * 64,
            "context_digest": "2" * 64, "context_bytes": 3200, "context_item_count": 2,
            "untrusted_data": True, "producer_verified": False, "counts_as_success": False,
            "body": "PRIVATE_BODY", "refs": [{"body": "PRIVATE_REFS"}], "worker_id": "PRIVATE_SUBJECT",
            "source": "PRIVATE_SOURCE", "expected": "PRIVATE_HIDDEN"}, "local-executor")
    before = store.events("a", run)
    response = client.get(f"/v1/runs/{run}/attempts", headers=headers)
    assert response.status_code == 200 and "PRIVATE_" not in response.text
    item = response.json()["items"][0]
    assert item["context_digest"] == "2" * 64 and item["context_bytes"] == 3200 and item["context_item_count"] == 2
    assert item["untrusted_data"] is True and item["producer_verified"] is item["counts_as_success"] is False
    review = client.get(f"/v1/runs/{run}/review", headers=headers)
    assert review.status_code == 200 and review.json()["attempts"] == [item]
    assert "PRIVATE_" not in review.text and store.events("a", run) == before


@pytest.mark.parametrize("bad", [True, -1, 65537, {"body": "PRIVATE_NESTED"}, "PRIVATE_STRING"])
def test_invalid_binding_scalars_never_escape(context_history, bad):
    client, store, run, headers = context_history
    with store.transaction() as db:
        store._event(db, "a", run, "execution.context_bound", {
            "task_id": "work", "context_digest": bad, "context_bytes": bad, "context_item_count": bad,
            "untrusted_data": "PRIVATE_STRING", "producer_verified": 0, "counts_as_success": []}, "local-executor")
    response = client.get(f"/v1/runs/{run}/attempts", headers=headers)
    assert "PRIVATE_" not in response.text
    item = response.json()["items"][0]
    assert all(key not in item for key in ("context_digest", "context_bytes", "context_item_count",
                                         "untrusted_data", "producer_verified", "counts_as_success"))


def test_untrusted_actor_cannot_inject_a_context_binding_summary(context_history):
    client, store, run, headers = context_history
    with store.transaction() as db:
        store._event(db, "a", run, "execution.context_bound", {"task_id": "work", "context_digest": "3" * 64}, "worker:fixture")
    assert client.get(f"/v1/runs/{run}/attempts", headers=headers).json()["items"] == []
