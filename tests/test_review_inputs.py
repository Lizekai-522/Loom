"""Real SQLite and lifecycle; simulated provider and sandbox, no paid calls."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.approved_execution import ReadOnlyStore
from loom.execution import LocalTaskRunner, freeze_tests
from loom.model_gateway import ModelGateway, ModelRoute
from loom.review_inputs import ReviewInputService
from loom.sandbox import FrozenTest
from loom.store import Conflict, InvalidInput, NotFound, Store, canonical, digest
from loom.workflow import WorkflowService
from tests.test_execution import FakeSandbox, SOURCE


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def review_case(tmp_path):
    store = Store(tmp_path / "review.db")
    workflow = WorkflowService(store)
    suite = (FrozenTest("hidden-name-marker", {"hidden-input": "secret-golden"}, {"answer": 2}),)
    goal = workflow.create_goal("a", "goal", title="审查测试", description="只审查已提交的代码",
        acceptance=["tests.pass"], cost_limit=1000000, token_limit=2000000)
    run = goal["run_id"]
    definition = json.loads((ROOT / "packs/software-dev/workflow-v1.json").read_bytes())
    definition["nodes"] = [{"id": "work", "title": "开发", "instruction": "加一", "role": "developer",
        "limits": {"cost_limit_microusd": 999900, "token_limit": 1999900, "max_attempts": 2},
        "evaluation_digest": freeze_tests(suite)[1]}, {"id": "review", "title": "审查", "instruction": "等待独立验收",
        "role": "reviewer", "depends": ["work"],
        "limits": {"cost_limit_microusd": 100, "token_limit": 100, "max_attempts": 1}}]
    saved = workflow.save_plan("a", run, "save", definition, 0)
    workflow.approve_plan("a", run, "approve", saved["revision"], saved["digest"])
    claim = store.claim_next("a", run, "worker")

    async def execute(sandbox=None, source=SOURCE):
        def handler(request):
            return httpx.Response(200, json={"id": "review-fixture", "object": "chat.completion", "created": 1792700000,
                "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({"source": source})}}],
                "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35}})
        async with ModelGateway([ModelRoute("deepseek-flash")], transport=httpx.MockTransport(handler),
            environ={"DEEPSEEK_API_KEY": "review-public-test-only"}) as gateway:
            return await LocalTaskRunner(store, gateway, sandbox or FakeSandbox()).run_python(claim, suite, max_output_tokens=128)

    return dict(store=store, workflow=workflow, run=run, claim=claim, execute=execute, suite=suite,
                service=ReviewInputService(ReadOnlyStore(store.path)))


def ready(b):
    assert asyncio.run(b["execute"]())["status"] == "submitted"
    return b["service"].preview("a", b["run"], b["claim"].task_id)


def candidate(preview):
    return {"schema_version": "loom.review-findings/v1", "subject": preview["subject"], "summary": "候选意见，尚未独立验证",
            "verdict": "findings", "findings": [{"id": "one", "severity": "minor", "path": "main.py",
            "start_line": 1, "end_line": 1, "description": "模拟审查建议", "recommendation": "核对需求"}]}


def test_exact_preview_is_read_only_and_does_not_release_dependencies(review_case):
    b = review_case; p = ready(b)
    before = b["store"].events("a", b["run"])
    full = b["service"].preview("a", b["run"], b["claim"].task_id, include_source=True)
    assert full["input_digest"] == p["input_digest"] and full["subject"] == p["subject"]
    assert full["files"] == {"main.py": SOURCE} and "files" not in p
    assert p["subject"]["source_sha256"] == digest({"main.py": SOURCE})
    assert p["execution_summary"]["cases_passed"] == p["execution_summary"]["cases_executed"] == 1
    for hidden in ("hidden-name-marker", "hidden-input", "secret-golden", "SIMULATED OUTPUT", "stdout", "stderr"):
        assert hidden not in canonical(full)
    assert not full["independently_verified"] and not full["counts_as_success"] and not full["execution_authorized"]
    assert b["store"].events("a", b["run"]) == before
    assert b["store"].claim_next("a", b["run"], "reviewer") is None


def test_bound_findings_and_no_findings_only_validate_without_persistence(review_case):
    b = review_case; p = ready(b); body = candidate(p)
    before = b["store"].events("a", b["run"])
    for value in (body, {**body, "verdict": "no_findings", "findings": []}):
        result = b["service"].validate_findings("a", b["run"], b["claim"].task_id, value)
        assert result["schema_validated"] and result["subject_bound"]
        assert not result["persisted"] and not result["model_source_recorded"] and not result["counts_as_success"]
    assert b["store"].events("a", b["run"]) == before


@pytest.mark.parametrize("field", ["epoch", "plan_digest", "content_hash", "source_sha256", "evaluation_digest", "evaluation_event_hash", "submission_event_hash", "run_id", "task_id"])
def test_every_subject_pin_rechecked(review_case, field):
    b = review_case; body = candidate(ready(b))
    body["subject"][field] = 2 if field == "epoch" else "0" * 64
    with pytest.raises(Conflict):
        b["service"].validate_findings("a", b["run"], b["claim"].task_id, body)


@pytest.mark.parametrize("change", ["extra-authority", "wrong-schema", "passed-verdict", "empty-findings", "duplicate-id", "bool-line", "negative-line", "reverse-lines", "outside-source", "wrong-path", "wrong-severity", "too-large"])
def test_strict_findings_and_line_bounds(review_case, change):
    b = review_case; body = copy.deepcopy(candidate(ready(b))); finding = body["findings"][0]
    if change == "extra-authority": body["verified"] = True
    elif change == "wrong-schema": body["schema_version"] = "loom.requirements/v1"
    elif change == "passed-verdict": body["verdict"] = "passed"
    elif change == "empty-findings": body["findings"] = []
    elif change == "duplicate-id": body["findings"].append(copy.deepcopy(finding))
    elif change == "bool-line": finding["start_line"] = True
    elif change == "negative-line": finding["start_line"] = 0
    elif change == "reverse-lines": finding.update(start_line=2, end_line=1)
    elif change == "outside-source": finding["end_line"] = 100
    elif change == "wrong-path": finding["path"] = "../../private.json"
    elif change == "wrong-severity": finding["severity"] = "approved"
    elif change == "too-large": body["summary"] = "x" * 40000
    with pytest.raises(InvalidInput):
        b["service"].validate_findings("a", b["run"], b["claim"].task_id, body)


@pytest.mark.parametrize("change", ["source", "tests", "report-pass", "plan", "epoch", "pending", "unresolved", "cached-contract", "event-hash", "event-envelope"])
def test_source_projection_evidence_or_contract_drift_rejected(review_case, change):
    b = review_case; ready(b); claim = b["claim"]
    with b["store"].transaction() as db:
        row = db.execute("SELECT output FROM tasks WHERE id=?", (claim.task_id,)).fetchone()
        output = json.loads(row[0])
        if change == "source": output["source"] += "# changed\n"
        elif change == "tests": output["tests"]["tests_sha256"] = "0" * 64
        elif change == "report-pass": output["tests"]["passed"] = False
        elif change == "plan": output["plan_digest"] = "0" * 64
        elif change == "epoch": db.execute("UPDATE tasks SET epoch=epoch+1 WHERE id=?", (claim.task_id,))
        elif change == "pending": db.execute("UPDATE tasks SET status='pending' WHERE id=?", (claim.task_id,))
        elif change == "unresolved": db.execute("UPDATE effects SET state='indeterminate' WHERE task_id=?", (claim.task_id,))
        elif change == "cached-contract": db.execute("UPDATE workflow_tasks SET contract='{}' WHERE task_id=?", (claim.task_id,))
        elif change in {"event-hash", "event-envelope"}:
            db.execute("DROP TRIGGER events_no_update")
            if change == "event-hash": db.execute("UPDATE events SET hash=? WHERE run_id=? AND seq=1", ("0" * 64, claim.run_id))
            else: db.execute("UPDATE events SET envelope='{}' WHERE run_id=? AND seq=1", (claim.run_id,))
        db.execute("UPDATE tasks SET output=? WHERE id=?", (canonical(output), claim.task_id))
    with pytest.raises(Conflict): b["service"].preview("a", b["run"], claim.task_id)


def test_cross_tenant_cross_run_unsubmitted_and_worker_claims_rejected(review_case):
    b = review_case; claim = b["claim"]
    with pytest.raises(Conflict): b["service"].preview("a", b["run"], claim.task_id)
    with pytest.raises(NotFound): b["service"].preview("b", b["run"], claim.task_id)
    b["store"].submit_output(claim, "untrusted", {"kind": "python-json-program", "source": SOURCE, "verified": True})
    with pytest.raises(Conflict): b["service"].preview("a", b["run"], claim.task_id)


def test_http_permissions_source_opt_in_and_pure_validation(review_case):
    b = review_case; ready(b)
    path = f'/v1/runs/{b["run"]}/tasks/{b["claim"].task_id}'
    with TestClient(create_app(b["store"], {"a": "review-api-public-fixture-a", "b": "review-api-public-fixture-b"})) as client:
        headers = {"Authorization": "Bearer review-api-public-fixture-a"}
        assert client.get(path + "/review-input").status_code == 401
        assert client.get(path + "/review-input", headers={"Authorization": "Bearer review-api-public-fixture-b"}).status_code == 404
        response = client.get(path + "/review-input", headers=headers)
        assert response.status_code == 200 and "files" not in response.json()
        assert client.get(path + "/review-input?include_source=true", headers=headers).json()["files"] == {"main.py": SOURCE}
        before = b["store"].events("a", b["run"])
        result = client.post(path + "/review-findings/validate", headers=headers, json=candidate(response.json()))
        assert result.status_code == 200 and result.json()["persisted"] is False
        body = candidate(response.json()); body["verified"] = True
        assert client.post(path + "/review-findings/validate", headers=headers, json=body).status_code == 422
        assert b["store"].events("a", b["run"]) == before


def test_cli_is_read_only_and_malformed_input_errors_do_not_echo(review_case, tmp_path):
    b = review_case; ready(b)
    command = [sys.executable, str(ROOT / "scripts/preview_review_input.py"), "--db", b["store"].path,
               "--tenant", "a", "--run-id", b["run"], "--task-id", b["claim"].task_id]
    before = b["store"].events("a", b["run"])
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["passed"] and "files" not in json.loads(result.stdout)
    bad = tmp_path / "bad.json"; bad.write_text('{"secret":"DO-NOT-ECHO", "secret":2}', encoding="utf-8")
    result = subprocess.run(command + ["--candidate-file", str(bad)], capture_output=True, text=True)
    assert result.returncode == 1 and "DO-NOT-ECHO" not in result.stdout + result.stderr
    assert b["store"].events("a", b["run"]) == before


@pytest.mark.parametrize("change", ["wrong-actor", "missing-evaluation", "duplicate-evaluation", "wrong-tests", "wrong-verdict", "wrong-worker", "wrong-order"])
def test_consistent_event_hashes_cannot_replace_exact_executor_evidence(review_case, change):
    b = review_case; ready(b)
    with b["store"].transaction() as db:
        rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (b["run"],)).fetchall()
        events = [json.loads(row["envelope"]) for row in rows]
        evaluated = next(e for e in events if e["type"] == "execution.evaluated")
        submitted = next(e for e in events if e["type"] == "task.output_submitted")
        if change == "wrong-actor": evaluated["actor"] = "worker:worker"
        elif change == "missing-evaluation": evaluated["type"] = "worker.self_report"
        elif change == "duplicate-evaluation":
            # Append a second well-formed event after this transaction below.
            pass
        elif change == "wrong-tests": evaluated["payload"]["evaluation_digest"] = "0" * 64
        elif change == "wrong-verdict": evaluated["payload"]["hard_criteria_passed"] = False
        elif change == "wrong-worker": submitted["actor"] = "worker:another"
        elif change == "wrong-order":
            for key in ("type", "actor", "payload"):
                evaluated[key], submitted[key] = submitted[key], evaluated[key]
        db.execute("DROP TRIGGER events_no_update")
        previous = "0" * 64
        for row, event in zip(rows, events):
            event["prev_hash"] = previous; previous = digest(event)
            db.execute("UPDATE events SET envelope=?,hash=? WHERE id=?", (canonical(event), previous, row["id"]))
        if change == "duplicate-evaluation":
            b["store"]._event(db, "a", b["run"], "execution.evaluated", evaluated["payload"], "local-executor")
    assert b["store"].verify_events(b["store"].events("a", b["run"]))
    with pytest.raises(Conflict): b["service"].preview("a", b["run"], b["claim"].task_id)


def test_static_review_context_limit_rejects_large_source(review_case):
    b = review_case
    assert asyncio.run(b["execute"](source=SOURCE + "#" + "x" * 66000))["status"] == "submitted"
    with pytest.raises(Conflict, match="static byte limit"):
        b["service"].preview("a", b["run"], b["claim"].task_id)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(InvalidInput): ReadOnlyStore(path)
    assert not path.exists()
