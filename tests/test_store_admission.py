"""Budget and dispatch intent must be all-or-nothing across pause/failures."""
from unittest.mock import patch

import pytest

from loom.operator import OperatorService
from loom.store import BudgetExceeded, Conflict, Store


@pytest.fixture
def admission(tmp_path):
    store = Store(tmp_path / "admission.db")
    run_id = store.create_run("a", "create", acceptance=["tests.pass"], cost_limit=1000, token_limit=1000)["run_id"]
    store.add_task("a", run_id, "add", task_id="work", title="Atomic admission")
    claim = store.claim_next("a", run_id, "worker", lease_seconds=300)
    return store, run_id, claim


def admit(store, claim, reservation="budget", operation="model-generation:1", request=None):
    return store.reserve_and_begin_effect(claim, reservation, max_cost=100, max_tokens=200,
                                         operation_id=operation, request=request or {"prompt_digest": "example"})


def test_atomic_admission_replay_never_reauthorizes_dispatch(admission):
    store, run_id, claim = admission
    first = admit(store, claim)
    second = admit(store, claim)
    assert first["dispatch"] is True and second["dispatch"] is False
    assert first["call_id"] == second["call_id"]
    snapshot = store.snapshot("a", run_id)
    assert snapshot["reserved"] == 100 and snapshot["tokens_reserved"] == 200
    kinds = [event["type"] for event in store.events("a", run_id)]
    assert kinds.count("budget.reserved") == kinds.count("tool.intent") == 1
    assert store.replay("a", run_id)["reserved"] == 100


def test_intent_event_failure_rolls_back_reservation_projection_event_and_outbox(admission):
    store, run_id, claim = admission
    before = store.events("a", run_id)
    original = store._event
    def fail_intent(db, tenant, run, kind, *args):
        if kind == "tool.intent":
            raise RuntimeError("injected intent durability failure")
        return original(db, tenant, run, kind, *args)
    with patch.object(store, "_event", fail_intent), pytest.raises(RuntimeError):
        admit(store, claim)
    assert store.events("a", run_id) == before
    assert store.snapshot("a", run_id)["reserved"] == 0
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == len(before)


def test_pause_before_admission_creates_neither_hold_nor_effect(admission):
    store, run_id, claim = admission
    OperatorService(store).pause("a", run_id, "pause", "subject:operator", "Pause before admission")
    with pytest.raises(Conflict):
        admit(store, claim)
    assert store.snapshot("a", run_id)["reserved"] == 0
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 0


def test_pause_after_admission_has_a_reconcilable_effect_not_an_orphan_hold(admission):
    store, run_id, claim = admission
    grant = admit(store, claim)
    OperatorService(store).pause("a", run_id, "pause", "subject:operator", "Pause after atomic intent")
    assert store.snapshot("a", run_id)["reserved"] == 100
    with store.connect() as db:
        row = db.execute("SELECT state FROM effects WHERE id=?", (grant["call_id"],)).fetchone()
        assert row["state"] == "intent"
    with pytest.raises(Conflict):
        admit(store, claim)


def test_known_effect_cannot_create_an_unrelated_orphan_reservation(admission):
    store, run_id, claim = admission
    store.begin_effect(claim, "model-generation:1", {"prompt_digest": "example"})
    with pytest.raises(Conflict):
        admit(store, claim)
    assert store.snapshot("a", run_id)["reserved"] == 0
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_settled_reservation_cannot_fund_a_fresh_intent(admission):
    store, run_id, claim = admission
    store.reserve(claim, "budget", max_cost=100, max_tokens=200)
    store.settle("a", "budget", actual_cost=1, actual_tokens=2)
    with pytest.raises(Conflict):
        admit(store, claim)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 0
    assert store.snapshot("a", run_id)["spent"] == 1


def test_budget_failure_cannot_leave_an_intent(admission):
    store, _, claim = admission
    with pytest.raises(BudgetExceeded):
        store.reserve_and_begin_effect(claim, "too-large", max_cost=1001, max_tokens=1,
                                       operation_id="model-generation:1", request={})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 0
