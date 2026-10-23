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
    data = json.loads(raw)
    if (not isinstance(data, dict) or set(data) != {"title", "description", "tests"}
            or not isinstance(data["title"], str) or not data["title"].strip()
            or len(data["title"]) > 500 or not isinstance(data["description"], str)
            or not data["description"].strip() or len(data["description"]) > 16000
            or not isinstance(data["tests"], list)):
        raise ValueError("TASK_INPUT_INVALID")
    tests, evaluation_digest = freeze_tests([FrozenTest(**case) for case in data["tests"]])
    return data, tests, evaluation_digest


async def run(args):
    job, tests, evaluation_digest = load_job(args.task_file)
    root = Path(__file__).resolve().parents[1]
    definition = json.loads((root / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
    definition["nodes"] = [
        {"id": "implement", "title": job["title"], "instruction": job["description"], "role": "developer",
         "depends": [], "priority": 10, "channel": "business", "evaluation_digest": evaluation_digest,
         "limits": {"cost_limit_microusd": 500000, "token_limit": 1100000, "max_attempts": 1}},
        {"id": "independent_review", "title": "Independent acceptance review", "instruction": "Review original evidence independently before delivery.",
         "role": "validator", "depends": ["implement"], "priority": 0, "channel": "verification",
         "limits": {"cost_limit_microusd": 1000, "token_limit": 1000, "max_attempts": 1}},
    ]
    definition = WorkflowDefinition.model_validate(definition)
    if not args.approve:
        return {"mode": "plan-preview", "network_requests": 0, "evaluation_digest": evaluation_digest,
                "definition": definition.model_dump(mode="json"), "requires_explicit_approve": True}
    routes, roles = load_model_config(args.model_config)
    secrets = load_project_secrets(args.secrets_file, allowed_names={route.key_env for route in routes})
    sandbox = DockerSandbox(SandboxConfig(image=args.sandbox_image))
    # Validate daemon/image before persisting a new runnable goal or paying.
    await asyncio.to_thread(sandbox.preflight)
    async with ModelGateway(routes, role_aliases=roles, environ=secrets) as gateway:
        ready = gateway.readiness("deepseek-flash")
        if not ready.ready:
            raise GatewayError(ready.reason)
        store = Store(args.db)
        workflows = WorkflowService(store)
        prefix = "local-execution:" + uuid4().hex
        goal = workflows.create_goal("local-execution", prefix, title=job["title"], description=job["description"],
                                     acceptance=["schema.valid", "tests.pass", "report.traceable"],
                                     cost_limit=1000000, token_limit=2000000)
        run_id = goal["run_id"]
        plan = workflows.save_plan("local-execution", run_id, prefix + ":plan", definition, 0)
        workflows.approve_plan("local-execution", run_id, prefix + ":approve", plan["revision"], plan["digest"])
        claim = store.claim_next("local-execution", run_id, "local-python-worker", lease_seconds=90)
        result = await LocalTaskRunner(store, gateway, sandbox).run_python(claim, tests, max_output_tokens=args.max_output_tokens)
        child = store.claim_next("local-execution", run_id, "review-check", channel="verification") if result["status"] == "submitted" else None
        chain = store.events("local-execution", run_id)
        snapshot = store.snapshot("local-execution", run_id)
        replay = store.replay("local-execution", run_id)
        passed = result.get("hard_criteria_passed") is True and result["status"] == "submitted" and child is None and store.verify_events(chain)
        return {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "mode": "real-model-and-docker-local-integration", "passed": passed,
                "real_model_used": True, "full_workflow_completed": False, "counts_as_success": False,
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
    parser.add_argument("--task-file", type=Path, default=Path("configs/python-task.example.json"))
    parser.add_argument("--model-config", type=Path, default=Path("configs/models.example.json"))
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--sandbox-image", required=True)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("artifacts/local-task.json"))
    args = parser.parse_args()
    if not 1 <= args.max_output_tokens <= 1024:
        parser.error("initial local task output is limited to 1-1024 tokens")
    if args.report.resolve() in {args.secrets_file.resolve(), args.task_file.resolve(), args.model_config.resolve(), Path(args.db).resolve()}:
        parser.error("report cannot overwrite input files or database")
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
