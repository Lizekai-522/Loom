"""Preview or explicitly approve one local auxiliary-input Python execution.

The named HTTP Worker publishes operator-reviewed data, not a model-produced
analysis. Only --approve permits model I/O. No independent verification is faked.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from loom import __version__
from loom.artifact_schemas import validate_body
from loom.auth import load_identities
from loom.execution import LocalTaskRunner, freeze_tests
from loom.local_config import load_project_secrets
from loom.model_gateway import ModelGateway, load_model_config
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig
from loom.store import Claim, Store, digest
from loom.workflow import WorkflowDefinition, WorkflowService


class CheckFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def load_job(path):
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise CheckFailure("CONTEXT_JOB_TOO_LARGE")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CheckFailure("CONTEXT_JOB_DUPLICATE_FIELD")
            result[key] = value
        return result
    job = json.loads(raw, object_pairs_hook=pairs)
    if (type(job) is not dict or set(job) != {"title", "description", "requirements", "tests"}
            or any(type(job[key]) is not str or not job[key].strip() or len(job[key]) > maximum
                   for key, maximum in (("title", 500), ("description", 16000)))
            or type(job["tests"]) is not list):
        raise CheckFailure("CONTEXT_JOB_INVALID")
    body = validate_body("requirements", job["requirements"])
    tests, evaluation_digest = freeze_tests([FrozenTest(**case) for case in job["tests"]])
    definition = json.loads((Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
    small = {"cost_limit_microusd": 1000, "token_limit": 1000, "max_attempts": 1}
    definition["nodes"] = [
        {"id": "spec", "title": "Operator-reviewed input", "instruction": "Publish the supplied requirements as unverified data.",
         "role": "analyst", "depends": [], "priority": 20, "limits": small},
        {"id": "implement", "title": job["title"], "instruction": "Implement the approved auxiliary requirements. Return the bounded JSON source contract.",
         "role": "developer", "depends": [], "priority": 10, "evaluation_digest": evaluation_digest,
         "limits": {"cost_limit_microusd": 500000, "token_limit": 1100000, "max_attempts": 1},
         "input_artifacts": [{"source_node": "spec", "kind": "requirements", "schema_version": "loom.requirements/v1", "required": True}]},
        {"id": "independent_review", "title": "Independent review required", "instruction": "Review original evidence independently.",
         "role": "validator", "depends": ["implement"], "channel": "verification", "limits": small}]
    return job, body, tests, WorkflowDefinition.model_validate(definition)


async def run(args, private, progress=None):
    progress = {} if progress is None else progress
    job, body, tests, definition = load_job(args.task_file)
    if not args.approve:
        return {"mode": "plan-preview", "network_requests": 0, "requires_explicit_approve": True,
                "requirements_content_hash": digest(body), "definition": definition.model_dump(mode="json"),
                "input_origin": "operator-reviewed local data, published by a named HTTP Worker only after approval",
                "note": "spec is an auxiliary data source; implement has no hard dependency on spec. Independent review still depends on implement."}
    if not Path(args.db).is_file():
        raise CheckFailure("EXISTING_API_DATABASE_REQUIRED")
    store = Store(args.db)  # Check schema/capacity compatibility before HTTP writes.
    workflows = WorkflowService(store)
    identities = load_identities(args.identities_file)
    selected = {role: next((item for item in identities if item.tenant_id == args.tenant and role in item.roles), None)
                for role in ("operator", "worker")}
    if any(value is None for value in selected.values()):
        raise CheckFailure("MATCHED_NAMED_IDENTITIES_REQUIRED")
    private.extend(item.token for item in identities)
    routes, roles = load_model_config(args.model_config)
    secrets = load_project_secrets(args.secrets_file, allowed_names={route.key_env for route in routes})
    private.extend(secrets.values())
    sandbox = DockerSandbox(SandboxConfig(image=args.sandbox_image))
    await asyncio.to_thread(sandbox.preflight)
    async with ModelGateway(routes, role_aliases=roles, environ=secrets) as gateway:
        ready = gateway.readiness("deepseek-flash")
        if not ready.ready:
            raise CheckFailure(ready.reason)
        prefix = "context-execution:" + uuid4().hex
        with httpx.Client(base_url=args.base_url, trust_env=False, follow_redirects=False, timeout=10) as client:
            def request(role, method, path, body=None, key=None):
                headers = {"Authorization": "Bearer " + selected[role].token}
                if key:
                    headers["Idempotency-Key"] = key
                response = client.request(method, path, json=body, headers=headers)
                if response.status_code not in (200, 201) or len(response.content) > 2097152:
                    raise CheckFailure("CONTEXT_HTTP_STEP_REJECTED")
                return response.json()
            health = client.get("/health").json()
            if health.get("version") != __version__:
                raise CheckFailure("CURRENT_LOCAL_API_REQUIRED")
            goal = request("operator", "POST", "/v1/goals", {"title": job["title"], "description": job["description"],
                "acceptance": ["schema.valid", "tests.pass", "report.traceable"],
                "cost_limit_microusd": 1000000, "token_limit": 2000000}, prefix)
            run_id = goal["run_id"]
            UUID(run_id)
            progress["run_id"] = run_id
            # A wrong local DB may leave one draft, never approved/running work.
            try:
                local_goal = workflows.get_goal(args.tenant, run_id)
            except Exception:
                raise CheckFailure("API_DATABASE_CONTRACT_MISMATCH") from None
            if local_goal["description"] != job["description"]:
                raise CheckFailure("API_DATABASE_CONTRACT_MISMATCH")
            goal_path = "/v1/goals/" + run_id
            base = "/v1/runs/" + run_id
            plan = request("operator", "PUT", goal_path + "/plan", {"definition": definition.model_dump(mode="json"), "expected_revision": 0}, prefix + ":plan")
            approved = request("operator", "POST", goal_path + "/plan/approve", {
                "expected_revision": plan["revision"], "expected_digest": plan["digest"]}, prefix + ":approve")
            worker = selected["worker"].subject_id
            source = request("worker", "POST", base + "/claim", {"worker_id": worker, "lease_seconds": 90})["claim"]
            if not source or source["task_id"] != approved["task_ids"]["spec"]:
                raise CheckFailure("EXPECTED_INPUT_SOURCE_CLAIM_REQUIRED")
            candidate = request("worker", "POST", base + f"/tasks/{source['task_id']}/artifacts", {
                "worker_id": worker, "epoch": source["epoch"], "kind": "requirements", "body": body}, prefix + ":candidate")
            request("worker", "POST", base + f"/tasks/{source['task_id']}/submit", {
                "worker_id": worker, "epoch": source["epoch"], "output": {"kind": "requirements", "artifact_id": candidate["artifact_id"]}}, prefix + ":input-submit")
            target = request("worker", "POST", base + "/claim", {"worker_id": worker, "lease_seconds": 90})["claim"]
            if not target or target["task_id"] != approved["task_ids"]["implement"]:
                raise CheckFailure("EXPECTED_EXECUTION_CLAIM_REQUIRED")
            request("operator", "POST", base + f"/tasks/{target['task_id']}/auxiliary-inputs", {
                "artifact_id": candidate["artifact_id"], "expected_plan_digest": plan["digest"],
                "expected_source_epoch": source["epoch"], "expected_target_epoch": target["epoch"],
                "expected_revision": candidate["revision"], "expected_content_hash": candidate["content_hash"]}, prefix + ":input-approve")
        # HTTP server and executor must point to the very same approved local DB.
        spec = workflows.get_task_spec(args.tenant, run_id, target["task_id"])
        if spec["plan_digest"] != plan["digest"]:
            raise CheckFailure("API_DATABASE_CONTRACT_MISMATCH")
        claim = Claim(**target)
        progress["execution_started"] = True
        result = await LocalTaskRunner(store, gateway, sandbox).run_python(claim, tests, max_output_tokens=args.max_output_tokens)
        chain = store.events(args.tenant, run_id)
        bound = [event["payload"] for event in chain if event["type"] == "execution.context_bound"]
        snapshot = store.snapshot(args.tenant, run_id)
        child = store.claim_next(args.tenant, run_id, worker, channel="verification") if result["status"] == "submitted" else None
        valid = store.verify_events(chain)
        calls = {event["payload"]["call_id"] for event in chain if event["type"] == "tool.intent"}
        responses = sum(event["type"] == "tool.done" and event["payload"].get("call_id") in calls for event in chain)
        passed = result.get("hard_criteria_passed") is True and result["status"] == "submitted" and child is None and valid and len(bound) == 1
        return {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "version": __version__, "passed": passed,
                "mode": "real-flash-and-docker-with-approved-auxiliary-input", "run_id": run_id,
                "real_model_used": responses > 0, "model_response_received": responses > 0,
                "model_dispatch_admissions": len(calls), "model_responses_received": responses,
                "model_dispatch_outcome": "response_received" if responses else ("uncertain" if calls else "not_started"),
                "full_workflow_completed": False, "counts_as_success": False,
                "input_producer_verified": False, "input_origin": "operator-reviewed local requirements via authenticated HTTP Worker",
                "execution": result, "context_digest": bound[0]["context_digest"] if bound else None,
                "context_item_count": bound[0]["context_item_count"] if bound else 0,
                "context_bytes": bound[0]["context_bytes"] if bound else 0,
                "event_chain_valid": valid, "next_task_waits_for_verified_parent": child is None,
                "accounted_tokens": snapshot["tokens_spent"], "accounted_cost_upper_microusd": snapshot["spent"],
                "remaining_cost_reservation_microusd": snapshot["reserved"], "billed_currency_verified": False,
                "note": "Binding is local dispatch admission; finite tests do not prove full Goal acceptance or independent verification."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-file", type=Path, default=Path("configs/python-context-task.example.json"))
    parser.add_argument("--model-config", type=Path, default=Path("configs/models.example.json"))
    parser.add_argument("--db", required=True)
    parser.add_argument("--tenant", default="first-round")
    parser.add_argument("--base-url", default="http://127.0.0.1:8796")
    parser.add_argument("--identities-file", type=Path, required=True)
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--sandbox-image", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("artifacts/context-task-preview.json"))
    args = parser.parse_args()
    origin = urlsplit(args.base_url)
    if (origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or origin.username or origin.password or origin.path or origin.query or origin.fragment):
        parser.error("local HTTP origin required")
    if not 1 <= args.max_output_tokens <= 1024:
        parser.error("output limit must be 1-1024 tokens")
    report = args.report.resolve()
    inputs = [args.task_file, args.model_config, args.identities_file, args.secrets_file, Path(args.db)]
    if any(report == path.resolve() for path in inputs) or any(report.is_relative_to(path.resolve().parent) for path in (args.identities_file, args.secrets_file)):
        parser.error("report cannot overwrite inputs or sit beside private config")
    private, progress = [], {}
    try:
        result = asyncio.run(run(args, private, progress))
    except Exception as error:
        result = {"passed": False, "error_code": error.code if type(error) is CheckFailure else "CONTEXT_EXECUTION_FAILED",
                  **progress, "model_dispatch_outcome": "unknown_after_error" if progress.get("execution_started") else "not_started"}
    raw = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if any(value and value in raw for value in private):
        result = {"passed": False, "error_code": "PRIVATE_VALUE_REJECTED"}
        raw = json.dumps(result) + "\n"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(raw, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("passed") or result.get("mode") == "plan-preview" else 1


if __name__ == "__main__":
    raise SystemExit(main())
