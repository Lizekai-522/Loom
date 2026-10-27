"""Exercise the real loopback goal/plan API without model calls or tokens in output."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from loom import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("artifacts/goal-api-check.json"))
    args = parser.parse_args()
    if args.report.resolve() == args.session.resolve():
        parser.error("report cannot overwrite private session")
    config = json.loads(args.session.read_text(encoding="utf-8-sig"))
    base = config["base_url"].rstrip("/")
    origin = urlsplit(base)
    if (origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1", "::1"}
            or origin.username or origin.password or origin.path or origin.query or origin.fragment):
        parser.error("checks require a loopback HTTP origin")
    token = config["token"]
    if not isinstance(token, str) or len(token) < 16:
        parser.error("invalid local session")
    checks = []

    def request(method, path, body=None, key=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def expect(condition, name):
        if not condition:
            raise RuntimeError(name)
        checks.append(name)

    health = request("GET", "/health", auth=False)[1]
    expect(health["version"] == __version__, "current_version_healthy")
    body = {"title": "HTTP goal/plan smoke", "description": "Verify the approved plan barrier; no model request.",
            "acceptance": ["tests.pass"], "cost_limit_microusd": 1000000, "token_limit": 10000}
    prefix = "goal-http:" + uuid4().hex
    expect(request("POST", "/v1/goals", body, prefix, auth=False)[0] == 401, "unauthenticated_goal_denied")
    status, created = request("POST", "/v1/goals", body, prefix)
    run_id = created["run_id"]
    expect(status == 201 and request("POST", "/v1/goals", body, prefix)[1]["run_id"] == run_id,
           "goal_creation_is_idempotent")
    expect(request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "before-approval"})[0] == 409,
           "draft_not_dispatchable")
    definition = json.loads((Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
    status, plan = request("PUT", f"/v1/goals/{run_id}/plan", {"expected_revision": 0, "definition": definition}, prefix + ":plan")
    expect(status == 200 and plan["revision"] == 1, "seven_role_plan_saved")
    approval = {"expected_revision": plan["revision"], "expected_digest": plan["digest"]}
    expect(request("POST", f"/v1/goals/{run_id}/plan/approve", {**approval, "expected_digest": "0" * 64}, prefix + ":bad")[0] == 409,
           "wrong_digest_denied")
    status, approved = request("POST", f"/v1/goals/{run_id}/plan/approve", approval, prefix + ":approve")
    expect(status == 200 and request("POST", f"/v1/goals/{run_id}/plan/approve", approval, prefix + ":approve")[1] == approved,
           "approval_is_idempotent")
    expect(request("PUT", f"/v1/goals/{run_id}/plan", {"expected_revision": 1, "definition": definition}, prefix + ":rewrite")[0] == 409,
           "approved_plan_is_immutable")
    expect(request("POST", f"/v1/runs/{run_id}/tasks", {"task_id": "injected", "title": "injected"}, prefix + ":inject")[0] == 409,
           "unplanned_task_injection_denied")
    status, response = request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "goal-http", "lease_seconds": 300})
    claim = response["claim"]
    expect(status == 200 and claim["task_id"] == run_id + ":spec", "only_ready_approved_root_claimed")
    status, output = request("POST", f"/v1/runs/{run_id}/tasks/{claim['task_id']}/submit",
                            {"worker_id": "goal-http", "epoch": claim["epoch"], "output": {"verified": True}}, prefix + ":submit")
    expect(status == 200 and output["status"] == "submitted"
           and request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "downstream"})[1]["claim"] is None,
           "self_attestation_does_not_open_dependencies")
    replay = request("GET", f"/v1/runs/{run_id}/replay")[1]
    integrity = request("GET", f"/v1/runs/{run_id}/integrity")[1]
    expect(len(replay["tasks"]) == 7 and replay["status"] == "open" and integrity["valid"] is True,
           "seven_task_replay_and_event_chain_valid")
    report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "passed": True,
              "version": health["version"], "api": base, "run_id": run_id,
              "passed_checks": len(checks), "checks": checks,
              "real_models_used": False, "generated_code_executed": False, "full_workflow_completed": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
