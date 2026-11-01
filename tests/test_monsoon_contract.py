"""Loom-side batch-3 contract (monsoon-harness plan v2.2): fixture credentials, SQLite only.

Covers the acceptance criteria A2/A3 (cursor), B1/B4/B5/G6 (human-only approval),
D1/D2 (template parameter contract), E3 (requester attribution), F3 (legacy keys),
G1 (effect registration prevents replay) and the 6.2 execution-domain policy.
"""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.auth import AuthConfigError, LocalIdentity
from loom.domain_packs import DomainPackService, PackTemplate
from loom.store import Store

PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
TOKENS = {name: f"monsoon-contract-{name}-fixture-token-0000000000" for name in ("user", "operator", "worker", "viewer", "legacy")}
IDENTITIES = (LocalIdentity("a", "alice", frozenset({"user"}), TOKENS["user"]),
              LocalIdentity("a", "ms1:operator", frozenset({"operator"}), TOKENS["operator"]),
              LocalIdentity("a", "ms1:worker", frozenset({"worker"}), TOKENS["worker"]),
              LocalIdentity("a", "viewer", frozenset({"viewer"}), TOKENS["viewer"]))


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "contract.db")
    with TestClient(create_app(store, {"a": TOKENS["legacy"]}, identities=IDENTITIES, legacy_developer_keys=True)) as client:
        def call(method, path, body=None, *, who="operator", key=None):
            headers = {"Authorization": "Bearer " + TOKENS[who]}
            if key:
                headers["Idempotency-Key"] = key
            return client.request(method, path, json=body, headers=headers)
        yield store, client, call


def goal(call, description="合同测试目标", key="goal"):
    return call("POST", "/v1/goals", {"title": "合同", "description": description, "acceptance": ["tests.pass"]}, key=key)


def saved_plan(call, key="g"):
    run = goal(call, key=key).json()["run_id"]
    plan = call("PUT", f"/v1/goals/{run}/plan", {"definition": json.loads(PACK.read_text(encoding="utf-8")), "expected_revision": 0},
                key=key + "-plan").json()
    return run, {"expected_revision": plan["revision"], "expected_digest": plan["digest"]}


def test_legacy_keys_are_refused_without_the_explicit_transition_switch(tmp_path):
    with pytest.raises(AuthConfigError):
        create_app(Store(tmp_path / "x.db"), {"a": TOKENS["legacy"]}, identities=IDENTITIES)


def test_only_the_named_human_approves_and_the_event_names_them(api):
    store, _, call = api
    run, approval = saved_plan(call)
    for who in ("operator", "worker", "viewer", "legacy"):
        assert call("POST", f"/v1/goals/{run}/plan/approve", approval, who=who, key="try-" + who).status_code == 403
    response = call("POST", f"/v1/goals/{run}/plan/approve", approval, who="user", key="human")
    assert response.status_code == 200, response.text
    approved = [e for e in store.events("a", run) if e["type"] == "workflow.plan_approved"]
    assert len(approved) == 1 and approved[0]["actor"] == "subject:alice"
    # Refusals are recorded, not silently dropped.
    from loom.platform_ledger import Ledger
    refusals = Ledger(store, initialize=False).history("a", "approval-refusal", f"/v1/goals/{run}/plan/approve")
    assert sorted(r["record"]["subject"] for r in refusals) == ["subject:legacy:a", "subject:ms1:operator", "subject:ms1:worker", "subject:viewer"]
    assert Ledger(store, initialize=False).verify("a", "approval-refusal")


def test_every_audited_write_names_its_subject_even_for_legacy_developer(api):
    # Criterion B5: no anonymous "operator:local" literal for API writes.
    store, _, call = api
    response = call("POST", "/v1/goals", {"title": "x", "description": "y", "acceptance": ["tests.pass"]}, who="legacy", key="lg")
    assert response.status_code == 201
    created = {e["type"]: e["actor"] for e in store.events("a", response.json()["run_id"])}
    assert created["run.created"] == created["goal.created"] == "subject:legacy:a"


def test_event_cursor_pages_are_per_consumer_and_flag_invalid_cursors(api):
    store, _, call = api
    run, approval = saved_plan(call)
    call("POST", f"/v1/goals/{run}/plan/approve", approval, who="user", key="approve")
    total = len(store.events("a", run))
    assert total >= 4
    first = call("GET", f"/v1/runs/{run}/events?after_seq=0&limit=2").json()
    assert [e["seq"] for e in first["events"]] == [1, 2] and first["next_after_seq"] == 2 and first["has_more"]
    # A second consumer starting from the same cursor advances independently (A3).
    other = call("GET", f"/v1/runs/{run}/events?after_seq=0&limit=3", who="worker").json()
    assert [e["seq"] for e in other["events"]] == [1, 2, 3]
    rest = call("GET", f"/v1/runs/{run}/events?after_seq={first['next_after_seq']}&limit=1000").json()
    assert [e["seq"] for e in rest["events"]] == list(range(3, total + 1)) and not rest["has_more"]
    assert rest["push"] == "none" and rest["cursor_owner"] == "consumer"
    stale = call("GET", f"/v1/runs/{run}/events?after_seq={total + 5}&limit=10")
    assert stale.status_code == 409 and stale.json()["code"] == "CURSOR_INVALID"
    assert call("GET", f"/v1/runs/{run}/events?after_seq=0&limit=0").status_code == 422
    assert "events" in call("GET", f"/v1/runs/{run}/events").json()  # unchanged full snapshot without cursor


def plain_claim(store, call, *, lease=30):
    created = store.create_run("a", "plain-" + str(lease), acceptance=["tests.pass"])
    store.add_task("a", created["run_id"], "add-" + str(lease), task_id="work-" + str(lease), title="外部执行")
    claim = call("POST", f"/v1/runs/{created['run_id']}/claim", {"worker_id": "ms1:worker", "lease_seconds": lease}, who="worker").json()["claim"]
    return created["run_id"], claim


def test_renew_extends_only_the_live_owner_epoch(api):
    store, _, call = api
    run, claim = plain_claim(store, call)
    body = {"worker_id": "ms1:worker", "epoch": claim["epoch"], "lease_seconds": 120}
    renewed = call("POST", f"/v1/runs/{run}/tasks/{claim['task_id']}/renew", body, who="worker")
    assert renewed.status_code == 200 and renewed.json()["claim"]["lease_until"] > claim["lease_until"]
    assert call("POST", f"/v1/runs/{run}/tasks/{claim['task_id']}/renew", {**body, "epoch": claim["epoch"] + 1}, who="worker").status_code == 409
    assert call("POST", f"/v1/runs/{run}/tasks/{claim['task_id']}/renew", {**body, "worker_id": "someone"}, who="worker").status_code == 403
    assert call("POST", f"/v1/runs/{run}/tasks/{claim['task_id']}/renew", body, who="operator").status_code == 403


def test_registered_effect_is_never_replayed_after_the_executor_dies(api):
    """Criterion G1: register before acting; an expired owner leaves the task held for a person."""
    store, _, call = api
    run, claim = plain_claim(store, call, lease=0.3)
    body = {"task_id": claim["task_id"], "worker_id": "ms1:worker", "epoch": claim["epoch"], "action_id": "send-mail-1",
            "effect_level": "irreversible", "request": {"to": "team"}}
    registered = call("POST", f"/v1/runs/{run}/effects", body, who="worker")
    assert registered.status_code == 201 and registered.json()["state"] == "intent" and registered.json()["dispatch"] is True
    again = call("POST", f"/v1/runs/{run}/effects", body, who="worker").json()
    assert again["dispatch"] is False  # the same logical action never dispatches twice
    assert call("POST", f"/v1/runs/{run}/effects", {**body, "request": {"to": "other"}}, who="worker").status_code == 409
    time.sleep(0.5)  # the executor "dies": its lease expires without finishing
    assert call("POST", f"/v1/runs/{run}/claim", {"worker_id": "ms1:worker"}, who="worker").json()["claim"] is None
    snapshot = store.snapshot("a", run)
    task = next(t for t in snapshot["tasks"] if t["id"] == claim["task_id"])
    assert task["status"] == "indeterminate"
    effects = call("GET", f"/v1/runs/{run}/review").json()["effects"]
    assert [e["state"] for e in effects] == ["indeterminate"]


def test_finish_effect_settles_a_live_registration(api):
    store, _, call = api
    run, claim = plain_claim(store, call)
    body = {"task_id": claim["task_id"], "worker_id": "ms1:worker", "epoch": claim["epoch"], "action_id": "write-1",
            "effect_level": "write", "request": {}}
    call_id = call("POST", f"/v1/runs/{run}/effects", body, who="worker").json()["call_id"]
    finished = call("POST", f"/v1/runs/{run}/effects/{call_id}/finish",
                    {"task_id": claim["task_id"], "worker_id": "ms1:worker", "epoch": claim["epoch"], "result": {"ok": True}}, who="worker")
    assert finished.status_code == 200 and finished.json()["state"] == "done"
    assert call("POST", f"/v1/runs/{run}/effects", {**body, "effect_level": "teleport"}, who="worker").status_code == 422


def test_requested_by_prefix_is_attributed_at_creation_and_malformed_prefixes_are_rejected(api):
    store, _, call = api
    created = goal(call, "requested-by:bob 统计英文词频", key="attributed").json()
    assert call("GET", f"/v1/goals/{created['run_id']}/requester").json()["requester"] == "bob"
    plain = goal(call, "没有前缀的目标", key="plain").json()
    assert call("GET", f"/v1/goals/{plain['run_id']}/requester").json()["requester"] is None
    listed = call("GET", "/v1/requester-attribution?requester=bob").json()["items"]
    assert [row["run_id"] for row in listed] == [created["run_id"]]
    before = len(call("GET", "/v1/goals").json()["goals"])
    bad = goal(call, "requested-by: 缺少用户名", key="bad")
    assert bad.status_code == 422 and "requested-by" in bad.json()["detail"]
    assert len(call("GET", "/v1/goals").json()["goals"]) == before  # nothing written on failure


def test_execution_domain_inherits_the_tenant_default_unless_an_agent_overrides(api):
    _, _, call = api
    assert call("GET", "/v1/execution-policy").json()["default_execution_domain"] == "local"
    call("POST", "/v1/agents", {"agent_id": "inherit", "base_role": "analyst", "version": "1.0.0"}, who="user", key="a1")
    call("POST", "/v1/agents", {"agent_id": "pinned", "base_role": "analyst", "version": "1.0.0", "execution_domain": "local"},
         who="user", key="a2")
    changed = call("PUT", "/v1/execution-policy", {"default_execution_domain": "remote", "expected_revision": 0, "reason": "中心执行"},
                   who="operator", key="policy")
    assert changed.status_code == 200 and changed.json()["revision"] == 1
    assert call("PUT", "/v1/execution-policy", {"default_execution_domain": "local", "expected_revision": 0, "reason": "旧"},
                who="operator", key="policy-stale").status_code == 409
    assert call("PUT", "/v1/execution-policy", {"default_execution_domain": "local", "expected_revision": 1, "reason": "x"},
                who="worker", key="policy-worker").status_code == 403
    inherited = call("GET", "/v1/agents/inherit").json()["effective_execution_domain"]
    pinned = call("GET", "/v1/agents/pinned").json()["effective_execution_domain"]
    assert inherited == {"execution_domain": "remote", "source": "inherited-from-tenant", "tenant_default": "remote"}
    assert pinned == {"execution_domain": "local", "source": "agent-override", "tenant_default": "remote"}
    assert call("POST", "/v1/agents", {"agent_id": "bad", "base_role": "analyst", "version": "1.0.0", "execution_domain": "mars"},
                who="user", key="a3").status_code == 422


def test_template_parameter_contract_rejects_instead_of_degrading(api, monkeypatch):
    store, _, call = api
    # Builtin template declares no parameters: only an empty argument object passes.
    path = "/v1/domain-packs/software-dev/0.1.0/templates/seven-roles/arguments/validate"
    assert call("POST", path, {"arguments": {}}).json()["valid"] is True
    rejected = call("POST", path, {"arguments": {"x": 1}}).json()
    assert rejected["valid"] is False and rejected["plan_submitted"] is False
    assert call("GET", "/v1/domain-packs/software-dev/0.1.0/templates/seven-roles").json()["parameters"] is None
    # A declared contract validates by schema only (D1).
    contract = {"type": "object", "required": ["text"], "additionalProperties": False,
                "properties": {"text": {"type": "string", "maxLength": 100}, "limit": {"type": "integer", "minimum": 0}}}
    service = DomainPackService(store)
    original = service.template
    monkeypatch.setattr(service, "template", lambda *a: {**original("a", "software-dev", "0.1.0", "seven-roles"), "parameters": contract})
    assert service.validate_arguments("a", "software-dev", "0.1.0", "seven-roles", {"text": "hi", "limit": 2})["valid"]
    bad = service.validate_arguments("a", "software-dev", "0.1.0", "seven-roles", {"limit": -1, "extra": True})
    assert not bad["valid"] and len(bad["errors"]) >= 2
    with pytest.raises(ValueError):
        PackTemplate.model_validate({"id": "t", "title": "t", "nodes": json.loads(PACK.read_text(encoding="utf-8"))["nodes"],
                                     "parameters": {"type": "object", "patternProperties": {}}})
