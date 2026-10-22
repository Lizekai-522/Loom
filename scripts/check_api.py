"""Exercise the development HTTP API using an existing private session file.

Creates a fixture run; submitted/pending is the expected verification barrier.
Never prints or copies the bearer token into the result.
"""
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("artifacts/api-check.json"))
    args = parser.parse_args()
    config = json.loads(args.session.read_text(encoding="utf-8-sig"))
    base = config["base_url"].rstrip("/")
    url = urlsplit(base)
    if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username or url.password or url.path or url.query or url.fragment):
        parser.error("development checks require a loopback HTTP origin")
    token = config["token"]
    if not isinstance(token, str) or len(token) < 16:
        parser.error("private session must contain a valid development token")
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

    def expect(condition, message):
        if not condition:
            raise RuntimeError(message)

    status, health = request("GET", "/health", auth=False)
    expect(status == 200 and health["version"] == __version__ and health["stage"] == "S1-local"
           and health["external_writes_enabled"] is False, "unexpected API version or capabilities")
    checks.append("current_version_and_local_api_healthy")
    body = {"acceptance": ["tests.pass"], "token_limit": 1000}
    expect(request("POST", "/v1/runs", body, "no-auth", auth=False)[0] == 401, "missing authentication accepted")
    checks.append("missing_auth_denied_401")
    prefix = "api-check:" + uuid4().hex
    status, created = request("POST", "/v1/runs", body, prefix)
    expect(status == 201, "run creation failed")
    run_id = created["run_id"]
    parent_id, child_id = run_id[:8] + "-parent", run_id[:8] + "-child"
    checks.append("run_created")
    status, repeated = request("POST", "/v1/runs", body, prefix)
    expect(status == 201 and repeated["run_id"] == run_id, "idempotent replay changed run")
    checks.append("same_key_same_args_returns_same_run")
    expect(request("POST", "/v1/runs", {**body, "token_limit": 1001}, prefix)[0] == 409, "idempotency mismatch accepted")
    checks.append("same_key_different_args_denied_409")
    for task_id, depends in ((parent_id, []), (child_id, [parent_id])):
        status, _ = request("POST", f"/v1/runs/{run_id}/tasks",
                            {"task_id": task_id, "title": task_id, "depends_on": depends}, prefix + ":" + task_id)
        expect(status == 201, "task creation failed")
    status, claimed = request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "api-check", "lease_seconds": 300})
    claim = claimed["claim"]
    expect(status == 200 and claim["task_id"] == parent_id, "dependent task claimed prematurely")
    checks.append("only_ready_parent_is_claimed")
    status, submitted = request("POST", f"/v1/runs/{run_id}/tasks/{parent_id}/submit",
                                {"worker_id": "api-check", "epoch": claim["epoch"],
                                 "output": {"verified": True, "message": "local API fixture"}}, prefix + ":submit")
    expect(status == 200 and submitted["status"] == "submitted", "worker self-verification accepted")
    checks.append("worker_cannot_self_verify")
    expect(request("POST", f"/v1/runs/{run_id}/claim", {"worker_id": "api-check-second"})[1]["claim"] is None,
           "child became ready before trusted verification")
    replay = request("GET", f"/v1/runs/{run_id}/replay")[1]
    expect(replay["tasks"][parent_id]["status"] == "submitted" and replay["tasks"][child_id]["status"] == "pending",
           "unexpected verification barrier state")
    checks.append("child_waits_for_trusted_verification")
    integrity = request("GET", f"/v1/runs/{run_id}/integrity")[1]
    expect(integrity["valid"] is True and integrity["external_checkpoint"] == "not_configured", "unexpected event integrity status")
    checks.append("event_chain_and_replay_valid")
    report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "passed": True,
              "version": health["version"], "stage": health["stage"], "api": base, "run_id": run_id,
              "passed_checks": len(checks), "checks": checks, "tasks": replay["tasks"], "integrity": integrity,
              "expected_run_state": "open", "real_models_used": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "version": health["version"], "passed_checks": len(checks),
                      "run_id": run_id, "parent": "submitted", "child": "pending"}, indent=2))


if __name__ == "__main__":
    main()
