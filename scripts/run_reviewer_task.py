"""预览或执行既有批准的代码审查任务；不运行代码、不自动批准修复。"""
import argparse
import asyncio
import json
from pathlib import Path

from loom.approved_execution import ApprovedExecutionService, ReadOnlyStore
from loom.auth import load_identities
from loom.local_config import load_project_secrets
from loom.model_gateway import ModelGateway, GatewayError, load_model_config
from loom.role_execution import RoleTaskRunner
from loom.store import Store, LoomError


async def run(args):
    params = (args.tenant, args.run_id, args.node_id, args.expected_plan_digest, None)
    preview = ApprovedExecutionService(ReadOnlyStore(args.db)).preview(*params, max_output_tokens=args.max_output_tokens)
    if not args.execute:
        return preview
    if args.preview_digest != preview['preview_digest']:
        raise ValueError('CURRENT_PREVIEW_REQUIRED')
    if not args.identities_file or not args.secrets_file:
        raise ValueError('PRIVATE_CONFIGURATION_REQUIRED')
    identities = load_identities(args.identities_file)
    operators = [i for i in identities if i.tenant_id == args.tenant and 'operator' in i.roles]
    workers = [i for i in identities if i.tenant_id == args.tenant and 'worker' in i.roles]
    if len(operators) != 1 or len(workers) != 1:
        raise ValueError('UNAMBIGUOUS_OPERATOR_AND_WORKER_REQUIRED')
    routes, roles = load_model_config(args.model_config)
    secrets = load_project_secrets(args.secrets_file, allowed_names={r.key_env for r in routes})
    async with ModelGateway(routes, role_aliases=roles, environ=secrets) as gateway:
        readiness = gateway.readiness(preview['model_alias'])
        if not readiness.ready:
            raise GatewayError(readiness.reason)
        store = Store(args.db)
        claim = ApprovedExecutionService(store).claim(*params, args.preview_digest,
            operator=operators[0], worker=workers[0], max_output_tokens=args.max_output_tokens)
        result = await RoleTaskRunner(store, gateway).run_role(claim, max_output_tokens=args.max_output_tokens)
        safe = {key: result[key] for key in ('task_id', 'status', 'artifact_id', 'artifact_content_hash',
            'model_call_id', 'schema_validated', 'model_source_recorded', 'execution_context_digest',
            'usage', 'usage_cost_upper_microusd', 'billing_uncertain', 'stop_reason') if key in result}
        return dict(passed=result.get('status') == 'submitted', mode='existing-plan-reviewer-execution',
            execution=safe, creates_new_goal=False, verified=False, counts_as_success=False,
            full_workflow_completed=False, fixer_automatically_dispatched=False,
            independent_second_model='pending_configuration')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'tenant', 'run-id', 'node-id', 'expected-plan-digest'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--max-output-tokens', type=int, default=1024)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--preview-digest')
    parser.add_argument('--identities-file', type=Path)
    parser.add_argument('--secrets-file', type=Path)
    parser.add_argument('--model-config', type=Path, default=Path('configs/models.example.json'))
    parser.add_argument('--report', type=Path, default=Path('artifacts/reviewer-task.json'))
    args = parser.parse_args()
    protected = [args.db, args.identities_file, args.secrets_file, args.model_config]
    if any(args.report.resolve() == Path(p).resolve() for p in protected if p):
        parser.error('报告不能覆盖输入文件')
    if args.secrets_file and args.report.resolve().is_relative_to(args.secrets_file.resolve().parent):
        parser.error('报告不能放在私有配置目录')
    try:
        result = asyncio.run(run(args))
    except (LoomError, OSError, ValueError, TypeError):
        result = dict(passed=False, error_code='REVIEW_EXECUTION_FAILED', message='审查未通过，请核对计划、来源、身份、预算与运行状态。')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get('passed') or result.get('mode') == 'existing-plan-preview' else 1


if __name__ == '__main__':
    raise SystemExit(main())
