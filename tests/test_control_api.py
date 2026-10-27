"""Named local API control boundaries, using fixture credentials and SQLite only."""
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.store import Store, digest


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
KEYS = {name: "fixture-" + name + "-api-credential-never-used-remotely" for name in
        ("operator", "operator2", "worker", "worker2", "viewer", "other", "legacy")}


def test_maximum_length_named_worker_can_claim_and_submit(tmp_path):
    store = Store(tmp_path / "long-subject.db")
    created = store.create_run("long-worker", "create", acceptance=["tests.pass"])
    run_id = created["run_id"]
    store.add_task("long-worker", run_id, "add", task_id="work", title="Bound subject length")
    subject = "w" * 128
    identity = LocalIdentity("long-worker", subject, frozenset({"worker"}), KEYS["worker"])
    with TestClient(create_app(store, {}, identities=(identity,))) as client:
        headers = {"Authorization": "Bearer " + KEYS["worker"]}
        claimed = client.post(f"/v1/runs/{run_id}/claim", headers=headers, json={"worker_id": subject})
        assert claimed.status_code == 200
        epoch = claimed.json()["claim"]["epoch"]
        submitted = client.post(f"/v1/runs/{run_id}/tasks/work/submit",
            headers={**headers, "Idempotency-Key": "submit"}, json={"worker_id": subject, "epoch": epoch, "output": {"message": "bound"}})
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "submitted"


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "control-api.db")
    identities = tuple(LocalIdentity(tenant, subject, frozenset({role}), KEYS[name]) for name, tenant, subject, role in (
        ("operator", "a", "operator-a", "operator"),
        ("operator2", "a", "operator-two", "operator"),
        ("worker", "a", "worker-a", "worker"),
        ("worker2", "a", "worker-b", "worker"),
        ("viewer", "a", "viewer-a", "viewer"),
        ("other", "b", "operator-b", "operator"),
    ))
    with TestClient(create_app(store, {"a": KEYS["legacy"]}, identities=identities)) as client:
        sequence = 0

        def request(method, path, body=None, *, role="operator", key=None, headers=None):
            supplied = {"Authorization": "Bearer " + KEYS[role]}
            if key:
                supplied["Idempotency-Key"] = key
            supplied.update(headers or {})
            return client.request(method, path, json=body, headers=supplied)

        def managed_goal(*, child=False):
            nonlocal sequence
            sequence += 1
            prefix = "control-api:" + str(sequence)
            body = {"title": "Fixture control target", "description": "No paid calls or source execution.",
                    "acceptance": ["tests.pass"], "cost_limit_microusd": 1000000, "token_limit": 10000}
            created = request("POST", "/v1/goals", body, key=prefix)
            assert created.status_code == 201
            run_id = created.json()["run_id"]
            definition = json.loads(PACK.read_text(encoding="utf-8"))
            definition["nodes"] = [{"id": "work", "title": "Fixture developer", "instruction": "Fixture JSON task.",
                                    "role": "developer", "depends": [], "priority": 10, "channel": "business",
                                    "limits": {"cost_limit_microusd": 200000, "token_limit": 4000, "max_attempts": 3}}]
            if child:
                definition["nodes"].append({"id": "review", "title": "Fixture independent review", "instruction": "Wait for trusted verification.",
                                             "role": "validator", "depends": ["work"], "priority": 0, "channel": "verification",
                                             "limits": {"cost_limit_microusd": 10000, "token_limit": 1000, "max_attempts": 1}})
            saved = request("PUT", f"/v1/goals/{run_id}/plan", {"expected_revision": 0, "definition": definition}, key=prefix + ":plan")
            assert saved.status_code == 200
            plan = saved.json()
            approved = request("POST", f"/v1/goals/{run_id}/plan/approve",
                               {"expected_revision": plan["revision"], "expected_digest": plan["digest"]}, key=prefix + ":approve")
            assert approved.status_code == 200
            return run_id

        yield SimpleNamespace(store=store, client=client, request=request, managed_goal=managed_goal)


def claim(api, run_id, *, role="worker", subject="worker-a"):
    response = api.request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": subject, "lease_seconds": 300}, role=role)
    assert response.status_code == 200
    return response.json()["claim"]


def submit(api, run_id, current, output, *, role="worker", subject="worker-a", key="submit"):
    return api.request("POST", f"/v1/runs/{run_id}/tasks/{current['task_id']}/submit",
                       {"worker_id": subject, "epoch": current["epoch"], "output": output}, role=role, key=key)


def unknown_model_fixture(api, run_id):
    """Persist an unresolved fixture intent; no model/network request is sent."""
    current = api.store.claim_next("a", run_id, "fixture-trusted-executor", lease_seconds=300)
    reservation_id = f"python:{current.task_id}:{current.epoch}"
    api.store.reserve(current, reservation_id, max_cost=1000, max_tokens=1000)
    effect = api.store.begin_effect(current, f"model-generation:{current.epoch}", {"fixture": "not a real request"})
    with api.store.transaction() as db:
        db.execute("UPDATE effects SET state='indeterminate' WHERE tenant='a' AND id=?", (effect["call_id"],))
        api.store._event(db, "a", run_id, "tool.indeterminate", {"call_id": effect["call_id"], "state": "indeterminate"}, "fixture-executor")
        db.execute("UPDATE runs SET status='awaiting_human' WHERE tenant='a' AND id=?", (run_id,))
        db.execute("UPDATE tasks SET status='awaiting_human',worker=NULL,lease_until=NULL WHERE tenant='a' AND id=?", (current.task_id,))
        api.store._event(db, "a", run_id, "execution.blocked", {"task_id": current.task_id, "status": "awaiting_human"}, "fixture-executor")
    return current, effect["call_id"], reservation_id


def test_me_lists_named_capabilities_without_credentials(api):
    response = api.request("GET", "/v1/me")
    assert response.status_code == 200
    assert response.json() == {"tenant_id": "a", "subject_id": "operator-a", "roles": ["operator"],
                               "capabilities": ["control", "goal.write", "plan.approve", "read", "suite.approve"],
                               "authentication": "local-bearer", "production_identity": False}
    assert all(key not in response.text for key in KEYS.values())
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert api.client.get("/v1/me").status_code == 401
    assert api.client.get("/v1/interventions").status_code == 401


@pytest.mark.parametrize("role", ["worker", "viewer", "legacy"])
def test_control_is_denied_to_non_operator_roles(api, role):
    run_id = api.managed_goal()
    for action in ("pause", "resume", "terminate"):
        assert api.request("POST", f"/v1/runs/{run_id}/control/{action}", {"reason": "fixture reason"}, role=role, key=role + action).status_code == 403
    assert api.store.snapshot("a", run_id)["status"] == "open"


@pytest.mark.parametrize("role", ["worker", "viewer"])
def test_worker_and_viewer_cannot_write_goals_plans_or_approve(api, role):
    run_id = api.managed_goal()
    assert api.request("POST", "/v1/goals", {"title": "x", "description": "x", "acceptance": ["tests.pass"]}, role=role, key="denied-goal").status_code == 403
    assert api.request("POST", "/v1/runs", {"acceptance": ["tests.pass"]}, role=role, key="denied-run").status_code == 403
    assert api.request("PUT", f"/v1/goals/{run_id}/plan", {"expected_revision": 1, "definition": json.loads(PACK.read_text(encoding="utf-8"))}, role=role, key="denied-save").status_code == 403
    assert api.request("POST", f"/v1/goals/{run_id}/plan/approve", {"expected_revision": 1, "expected_digest": "0" * 64}, role=role, key="denied-approve").status_code == 403
    assert api.request("POST", f"/v1/runs/{run_id}/tasks", {"task_id": "injected", "title": "bad"}, role=role, key="denied-task").status_code == 403


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_operator_and_viewer_cannot_claim_or_submit_as_workers(api, role):
    run_id = api.managed_goal()
    current = claim(api, run_id)
    assert api.request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "worker-a"}, role=role).status_code == 403
    assert submit(api, run_id, current, {}, role=role).status_code == 403


def test_named_workers_cannot_impersonate_same_tenant_peer(api):
    run_id = api.managed_goal()
    assert api.request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "worker-a"}, role="worker2").status_code == 403
    current = claim(api, run_id)
    assert submit(api, run_id, current, {}, role="worker2", subject="worker-a").status_code == 403
    # Matching one's own subject still cannot satisfy another worker's claim.
    assert submit(api, run_id, current, {}, role="worker2", subject="worker-b").status_code == 409
    assert submit(api, run_id, current, {"message": "own fixture submission"}).status_code == 200


def test_headers_and_json_cannot_override_authenticated_principal(api):
    run_id = api.managed_goal()
    current = claim(api, run_id)
    headers = {"X-Role": "operator", "X-Tenant-ID": "a", "X-Subject-ID": "operator-a"}
    assert api.request("GET", "/v1/me", role="worker", headers=headers).json()["subject_id"] == "worker-a"
    assert api.request("POST", f"/v1/runs/{run_id}/control/pause", {"reason": "spoof"}, role="worker", key="spoof", headers=headers).status_code == 403
    response = api.request("POST", f"/v1/runs/{run_id}/tasks/{current['task_id']}/submit",
                           {"worker_id": "worker-a", "epoch": current["epoch"], "output": {}, "roles": ["operator"], "tenant_id": "b"}, role="worker", key="extra-subject")
    assert response.status_code == 422
    assert all(key not in response.text for key in KEYS.values())


def test_cross_tenant_resources_and_controls_are_not_found(api):
    run_id = api.managed_goal()
    for suffix in ("", "/review", "/events", "/integrity", "/replay"):
        assert api.request("GET", f"/v1/runs/{run_id}" + suffix, role="other").status_code == 404
    for suffix in ("", "/plan"):
        assert api.request("GET", f"/v1/goals/{run_id}" + suffix, role="other").status_code == 404
    assert api.request("POST", f"/v1/runs/{run_id}/control/pause", {"reason": "cross-tenant"}, role="other", key="other-pause").status_code == 404
    assert api.request("GET", "/v1/goals", role="other").json() == {"goals": []}
    assert api.request("GET", "/v1/interventions", role="other").json() == {"items": []}


def test_named_goal_plan_actors_and_idempotency_are_bound_to_subject(api):
    goal_body = {"title": "Actor test", "description": "No live execution", "acceptance": ["tests.pass"]}
    created = api.request("POST", "/v1/goals", goal_body, key="actor-create")
    assert created.status_code == 201
    assert api.request("POST", "/v1/goals", goal_body, key="actor-create").json() == created.json()
    assert api.request("POST", "/v1/goals", goal_body, role="operator2", key="actor-create").status_code == 409
    run_id = created.json()["run_id"]
    plan_body = {"expected_revision": 0, "definition": json.loads(PACK.read_text(encoding="utf-8"))}
    saved = api.request("PUT", f"/v1/goals/{run_id}/plan", plan_body, key="actor-save")
    assert saved.status_code == 200
    assert api.request("PUT", f"/v1/goals/{run_id}/plan", plan_body, key="actor-save").json() == saved.json()
    assert api.request("PUT", f"/v1/goals/{run_id}/plan", plan_body, role="operator2", key="actor-save").status_code == 409
    approval = {"expected_revision": saved.json()["revision"], "expected_digest": saved.json()["digest"]}
    assert api.request("POST", f"/v1/goals/{run_id}/plan/approve", approval, key="actor-approve").status_code == 200
    assert api.request("POST", f"/v1/goals/{run_id}/plan/approve", approval, role="operator2", key="actor-approve").status_code == 409
    events = api.store.events("a", run_id)
    writes = [event for event in events if event["type"] in {"run.created", "goal.created", "workflow.plan_saved", "workflow.plan_approved"}]
    assert len(writes) == 4 and all(event["actor"] == "subject:operator-a" for event in writes)
    assert api.store.verify_events(events)


def test_fake_worker_tests_are_not_trusted_evidence_or_final_success(api):
    run_id = api.managed_goal(child=True)
    current = claim(api, run_id)
    output = {"kind": "pretend-python", "source": "PRIVATE_FIXTURE_SOURCE_SHOULD_NOT_BE_DISPLAYED",
              "verified": True, "tests": {"passed": True, "cases": [{"name": "pretend", "passed": True}]}}
    assert submit(api, run_id, current, output).status_code == 200
    review = api.request("GET", f"/v1/runs/{run_id}/review", role="viewer")
    assert review.status_code == 200
    result = review.json()
    artifact = result["tasks"][0]["artifact"]
    assert artifact["trusted_evaluation"] is False and artifact["hard_criteria_passed"] is None
    assert artifact["cases"] == [] and artifact["verification"] == "awaiting_independent_models"
    assert result["verification"] == "pending_configuration" and result["counts_as_success"] is False
    assert output["source"] not in review.text and all(key not in review.text for key in KEYS.values())
    assert api.request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "worker-a", "channel": "verification"}, role="worker").json()["claim"] is None
    items = api.request("GET", "/v1/interventions", role="viewer").json()["items"]
    assert items[0]["run_id"] == run_id and items[0]["awaiting_independent_verification"] == 1


@pytest.mark.parametrize("binding", ["matching", "wrong-epoch", "wrong-content"])
def test_review_only_uses_fixture_executor_evidence_bound_to_current_output(api, binding):
    run_id = api.managed_goal()
    current = claim(api, run_id)
    output = {"kind": "fixture", "tests": {"cases": [{"name": "fixture-case", "passed": True, "status": "passed"}]}}
    assert submit(api, run_id, current, output).status_code == 200
    with api.store.transaction() as db:
        api.store._event(db, "a", run_id, "execution.evaluated", {
            "task_id": current["task_id"], "epoch": current["epoch"] + (binding == "wrong-epoch"),
            "content_hash": "0" * 64 if binding == "wrong-content" else digest(output), "hard_criteria_passed": True}, "fixture-trusted-executor")
    artifact = api.request("GET", f"/v1/runs/{run_id}/review").json()["tasks"][0]["artifact"]
    assert artifact["trusted_evaluation"] is (binding == "matching")
    assert len(artifact["cases"]) == (1 if binding == "matching" else 0)
    assert artifact["verification"] == "awaiting_independent_models"


def test_control_idempotency_actor_and_terminal_state_are_enforced(api):
    run_id = api.managed_goal()
    path = f"/v1/runs/{run_id}/control/"
    pause = api.request("POST", path + "pause", {"reason": "Inspect fixture"}, key="pause")
    assert pause.status_code == 200 and pause.json()["status"] == "paused"
    assert api.request("POST", path + "pause", {"reason": "Inspect fixture"}, key="pause").json() == pause.json()
    assert api.request("POST", path + "pause", {"reason": "Inspect fixture"}, role="operator2", key="pause").status_code == 409
    assert api.request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "worker-a"}, role="worker").status_code == 409
    assert api.request("POST", path + "resume", {"reason": "Reviewed fixture"}, key="resume").json()["status"] == "open"
    cancelled = api.request("POST", path + "terminate", {"reason": "Stop fixture"}, key="terminate")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["counts_as_success"] is False
    assert api.request("POST", path + "resume", {"reason": "No revival"}, key="after-terminal").status_code == 409
    assert api.request("POST", path + "accept", {"reason": "No bypass"}, key="bad-action").status_code == 404
    replay = api.store.replay("a", run_id)
    assert replay["status"] == "cancelled"
    assert api.store.verify_events(api.store.events("a", run_id))


def test_pausing_and_resuming_submitted_candidate_cannot_mark_it_done(api):
    run_id = api.managed_goal(child=True)
    current = claim(api, run_id)
    assert submit(api, run_id, current, {"tests": {"passed": True}}).status_code == 200
    for action in ("pause", "resume"):
        assert api.request("POST", f"/v1/runs/{run_id}/control/{action}", {"reason": "Keep candidate pending"}, key="submitted-" + action).status_code == 200
    statuses = {item["id"]: item["status"] for item in api.store.snapshot("a", run_id)["tasks"]}
    assert statuses[current["task_id"]] == "submitted"
    assert statuses[run_id + ":review"] == "pending"
    assert api.request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "worker-a", "channel": "verification"}, role="worker").json()["claim"] is None


def test_unknown_model_call_requires_explicit_reconciliation_before_resume(api):
    run_id = api.managed_goal()
    current, call_id, reservation_id = unknown_model_fixture(api, run_id)
    assert api.request("POST", f"/v1/runs/{run_id}/control/resume", {"reason": "Must first reconcile"}, key="unknown-resume").status_code == 409
    request = {"decision": "completed", "evidence": {"reference": "fixture-receipt:known", "summary": "Fixture usage was manually checked."},
               "actual_cost_microusd": 120, "actual_tokens": 35}
    path = f"/v1/runs/{run_id}/effects/{call_id}/reconcile"
    assert api.request("POST", path, request, role="worker", key="worker-reconcile").status_code == 403
    assert api.request("POST", path, request, role="other", key="other-reconcile").status_code == 404
    response = api.request("POST", path, request, key="reconcile")
    assert response.status_code == 200
    assert response.json()["state"] == "reconciled" and response.json()["verified"] is False
    assert api.request("POST", path, request, key="reconcile").json() == response.json()
    assert api.request("POST", path, request, role="operator2", key="reconcile").status_code == 409
    assert api.request("POST", path, {**request, "actual_tokens": 36}, key="rewrite").status_code == 409
    snapshot = api.store.snapshot("a", run_id)
    assert snapshot["spent"] == 120 and snapshot["reserved"] == 0 and snapshot["tokens_spent"] == 35
    assert api.request("POST", f"/v1/runs/{run_id}/control/resume", {"reason": "Approved next bounded attempt"}, key="reconciled-resume").status_code == 200
    new_claim = claim(api, run_id)
    assert new_claim["epoch"] > current.epoch
    assert api.store.replay("a", run_id)["spent"] == snapshot["spent"]
    assert api.store.verify_events(api.store.events("a", run_id))


@pytest.mark.parametrize("invalid", [
    {"decision": "accept"}, {"actual_cost_microusd": True}, {"actual_tokens": -1},
    {"evidence": {"reference": "fixture-only"}}, {"actor": "forged-operator"},
])
def test_reconciliation_schema_rejects_privilege_and_usage_injection(api, invalid):
    run_id = api.managed_goal()
    _, call_id, _ = unknown_model_fixture(api, run_id)
    body = {"decision": "not_sent", "evidence": {"reference": "fixture-no-dispatch", "summary": "Fixture call was not sent."},
            "actual_cost_microusd": 0, "actual_tokens": 0}
    response = api.request("POST", f"/v1/runs/{run_id}/effects/{call_id}/reconcile", {**body, **invalid}, key="invalid-reconcile")
    assert response.status_code == 422
    assert api.store.snapshot("a", run_id)["reserved"] == 1000
    assert all(key not in response.text for key in KEYS.values())
