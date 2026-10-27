"""统一预览/执行已批准节点：六种执行角色，独立验证保持待配置。"""
import argparse
import asyncio
import json
from pathlib import Path

from loom.approved_execution import ApprovedExecutionService, ReadOnlyStore
from loom.artifacts import ArtifactService
from loom.auth import load_identities
from loom.execution import LocalTaskRunner, freeze_tests
from loom.local_config import load_project_secrets
from loom.model_gateway import ModelGateway, GatewayError, load_model_config
from loom.role_execution import RoleTaskRunner
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig
from loom.store import Store, LoomError, Conflict


def load_tests(path):
    raw=Path(path).read_bytes()
    if len(raw)>262144:raise ValueError('HOST_TESTS_TOO_LARGE')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('DUPLICATE_TEST_FIELD')
            result[key]=value
        return result
    data=json.loads(raw,object_pairs_hook=pairs)
    if type(data) is not dict or set(data)!={'tests'} or type(data['tests']) is not list:
        raise ValueError('HOST_TESTS_INVALID')
    return freeze_tests([FrozenTest(**case) for case in data['tests']])[0]


async def run(args):
    readonly=ReadOnlyStore(args.db)
    with readonly.connect() as db:
        db.execute('BEGIN')
        spec=ArtifactService(readonly,initialize=False)._contract(db,args.tenant,args.run_id,args.run_id+':'+args.node_id)
    if spec['role']=='validator':raise Conflict('independent validator remains pending configuration')
    structured=spec['role'] in {'analyst','architect','tester','reviewer'}
    if structured and args.tests_file:raise ValueError('STRUCTURED_ROLE_HAS_NO_HOST_TESTS')
    if not structured and not args.tests_file:raise ValueError('FROZEN_HOST_TESTS_REQUIRED')
    tests=None if structured else load_tests(args.tests_file)
    params=(args.tenant,args.run_id,args.node_id,args.expected_plan_digest,tests)
    preview=ApprovedExecutionService(readonly).preview(*params,max_output_tokens=args.max_output_tokens)
    if not args.execute:return preview
    if args.preview_digest!=preview['preview_digest']:raise ValueError('CURRENT_PREVIEW_REQUIRED')
    if not args.identities_file or not args.secrets_file:raise ValueError('PRIVATE_CONFIGURATION_REQUIRED')
    identities=load_identities(args.identities_file)
    operators=[i for i in identities if i.tenant_id==args.tenant and 'operator' in i.roles]
    workers=[i for i in identities if i.tenant_id==args.tenant and 'worker' in i.roles]
    if len(operators)!=1 or len(workers)!=1:raise ValueError('UNAMBIGUOUS_OPERATOR_AND_WORKER_REQUIRED')
    routes,roles=load_model_config(args.model_config)
    secrets=load_project_secrets(args.secrets_file,allowed_names={r.key_env for r in routes})
    sandbox=None
    if not structured:
        if not args.sandbox_image:raise ValueError('APPROVED_IMAGE_REQUIRED')
        sandbox=DockerSandbox(SandboxConfig(args.sandbox_image))
        await asyncio.to_thread(sandbox.preflight)
    async with ModelGateway(routes,role_aliases=roles,environ=secrets) as gateway:
        readiness=gateway.readiness(preview['model_alias'])
        if not readiness.ready:raise GatewayError(readiness.reason)
        store=Store(args.db)
        claim=ApprovedExecutionService(store).claim(*params,args.preview_digest,operator=operators[0],worker=workers[0],max_output_tokens=args.max_output_tokens)
        if structured:result=await RoleTaskRunner(store,gateway).run_role(claim,max_output_tokens=args.max_output_tokens)
        else:
            runner=LocalTaskRunner(store,gateway,sandbox)
            result=await (runner.run_python_cycle(claim,tests,max_output_tokens=args.max_output_tokens) if spec.get('repair')
                          else runner.run_python(claim,tests,max_output_tokens=args.max_output_tokens))
        # The report is metadata only. Program/role bodies remain in the ledger.
        safe={k:result[k] for k in ('task_id','status','artifact_id','artifact_content_hash','content_hash','model_call_id',
            'schema_validated','model_source_recorded','hard_criteria_passed','execution_context_digest','usage',
            'usage_cost_upper_microusd','billing_uncertain','stop_reason','reason','verified') if k in result}
        safe.setdefault('verified',False)
        return dict(passed=result.get('status')=='submitted' and (structured or result.get('hard_criteria_passed') is True),
            mode='existing-plan-node-execution',run_id=args.run_id,task_id=claim.task_id,role=spec['role'],
            execution_kind=preview['execution_kind'],plan_digest=args.expected_plan_digest,execution=safe,
            creates_new_goal=False,verified=False,counts_as_success=False,full_workflow_completed=False,
            independent_second_model='pending_configuration')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('db','tenant','run-id','node-id','expected-plan-digest'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--tests-file',type=Path)
    parser.add_argument('--max-output-tokens',type=int,default=1024)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--preview-digest')
    parser.add_argument('--identities-file',type=Path)
    parser.add_argument('--secrets-file',type=Path)
    parser.add_argument('--model-config',type=Path,default=Path('configs/models.example.json'))
    parser.add_argument('--sandbox-image')
    parser.add_argument('--report',type=Path,default=Path('artifacts/approved-node.json'))
    args=parser.parse_args()
    protected=[args.db,args.tests_file,args.identities_file,args.secrets_file,args.model_config]
    if any(args.report.resolve()==Path(p).resolve() for p in protected if p):parser.error('报告不能覆盖输入文件')
    if args.secrets_file and args.report.resolve().is_relative_to(args.secrets_file.resolve().parent):parser.error('报告不能放在私有配置目录')
    try:result=asyncio.run(run(args))
    except (LoomError,OSError,ValueError,TypeError):
        result=dict(passed=False,error_code='NODE_EXECUTION_FAILED',message='节点未执行成功，请核对批准计划、输入版本、冻结判据、身份及预算。')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0 if result.get('passed') or result.get('mode')=='existing-plan-preview' else 1


if __name__=='__main__':raise SystemExit(main())
