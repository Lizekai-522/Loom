"""Check role-separated loopback HTTP controls; never call a model or print tokens."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

from loom import __version__
from loom.auth import load_identities


class CheckFailure(Exception):
    """Only code-owned diagnostic labels may cross the report boundary."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run_checks(base, identities, checks):
    selected = None
    for operator in identities:
        if "operator" not in operator.roles:
            continue
        workers = [item for item in identities if item.tenant_id == operator.tenant_id and "worker" in item.roles]
        viewers = [item for item in identities if item.tenant_id == operator.tenant_id and item.roles == frozenset({"viewer"})]
        if workers and viewers:
            selected = (operator, workers[0], viewers[0])
            break
    if selected is None:
        raise CheckFailure("MATCHED_OPERATOR_WORKER_VIEWER_REQUIRED")
    operator, worker, viewer = selected
    private_values = tuple(identity.token.encode("ascii") for identity in identities)
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(identity, method, path, body=None, key=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if identity is not None:
            headers["Authorization"] = "Bearer " + identity.token
        if key is not None:
            headers["Idempotency-Key"] = key
        req = Request(base + path, data=json.dumps(body).encode("utf-8") if body is not None else None,
                      headers=headers, method=method)
        try:
            response = opener.open(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(2_097_153)
            if len(raw) > 2_097_152:
                raise CheckFailure("HTTP_RESPONSE_TOO_LARGE")
            if any(value in raw for value in private_values):
                raise CheckFailure("HTTP_RESPONSE_CONTAINS_PRIVATE_CREDENTIAL")
            return response.code, json.loads(raw)

    def expect(condition, label):
        if not condition:
            raise CheckFailure(label)
        checks.append(label)

    status, health = request(None, "GET", "/health")
    expect(status == 200 and health.get("version") == __version__, "current_version_healthy")
    status, me = request(operator, "GET", "/v1/me")
    expect(status == 200 and me.get("subject_id") == operator.subject_id and "operator" in me.get("roles", [])
           and "control" in me.get("capabilities", []) and "task.claim" not in me.get("capabilities", []),
           "operator_subject_and_control_permissions")
    status, me = request(worker, "GET", "/v1/me")
    expect(status == 200 and me.get("subject_id") == worker.subject_id and "task.claim" in me.get("capabilities", [])
           and "control" not in me.get("capabilities", []), "worker_subject_and_execution_permissions")
    status, me = request(viewer, "GET", "/v1/me")
    expect(status == 200 and me.get("subject_id") == viewer.subject_id and me.get("capabilities") == ["read"],
           "viewer_has_read_only_permissions")
    expect(request(None, "GET", "/v1/me")[0] == 401, "missing_identity_denied")
    prefix = "operator-http:" + uuid4().hex
    body = {"title": "Operator HTTP control fixture", "description": "Synthetic HTTP assertions only; no model or code execution.",
            "acceptance": ["tests.pass"], "cost_limit_microusd": 1000000, "token_limit": 10000}
    expect(request(viewer, "POST", "/v1/goals", body, prefix + ":viewer-goal")[0] == 403,
           "viewer_cannot_create_goal")
    status, created = request(operator, "POST", "/v1/goals", body, prefix + ":goal")
    run_id = created.get("run_id")
    expect(status == 201 and isinstance(run_id, str) and str(UUID(run_id)) == run_id
           and created.get("status") == "draft", "operator_created_draft_goal")
    definition = json.loads((Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
    plan_body = {"definition": definition, "expected_revision": 0}
    expect(request(worker, "PUT", f"/v1/goals/{run_id}/plan", plan_body, prefix + ":worker-plan")[0] == 403,
           "worker_cannot_write_plan")
    status, plan = request(operator, "PUT", f"/v1/goals/{run_id}/plan", plan_body, prefix + ":plan")
    expect(status == 200 and plan.get("revision") == 1 and len(plan.get("definition", {}).get("roles", [])) == 7,
           "operator_saved_seven_role_plan")
    approval = {"expected_revision": 1, "expected_digest": plan["digest"]}
    expect(request(viewer, "POST", f"/v1/goals/{run_id}/plan/approve", approval, prefix + ":viewer-approve")[0] == 403,
           "viewer_cannot_approve_plan")
    status, approved = request(operator, "POST", f"/v1/goals/{run_id}/plan/approve", approval, prefix + ":approve")
    expect(status == 200 and request(operator, "POST", f"/v1/goals/{run_id}/plan/approve", approval, prefix + ":approve")[1] == approved,
           "operator_approval_is_idempotent")
    reason = {"reason": "Synthetic control check; no real execution or external effects."}
    expect(request(viewer, "POST", f"/v1/runs/{run_id}/control/pause", reason, prefix + ":viewer-pause")[0] == 403,
           "viewer_cannot_control_run")
    expect(request(worker, "POST", f"/v1/runs/{run_id}/control/pause", reason, prefix + ":worker-pause")[0] == 403,
           "worker_cannot_control_run")
    claim_body = {"worker_id": worker.subject_id, "lease_seconds": 300}
    expect(request(operator, "POST", f"/v1/runs/{run_id}/claim", claim_body)[0] == 403,
           "operator_cannot_claim_worker_task")
    expect(request(viewer, "POST", f"/v1/runs/{run_id}/claim", claim_body)[0] == 403,
           "viewer_cannot_claim_worker_task")
    impersonated = "impersonated-worker" if worker.subject_id != "impersonated-worker" else "another-worker"
    expect(request(worker, "POST", f"/v1/runs/{run_id}/claim", {"worker_id": impersonated})[0] == 403,
           "worker_cannot_impersonate_another_subject")
    status, response = request(worker, "POST", f"/v1/runs/{run_id}/claim", claim_body)
    claim = response.get("claim")
    expect(status == 200 and isinstance(claim, dict) and claim.get("worker_id") == worker.subject_id
           and claim.get("task_id") == run_id + ":spec", "worker_claims_as_authenticated_subject")
    task_id = run_id + ":spec"
    fake_output = {"kind": "synthetic-http-fixture", "verified": True,
                   "tests": {"passed": True, "cases": [{"name": "untrusted-self-reported", "passed": True}]}}
    submission = {"worker_id": worker.subject_id, "epoch": claim["epoch"], "output": fake_output}
    expect(request(worker, "POST", f"/v1/runs/{run_id}/tasks/{task_id}/submit",
                   {**submission, "worker_id": impersonated}, prefix + ":impersonated-submit")[0] == 403,
           "worker_cannot_submit_under_forged_subject")
    status, submitted = request(worker, "POST", f"/v1/runs/{run_id}/tasks/{task_id}/submit", submission, prefix + ":submit")
    expect(status == 200 and submitted.get("status") == "submitted", "worker_submission_stays_unverified")
    expect(request(worker, "POST", f"/v1/runs/{run_id}/claim", claim_body)[1].get("claim") is None
           and request(worker, "POST", f"/v1/runs/{run_id}/claim", {**claim_body, "channel": "verification"})[1].get("claim") is None,
           "downstream_waits_for_trusted_verification")
    status, review = request(viewer, "GET", f"/v1/runs/{run_id}/review")
    artifacts = [task.get("artifact") for task in review.get("tasks", []) if task.get("id") == task_id]
    expect(status == 200 and len(artifacts) == 1 and artifacts[0].get("trusted_evaluation") is False
           and artifacts[0].get("hard_criteria_passed") is None and artifacts[0].get("cases") == []
           and review.get("verification") == "pending_configuration" and review.get("counts_as_success") is False,
           "worker_test_flags_do_not_become_trusted_evidence")
    status, queue = request(viewer, "GET", "/v1/interventions")
    expect(status == 200 and any(item.get("run_id") == run_id and item.get("awaiting_independent_verification") == 1
                                for item in queue.get("items", [])), "pending_verification_is_visible_in_interventions")
    status, paused = request(operator, "POST", f"/v1/runs/{run_id}/control/pause", reason, prefix + ":pause")
    expect(status == 200 and paused.get("status") == "paused"
           and request(operator, "POST", f"/v1/runs/{run_id}/control/pause", reason, prefix + ":pause")[1] == paused,
           "pause_is_durable_and_idempotent")
    expect(request(worker, "POST", f"/v1/runs/{run_id}/claim", claim_body)[0] == 409,
           "paused_run_cannot_dispatch")
    status, resumed = request(operator, "POST", f"/v1/runs/{run_id}/control/resume", reason, prefix + ":resume")
    expect(status == 200 and resumed.get("status") == "open" and resumed.get("task_updates") == []
           and request(operator, "POST", f"/v1/runs/{run_id}/control/resume", reason, prefix + ":resume")[1] == resumed,
           "resume_is_idempotent_without_verification_bypass")
    expect(request(worker, "POST", f"/v1/runs/{run_id}/claim", claim_body)[1].get("claim") is None,
           "resume_preserves_submitted_dependency_barrier")
    status, cancelled = request(operator, "POST", f"/v1/runs/{run_id}/control/terminate", reason, prefix + ":terminate")
    expect(status == 200 and cancelled.get("status") == "cancelled" and cancelled.get("counts_as_success") is False
           and request(operator, "POST", f"/v1/runs/{run_id}/control/terminate", reason, prefix + ":terminate")[1] == cancelled,
           "termination_is_idempotent_and_never_success")
    expect(request(operator, "POST", f"/v1/runs/{run_id}/control/resume", reason, prefix + ":terminal-resume")[0] == 409,
           "cancelled_run_cannot_be_resumed")
    status, review = request(viewer, "GET", f"/v1/runs/{run_id}/review")
    retained = [task for task in review.get("tasks", []) if task.get("id") == task_id]
    expect(status == 200 and review.get("run", {}).get("status") == "cancelled" and len(retained) == 1
           and retained[0].get("status") == "cancelled"
           and retained[0].get("artifact", {}).get("content_hash") == submitted.get("content_hash")
           and retained[0].get("artifact", {}).get("trusted_evaluation") is False,
           "cancelled_run_retains_submitted_artifact_identity")
    status, event_body = request(viewer, "GET", f"/v1/runs/{run_id}/events")
    events = event_body.get("events", [])
    operator_types = {"run.created", "workflow.plan_saved", "workflow.plan_approved", "run.paused", "run.resumed", "run.cancelled"}
    audited = [event for event in events if event.get("type") in operator_types]
    expect(status == 200 and {event.get("type") for event in audited} == operator_types
           and all(event.get("actor") == "subject:" + operator.subject_id for event in audited)
           and len([event for event in events if event.get("type") == "run.paused"]) == 1
           and any(event.get("type") == "task.output_submitted" and event.get("actor") == "worker:" + worker.subject_id for event in events),
           "audit_actors_derive_from_authenticated_subjects")
    status, integrity = request(viewer, "GET", f"/v1/runs/{run_id}/integrity")
    status2, replay = request(viewer, "GET", f"/v1/runs/{run_id}/replay")
    expect(status == 200 and integrity.get("valid") is True and status2 == 200 and replay.get("status") == "cancelled"
           and len(replay.get("tasks", {})) == 7 and all(task.get("status") == "cancelled" for task in replay["tasks"].values()),
           "event_integrity_and_cancelled_replay_match")
    return {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "passed": True,
            "version": health["version"], "api": base, "run_id": run_id,
            "passed_checks": len(checks), "checks": checks,
            "legacy_developer_control": "not_checked_no_legacy_token_input",
            "real_models_used": False, "generated_code_executed": False,
            "provider_requests": 0, "full_workflow_completed": False,
            "production_ready": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identities-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8796")
    parser.add_argument("--report", type=Path, default=Path("artifacts/operator-api-check.json"))
    args = parser.parse_args()
    target = args.report.resolve()
    private = args.identities_file.resolve()
    if target == private or target.is_relative_to(private.parent):
        parser.error("report cannot overwrite or be stored beside private identities")
    base = args.base_url.rstrip("/")
    origin = urlsplit(base)
    if (origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1", "::1"}
            or origin.username or origin.password or origin.path or origin.query or origin.fragment):
        parser.error("checks require a loopback HTTP origin")
    checks = []
    identities = ()
    try:
        identities = load_identities(args.identities_file)
        report = run_checks(base, identities, checks)
    except Exception as error:
        # Raw HTTP/auth/file exceptions can carry tokens and private paths.
        report = {"passed": False, "error_code": str(error) if type(error) is CheckFailure else "OPERATOR_HTTP_CHECK_FAILED",
                  "passed_checks": len(checks), "checks": checks, "real_models_used": False,
                  "generated_code_executed": False, "provider_requests": 0}
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if any(identity.token in raw for identity in identities):
        raw = '{"passed":false,"error_code":"REPORT_PRIVATE_VALUE_REJECTED"}\n'
        report = {"passed": False}
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(raw, encoding="utf-8")
    except OSError:
        print('{"passed":false,"error_code":"REPORT_WRITE_FAILED"}')
        return 1
    print(json.dumps(json.loads(raw), ensure_ascii=True, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
