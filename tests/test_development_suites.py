"""Durable review records never mutate plans, tasks, grants or execution rights."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from loom.artifacts import ArtifactService
from loom.development_suites import DevelopmentSuiteService
from loom.operator import OperatorService
from loom.store import Conflict, InvalidInput, NotFound, Store
from loom.workflow import WorkflowService


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "review.db")
    workflow = WorkflowService(store)
    definition = json.loads((Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
    definition["nodes"] = [{"id": "checks", "title": "Candidate checks", "instruction": "Data only",
        "role": "tester", "depends": [], "limits": {"cost_limit_microusd": 1000, "token_limit": 1000, "max_attempts": 1}}]
    goal = workflow.create_goal("a", "goal", title="Review", description="Development cases", acceptance=["tests.pass"])
    run = goal["run_id"]
    saved = workflow.save_plan("a", run, "plan", definition, 0)
    workflow.approve_plan("a", run, "approve", saved["revision"], saved["digest"])
    claim = store.claim_next("a", run, "tester", lease_seconds=3600)
    artifacts = ArtifactService(store)
    body = {"schema_version": "loom.public-checks/v1", "visibility": "development", "cases": [
        {"name": "first", "input": {"x": 9007199254740993}, "expected": True},
        {"name": "second", "input": None, "expected": [1, 1.0, False]}]}
    candidate = artifacts.save_candidate(claim, "candidate", "public-checks", body, actor="subject:tester")
    service = DevelopmentSuiteService(store)
    return store, workflow, artifacts, service, run, claim, candidate, body


def freeze(setup, key="freeze", names=None, **changes):
    _, _, _, service, run, _, candidate, _ = setup
    preview = service.preview("a", run, candidate["artifact_id"], selected_case_names=names)
    args = dict(actor="subject:operator", expected_review_digest=preview["review_digest"], selected_case_names=names)
    args.update(changes)
    return service.freeze("a", run, candidate["artifact_id"], key, **args)


def counts(store):
    with store.connect() as db:
        return tuple(db.execute("SELECT count(*) FROM "+name).fetchone()[0]
                     for name in ("development_suites", "events", "outbox", "receipts"))


def test_preview_is_read_only_and_freeze_only_records_explicit_development_review(setup):
    store, workflow, _, service, run, _, candidate, body = setup
    plan, state, before = workflow.get_plan("a", run), store.snapshot("a", run), counts(store)
    preview = service.preview("a", run, candidate["artifact_id"])
    assert counts(store) == before and preview["frozen"] is False
    result = freeze(setup)
    assert result["frozen"] is True and result["case_count"] == 2
    assert all(result[k] is False for k in ("execution_connected", "confers_authority", "producer_verified",
                                            "independently_verified", "counts_as_success"))
    assert workflow.get_plan("a", run) == plan
    after = store.snapshot("a", run)
    assert after == state
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM artifact_input_grants").fetchone()[0] == 0
    view = service.get_suite("a", run, result["suite_id"], include_body=True)
    assert view["body"] == body and view["stale"] is False
    assert "body" not in service.get_suite("a", run, result["suite_id"])
    assert view["source"]["origin"] == "authenticated-worker-submission"
    assert "model_provenance_digest" not in view["source"]


def test_subset_has_own_hash_and_exact_types_without_rewriting_original_candidate(setup):
    _, _, artifacts, service, run, _, candidate, body = setup
    result = freeze(setup, names=["second"])
    view = service.get_suite("a", run, result["suite_id"], include_body=True)
    assert view["source"]["body_hash"] == candidate["content_hash"]
    assert view["selected_body_hash"] != candidate["content_hash"]
    assert view["body"]["cases"] == [body["cases"][1]]
    assert type(view["body"]["cases"][0]["expected"][0]) is int
    assert type(view["body"]["cases"][0]["expected"][1]) is float
    assert artifacts.get_artifact("a", run, candidate["artifact_id"])["body"] == body


@pytest.mark.parametrize("names", [[], ["missing"], ["second", "first"], ["first", "first"], [True], "first", [1], [["first"]]])
def test_selection_must_be_unique_existing_names_in_source_order(setup, names):
    store, _, _, service, run, _, candidate, _ = setup
    before = counts(store)
    with pytest.raises(InvalidInput):
        service.preview("a", run, candidate["artifact_id"], selected_case_names=names)
    assert counts(store) == before


def test_exact_review_digest_and_actor_are_bound_to_idempotency(setup):
    store, _, _, service, run, _, candidate, _ = setup
    preview = service.preview("a", run, candidate["artifact_id"])
    result = freeze(setup)
    before = counts(store)
    assert freeze(setup) == result and counts(store) == before
    for args in (dict(actor="subject:other", expected_review_digest=preview["review_digest"]),
                 dict(actor="subject:operator", expected_review_digest="0"*64),
                 dict(actor="subject:operator", expected_review_digest=preview["review_digest"], selected_case_names=["first"])):
        with pytest.raises(Conflict):
            service.freeze("a", run, candidate["artifact_id"], "freeze", **args)
    assert counts(store) == before


def test_concurrent_same_request_creates_one_record_event_and_receipt(setup):
    store, _, _, service, run, _, candidate, _ = setup
    preview = service.preview("a", run, candidate["artifact_id"])
    before = counts(store)
    def invoke(_):
        return service.freeze("a", run, candidate["artifact_id"], "concurrent",
                              actor="subject:operator", expected_review_digest=preview["review_digest"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(8)))
    assert all(item == results[0] for item in results)
    assert tuple(a-b for a,b in zip(counts(store), before)) == (1, 1, 1, 1)


def test_insert_failure_rolls_back_event_outbox_record_and_receipt(setup):
    store, _, _, _, _, _, _, _ = setup
    before = counts(store)
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_suite BEFORE INSERT ON development_suites BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        freeze(setup)
    assert counts(store) == before


def test_storage_is_append_only_and_returned_body_is_detached(setup):
    store, _, _, service, run, _, _, _ = setup
    result = freeze(setup)
    before = service.get_suite("a", run, result["suite_id"], include_body=True)
    returned = service.get_suite("a", run, result["suite_id"], include_body=True)
    returned["body"]["cases"][1]["expected"].append("mutation")
    assert service.get_suite("a", run, result["suite_id"], include_body=True) == before
    for command in ("UPDATE development_suites SET record='{}'", "DELETE FROM development_suites"):
        with store.connect() as db, pytest.raises(sqlite3.IntegrityError):
            db.execute(command)


def test_new_revision_invalidates_old_preview_without_silent_latest_and_marks_old_suite_stale(setup):
    store, _, artifacts, service, run, claim, candidate, body = setup
    preview = service.preview("a", run, candidate["artifact_id"])
    result = freeze(setup)
    changed = deepcopy(body)
    changed["cases"][0]["expected"] = False
    artifacts.save_candidate(claim, "new-candidate", "public-checks", changed, actor="subject:tester")
    before = counts(store)
    with pytest.raises(Conflict):
        service.freeze("a", run, candidate["artifact_id"], "late", actor="subject:operator",
                       expected_review_digest=preview["review_digest"])
    view = service.get_suite("a", run, result["suite_id"], include_body=True)
    assert view["stale"] and "source_revision_superseded" in view["stale_reasons"]
    assert view["body"] == body and counts(store) == before
    # Idempotent historical replay adds no authorization or side effects.
    assert service.freeze("a", run, candidate["artifact_id"], "freeze", actor="subject:operator",
                          expected_review_digest=preview["review_digest"]) == result


def test_tenant_run_and_identity_boundaries(setup):
    store, _, _, service, run, _, candidate, _ = setup
    result = freeze(setup)
    before = counts(store)
    for tenant, run_id in (("b", run), ("a", "other-run")):
        with pytest.raises(NotFound):
            service.get_suite(tenant, run_id, result["suite_id"])
        with pytest.raises(NotFound):
            service.preview(tenant, run_id, candidate["artifact_id"])
    for actor in ("operator", "subject:", "", True):
        with pytest.raises(InvalidInput):
            freeze(setup, key="bad-actor", actor=actor)
    assert counts(store) == before


def test_compiler_change_or_wrong_review_digest_stops_before_writing(setup):
    store, _, _, service, run, _, candidate, _ = setup
    preview = service.preview("a", run, candidate["artifact_id"])
    before = counts(store)
    with patch("loom.public_checks.COMPILER_VERSION", "loom.public-checks-compiler/future"):
        with pytest.raises(Conflict):
            service.freeze("a", run, candidate["artifact_id"], "old-preview", actor="subject:operator",
                           expected_review_digest=preview["review_digest"])
    with pytest.raises(Conflict):
        freeze(setup, expected_review_digest="0"*64)
    assert counts(store) == before


def test_tampered_record_or_event_is_not_accepted_as_a_frozen_suite(setup):
    store, _, _, service, run, _, _, _ = setup
    result = freeze(setup)
    with store.connect() as db:
        db.execute("DROP TRIGGER development_suites_no_update")
        db.execute("UPDATE development_suites SET record='{}'")
    with pytest.raises(Conflict):
        service.get_suite("a", run, result["suite_id"])


def test_source_epoch_change_stops_new_freezes_but_keeps_stale_history(setup):
    store, _, _, service, run, claim, candidate, _ = setup
    preview = service.preview("a", run, candidate["artifact_id"])
    result = freeze(setup)
    store.invalidate("a", run, claim.task_id, "Reviewed source changed")
    before = counts(store)
    with pytest.raises(Conflict):
        service.freeze("a", run, candidate["artifact_id"], "old-epoch", actor="subject:operator",
                       expected_review_digest=preview["review_digest"])
    view = service.get_suite("a", run, result["suite_id"])
    assert view["stale"] and "source_epoch_changed" in view["stale_reasons"]
    assert view["source"]["epoch"] == claim.epoch and counts(store) == before


def test_paused_run_or_expired_source_lease_cannot_gain_new_freeze(setup):
    store, _, _, service, run, claim, candidate, _ = setup
    result = freeze(setup)
    OperatorService(store).pause("a", run, "pause", "subject:operator", "Pause review")
    before = counts(store)
    with pytest.raises(Conflict):
        service.preview("a", run, candidate["artifact_id"])
    assert "source_run_not_open" in service.get_suite("a", run, result["suite_id"])["stale_reasons"]
    with store.connect() as db:
        db.execute("UPDATE runs SET status='open' WHERE tenant=? AND id=?", ("a", run))
        db.execute("UPDATE tasks SET lease_until=0 WHERE tenant=? AND id=?", ("a", claim.task_id))
    with pytest.raises(Conflict):
        service.preview("a", run, candidate["artifact_id"])
    assert "source_lease_expired" in service.get_suite("a", run, result["suite_id"])["stale_reasons"]
    assert counts(store) == before


def test_restart_recovers_same_verified_record_and_unknown_suite_is_not_found(setup):
    store, _, _, service, run, _, _, _ = setup
    result = freeze(setup)
    before = service.get_suite("a", run, result["suite_id"], include_body=True)
    restarted = DevelopmentSuiteService(Store(store.path))
    assert restarted.get_suite("a", run, result["suite_id"], include_body=True) == before
    with pytest.raises(NotFound):
        restarted.get_suite("a", run, "unknown")
    with pytest.raises(InvalidInput):
        restarted.get_suite("a", run, result["suite_id"], include_body=1)


def test_tampered_freeze_event_is_rejected(setup):
    store, _, _, service, run, _, _, _ = setup
    result = freeze(setup)
    with store.connect() as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET envelope='{}' WHERE tenant=? AND hash=?",
                   ("a", result["freeze_event_hash"]))
    with pytest.raises(Conflict):
        service.get_suite("a", run, result["suite_id"])
