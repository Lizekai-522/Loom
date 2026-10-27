"""Exercise the loopback candidate/grant/preview APIs without model calls.

Only local named identity tokens are read. Reports contain code-owned labels
and identifiers, not credentials or arbitrary response/error bodies.
"""
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from loom import __version__
from loom.auth import load_identities


class CheckFailure(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def run(base, identities, checks):
    operator = next((item for item in identities if "operator" in item.roles), None)
    if operator is None:
        raise CheckFailure("NAMED_OPERATOR_REQUIRED")
    selected = {role: next((item for item in identities if item.tenant_id == operator.tenant_id
                            and role in item.roles), None) for role in ("worker", "viewer")}
    if any(item is None for item in selected.values()):
        raise CheckFailure("MATCHED_NAMED_WORKER_VIEWER_REQUIRED")
    selected["operator"] = operator
    private = [item.token.encode("ascii") for item in identities]
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(role, method, path, body=None, key=None):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if role:
            headers["Authorization"] = "Bearer " + selected[role].token
        if key:
            headers["Idempotency-Key"] = key
        req = Request(base + path, headers=headers, method=method,
                      data=None if body is None else json.dumps(body).encode("utf-8"))
        try:
            response = opener.open(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(2_097_153)
            if len(raw) > 2_097_152 or any(value in raw for value in private):
                raise CheckFailure("UNSAFE_OR_EXCESSIVE_RESPONSE")
            return response.status, json.loads(raw)

    def expect(condition, name):
        if not condition:
            raise CheckFailure(name)
        checks.append(name)

    status, health = request(None, "GET", "/health")
    expect(status == 200 and health.get("version") == __version__, "current_version_healthy")
    prefix = "artifact-http-check:" + uuid4().hex
    status, created = request("operator", "POST", "/v1/goals", {
        "title": "Artifact handoff API check (no model calls)",
        "description": "Authenticated Worker drafts and operator-approved auxiliary data; no models or code execution.",
        "acceptance": ["schema.valid", "report.traceable"], "cost_limit_microusd": 1000000,
        "token_limit": 2000000}, prefix)
    expect(status == 201, "operator_created_unexecuted_goal")
    run_id = created["run_id"]
    base_path = "/v1/runs/" + run_id
    definition = json.loads((Path(__file__).resolve().parents[1] / "configs/workflow.artifacts.example.json").read_text(encoding="utf-8"))
    status, plan = request("operator", "PUT", f"/v1/goals/{run_id}/plan",
                           {"definition": definition, "expected_revision": 0}, prefix + ":plan")
    expect(status == 200, "saved_explicit_auxiliary_selector")
    status, approved = request("operator", "POST", f"/v1/goals/{run_id}/plan/approve",
        {"expected_revision": plan["revision"], "expected_digest": plan["digest"]}, prefix + ":approve")
    expect(status == 200, "approved_exact_plan_without_releasing_dependencies")
    source, target = approved["task_ids"]["spec"], approved["task_ids"]["design"]
    worker = selected["worker"].subject_id
    status, claim_response = request("worker", "POST", base_path + "/claim", {"worker_id": worker, "lease_seconds": 90})
    expect(status == 200 and claim_response.get("claim", {}).get("task_id") == source, "named_worker_claimed_source")
    epoch = claim_response["claim"]["epoch"]
    body = {"schema_version": "loom.requirements/v1", "summary": "Structured candidate from an authenticated HTTP test worker.",
            "items": [{"id": "req-one", "text": "Keep hard dependencies and independent verification unchanged."}],
            "uncertainties": ["Independent model verification is not configured."]}
    candidate_path = base_path + f"/tasks/{source}/artifacts"
    publication = {"worker_id": worker, "epoch": epoch, "kind": "requirements", "body": body}
    expect(request("viewer", "POST", candidate_path, publication, prefix + ":viewer-publish")[0] == 403,
           "viewer_cannot_publish")
    expect(request("operator", "POST", candidate_path, publication, prefix + ":operator-publish")[0] == 403,
           "operator_cannot_publish_as_worker")
    expect(request("worker", "POST", candidate_path, {**publication, "worker_id": "another-worker"}, prefix + ":spoof")[0] == 403,
           "worker_cannot_forge_subject")
    status, artifact = request("worker", "POST", candidate_path, publication, prefix + ":candidate")
    expect(status == 201, "worker_saved_candidate")
    expect(artifact.get("origin") == "authenticated-worker-submission" and artifact.get("producer_verified") is False
           and artifact.get("counts_as_success") is False, "candidate_has_honest_unverified_origin")
    expect(request("worker", "POST", candidate_path, publication, prefix + ":candidate")[1] == artifact,
           "candidate_save_is_idempotent")
    status, listed = request("viewer", "GET", base_path + "/artifacts")
    expect(status == 200 and len(listed["items"]) == 1 and "body" not in listed["items"][0], "viewer_reads_metadata_without_body")
    expect(request("viewer", "GET", base_path + "/artifacts/" + artifact["artifact_id"])[1].get("body") == body,
           "viewer_reads_exact_typed_candidate")
    preview_path = base_path + f"/tasks/{target}/context-preview"
    status, missing = request("viewer", "GET", preview_path)
    expect(status == 200 and missing.get("ready") is False and len(missing.get("missing_inputs", [])) == 1,
           "required_input_waits_for_explicit_grant")
    approval = {"artifact_id": artifact["artifact_id"], "expected_plan_digest": plan["digest"],
                "expected_source_epoch": epoch, "expected_target_epoch": 0,
                "expected_revision": artifact["revision"], "expected_content_hash": artifact["content_hash"]}
    grant_path = base_path + f"/tasks/{target}/auxiliary-inputs"
    for role in ("worker", "viewer"):
        expect(request(role, "POST", grant_path, approval, prefix + ":bad-grant-" + role)[0] == 403, role + "_cannot_grant_input")
    expect(request("operator", "POST", grant_path, {**approval, "expected_content_hash": "0" * 64}, prefix + ":bad-hash")[0] == 409,
           "grant_requires_exact_content_hash")
    expect(request("operator", "POST", grant_path, {**approval, "expected_target_epoch": 99}, prefix + ":bad-epoch")[0] == 409,
           "grant_requires_current_target_epoch")
    status, grant = request("operator", "POST", grant_path, approval, prefix + ":grant")
    expect(status == 200 and grant.get("confers_authority") is False and grant.get("execution_connected") is False,
           "operator_grant_is_only_auxiliary_read_permission")
    expect(request("operator", "POST", grant_path, approval, prefix + ":grant")[1] == grant, "input_grant_is_idempotent")
    # A newer revision must not be silently substituted for the reviewed one.
    second_body = {**body, "summary": "Updated Worker candidate; still unverified."}
    status, second = request("worker", "POST", candidate_path, {**publication, "body": second_body}, prefix + ":candidate-v2")
    expect(status == 201 and second["revision"] == artifact["revision"] + 1, "new_content_has_new_immutable_revision")
    expect(request("viewer", "GET", base_path + "/artifacts/" + artifact["artifact_id"])[1].get("stale") is True,
           "old_revision_remains_historical")
    expect(request("viewer", "GET", preview_path)[0] == 409, "old_grant_does_not_follow_latest_revision")
    approval.update(artifact_id=second["artifact_id"], expected_revision=second["revision"], expected_content_hash=second["content_hash"])
    expect(request("operator", "POST", grant_path, approval, prefix + ":grant-v2")[0] == 200, "new_revision_requires_explicit_reapproval")
    status, submitted = request("worker", "POST", base_path + f"/tasks/{source}/submit", {
        "worker_id": worker, "epoch": epoch, "output": {"kind": "requirements", "artifact_id": second["artifact_id"]}}, prefix + ":submit")
    expect(status == 200 and submitted.get("status") == "submitted", "source_stays_submitted_unverified")
    before = request("viewer", "GET", base_path + "/events")[1]
    status, preview = request("viewer", "GET", preview_path)
    expect(status == 200 and preview.get("ready") is True and preview["items"][0]["body"] == second_body
           and preview.get("untrusted_data") is True and preview.get("execution_connected") is False,
           "preview_contains_only_explicitly_approved_untrusted_data")
    expect(len(json.dumps(preview, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) == preview["bytes"]
           and preview["bytes"] <= preview["max_bytes"], "preview_exact_bytes_are_bounded")
    expect(request("viewer", "GET", base_path + "/events")[1] == before, "preview_appends_no_events")
    status, child = request("worker", "POST", base_path + "/claim", {"worker_id": worker})
    expect(status == 200 and child.get("claim") is None, "auxiliary_grant_cannot_release_hard_dependency")
    snapshot = request("viewer", "GET", base_path)[1]
    expect([task["status"] for task in snapshot["tasks"]] == ["submitted", "pending"]
           and snapshot["spent"] == snapshot["reserved"] == 0, "task_states_and_budget_preserved")
    expect(request("viewer", "GET", base_path + "/integrity")[1].get("valid") is True, "event_chain_valid")
    return {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "passed": True, "version": health["version"],
            "run_id": run_id, "passed_checks": len(checks), "checks": checks, "provider_requests": 0,
            "real_models_used": False, "generated_code_executed": False, "execution_connected": False,
            "full_workflow_completed": False, "counts_as_success": False,
            "note": "Authenticated HTTP fixture candidates, not model outputs or independently verified results."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identities-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8796")
    parser.add_argument("--report", type=Path, default=Path("artifacts/artifact-api-check.json"))
    args = parser.parse_args()
    private_path, target = args.identities_file.resolve(), args.report.resolve()
    if target == private_path or target.is_relative_to(private_path.parent):
        parser.error("report cannot overwrite or sit beside private identities")
    base = args.base_url.rstrip("/")
    origin = urlsplit(base)
    if (origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or origin.username or origin.password or origin.path or origin.query or origin.fragment):
        parser.error("checks require a loopback HTTP origin")
    checks, identities = [], ()
    try:
        identities = load_identities(args.identities_file)
        report = run(base, identities, checks)
    except Exception as error:
        report = {"passed": False, "error_code": str(error) if type(error) is CheckFailure else "ARTIFACT_API_CHECK_FAILED",
                  "passed_checks": len(checks), "checks": checks, "provider_requests": 0, "real_models_used": False}
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if any(identity.token in raw for identity in identities):
        raw = '{"passed":false,"error_code":"PRIVATE_VALUE_REJECTED"}\n'
        report = {"passed": False}
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(raw, encoding="utf-8")
    except OSError:
        print('{"passed":false,"error_code":"REPORT_WRITE_FAILED"}')
        return 1
    print(json.dumps(json.loads(raw), ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
