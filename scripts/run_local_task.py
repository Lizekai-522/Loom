"""Run a new operator-reviewed Python task through the real local adapters.

Only --approve permits a paid request. The independent review barrier remains
closed: this adapter cannot turn passing finite tests into full verified success.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from loom.artifact_schemas import DevelopmentSuiteReference
from loom.auth import load_identities
from loom.development_suites import DevelopmentSuiteService
from loom.execution import LocalTaskRunner, freeze_tests
from loom.local_config import PrivateConfigError, load_project_secrets
from loom.model_gateway import GatewayError, ModelGateway, load_model_config
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig, SandboxError
from loom.store import LoomError, Store
from loom.workflow import WorkflowDefinition, WorkflowService


def load_job(path):
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise ValueError("TASK_INPUT_TOO_LARGE")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("TASK_DUPLICATE_FIELD")
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=pairs)
    if (not isinstance(data, dict) or not {"title", "description", "tests"} <= set(data) <= {"title", "description", "tests", "development_tests", "development_suite"}
            or not isinstance(data["title"], str) or not data["title"].strip()
            or len(data["title"]) > 500 or not isinstance(data["description"], str)
            or not data["description"].strip() or len(data["description"]) > 16000
            or not isinstance(data["tests"], list)):
        raise ValueError("TASK_INPUT_INVALID")
    tests, evaluation_digest = freeze_tests([FrozenTest(**case) for case in data["tests"]])
    development, development_digest = (), None
    if "development_tests" in data:
        if not isinstance(data["development_tests"], list):
            raise ValueError("DEVELOPMENT_TESTS_INVALID")
        development, development_digest = freeze_tests([FrozenTest(**case) for case in data["development_tests"]])
        if development_digest == evaluation_digest:
            raise ValueError("DEVELOPMENT_AND_ACCEPTANCE_MUST_DIFFER")
    if "development_suite" in data:
        if "development_tests" in data:
            raise ValueError("REVIEWED_SUITE_AND_RAW_CASES_CANNOT_MIX")
        data["development_suite"] = DevelopmentSuiteReference.model_validate(data["development_suite"]).model_dump(mode="json")
        development_digest = data["development_suite"]["development_digest"]
        if development_digest == evaluation_digest:
            raise ValueError("DEVELOPMENT_AND_ACCEPTANCE_MUST_DIFFER")
    return data, tests, evaluation_digest, development, development_digest


async def run(args):
    job, tests, evaluation_digest, development, development_digest = load_job(args.task_file)
    reference = job.get("development_suite")
    if reference is not None and not args.max_repairs:
        raise ValueError("REVIEWED_SUITE_REQUIRES_REPAIR_POLICY")
    if args.max_repairs and not development and reference is None:
        raise ValueError("PUBLIC_DEVELOPMENT_TESTS_REQUIRED")
    root = Path(__file__).resolve().parents[1]
    definition = json.loads((root / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
    definition["nodes"] = [
        {"id": "implement", "title": job["title"], "instruction": job["description"], "role": "developer",
         "depends": [], "priority": 10, "channel": "business", "evaluation_digest": evaluation_digest,
         "limits": {"cost_limit_microusd": 500000, "token_limit": 1100000, "max_attempts": 1 + args.max_repairs}},
        {"id": "independent_review", "title": "Independent acceptance review", "instruction": "Review original evidence independently before delivery.",
         "role": "validator", "depends": ["implement"], "priority": 0, "channel": "verification",
         "limits": {"cost_limit_microusd": 1000, "token_limit": 1000, "max_attempts": 1}},
    ]
    if args.max_repairs:
        definition["nodes"][0]["repair"] = {
            "schema_version": "loom.rebind/v1", "development_digest": development_digest,
            "max_repairs": args.max_repairs, "stop_on_duplicate_source": True}
    if reference is not None:
        definition["nodes"][0]["development_suite"] = reference
    definition = WorkflowDefinition.model_validate(definition)
    if not args.approve:
        return {"mode": "plan-preview", "network_requests": 0, "evaluation_digest": evaluation_digest,
                "definition": definition.model_dump(mode="json"), "requires_explicit_approve": True}
    operator = worker = None
    reviewed_store = None
    if reference is not None:
        identities_file = getattr(args, "identities_file", None)
        if identities_file is None or not Path(args.db).is_file():
            raise ValueError("REVIEWED_SUITE_IDENTITIES_AND_EXISTING_DATABASE_REQUIRED")
        identities = load_identities(identities_file)
        operator = next((i for i in identities if i.tenant_id == args.tenant and "operator" in i.roles), None)
        worker = next((i for i in identities if i.tenant_id == args.tenant and "worker" in i.roles), None)
        if operator is None or worker is None:
            raise ValueError("REVIEWED_SUITE_NAMED_OPERATOR_AND_WORKER_REQUIRED")
        reviewed_store = Store(args.db)
        suites = DevelopmentSuiteService(reviewed_store)
        with reviewed_store.connect() as db:
            db.execute("BEGIN")
            suites.resolve_reference_in(db, args.tenant, "new-execution-preview", reference)
    routes, roles = load_model_config(args.model_config)
    secrets = load_project_secrets(args.secrets_file, allowed_names={route.key_env for route in routes})
    sandbox = DockerSandbox(SandboxConfig(image=args.sandbox_image))
    # Validate daemon/image before persisting a new runnable goal or paying.
    await asyncio.to_thread(sandbox.preflight)
    async with ModelGateway(routes, role_aliases=roles, environ=secrets) as gateway:
        ready = gateway.readiness("deepseek-flash")
        if not ready.ready:
            raise GatewayError(ready.reason)
        store = reviewed_store if reviewed_store is not None else Store(args.db)
        workflows = WorkflowService(store)
        prefix = "local-execution:" + uuid4().hex
        tenant = args.tenant
        audit = {} if operator is None else {"actor": "subject:" + operator.subject_id}
        goal = workflows.create_goal(tenant, prefix, title=job["title"], description=job["description"],
                                     acceptance=["schema.valid", "tests.pass", "report.traceable"],
                                     cost_limit=1000000, token_limit=2000000, **audit)
        run_id = goal["run_id"]
        plan = workflows.save_plan(tenant, run_id, prefix + ":plan", definition, 0, **audit)
        workflows.approve_plan(tenant, run_id, prefix + ":approve", plan["revision"], plan["digest"], **audit)
        claim = store.claim_next(tenant, run_id, "local-python-worker" if worker is None else worker.subject_id, lease_seconds=90)
        runner = LocalTaskRunner(store, gateway, sandbox)
        if args.max_repairs:
            result = await runner.run_python_cycle(claim, tests, None if reference is not None else development,
                                                   max_output_tokens=args.max_output_tokens)
        else:
            result = await runner.run_python(claim, tests, max_output_tokens=args.max_output_tokens)
        child = store.claim_next(tenant, run_id, "review-check", channel="verification") if result["status"] == "submitted" else None
        chain = store.events(tenant, run_id)
        snapshot = store.snapshot(tenant, run_id)
        replay = store.replay(tenant, run_id)
        calls = {event["payload"]["call_id"] for event in chain if event["type"] == "tool.intent"}
        responses = {event["payload"]["call_id"] for event in chain if event["type"] == "tool.done"
                     and event["payload"].get("call_id") in calls}
        passed = result.get("hard_criteria_passed") is True and result["status"] == "submitted" and child is None and store.verify_events(chain)
        return {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "mode": "real-model-and-docker-local-integration", "passed": passed,
                "real_model_used": bool(responses), "model_dispatch_admissions": len(calls),
                "model_responses_received": len(responses),
                "model_dispatch_outcome": "uncertain" if calls-responses else "response_received" if responses else "not_started",
                "reviewed_suite_used": reference is not None,
                "generated_public_checks_executed": reference is not None and any(e["type"] == "development.evaluated" for e in chain),
                "full_workflow_completed": False, "counts_as_success": False,
                "run_id": run_id, "execution": result, "event_count": len(chain),
                "event_chain_valid": store.verify_events(chain), "run_status": snapshot["status"],
                "accounted_cost_upper_microusd": snapshot["spent"], "accounted_tokens": snapshot["tokens_spent"],
                "remaining_cost_reservation_microusd": snapshot["reserved"], "billed_currency_verified": False,
                "replay_matches_status": replay["status"] == snapshot["status"],
                "next_task_waits_for_verified_parent": child is None, "provider_cost_usd": None,
                "note": "Passing frozen JSON tests is recorded; independent model review and full acceptance remain pending."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--tenant", default="local-execution", help="tenant owning this approved local task; match the console identity")
    parser.add_argument("--task-file", type=Path, default=Path("configs/python-task.example.json"))
    parser.add_argument("--model-config", type=Path, default=Path("configs/models.example.json"))
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--identities-file", type=Path,
                        help="private named local operator/worker configuration, required for reviewed suites")
    parser.add_argument("--sandbox-image", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--max-repairs", type=int, default=0,
                        help="operator-approved public-check Rebind count, 0 disables, 1-2 allowed")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("artifacts/local-task.json"))
    args = parser.parse_args()
    if not 0 <= args.max_repairs <= 2:
        parser.error("repair count must be 0-2")
    if not 1 <= args.max_output_tokens <= 1024:
        parser.error("initial local task output is limited to 1-1024 tokens")
    if args.report.resolve().is_relative_to(args.secrets_file.resolve().parent):
        parser.error("report cannot be stored beside private secrets")
    if args.report.resolve() in {args.secrets_file.resolve(), args.task_file.resolve(), args.model_config.resolve(), Path(args.db).resolve()}:
        parser.error("report cannot overwrite input files or database")
    if args.identities_file is not None and args.report.resolve() == args.identities_file.resolve():
        parser.error("report cannot overwrite identities")
    try:
        report = asyncio.run(run(args))
    except (GatewayError, PrivateConfigError, SandboxError, LoomError, ValueError, TypeError, OSError) as error:
        # Deliberately omit raw exceptions, request objects and private paths.
        report = {"passed": False, "error_type": type(error).__name__, "error_code": getattr(error, "code", "LOCAL_EXECUTION_FAILED")}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("passed") or report.get("mode") == "plan-preview" else 1


if __name__ == "__main__":
    raise SystemExit(main())
