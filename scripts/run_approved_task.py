"""预览或显式执行工作台已经批准的开发任务，不创建重复目标。"""
import argparse
import asyncio
import json
from pathlib import Path

from loom.approved_execution import ApprovedExecutionService, ReadOnlyStore
from loom.auth import load_identities
from loom.execution import LocalTaskRunner, freeze_tests
from loom.local_config import load_project_secrets
from loom.model_gateway import GatewayError, ModelGateway, load_model_config
from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig
from loom.store import Store, LoomError


def load_tests(path):
    raw=Path(path).read_bytes()
    if len(raw)>262144:raise ValueError('HOST_TESTS_TOO_LARGE')
    def pairs(items):
        data={}
        for key,value in items:
            if key in data:raise ValueError('DUPLICATE_TEST_FIELD')
            data[key]=value
        return data
    data=json.loads(raw,object_pairs_hook=pairs)
    if type(data) is not dict or set(data)!={'tests'} or type(data['tests']) is not list:
        raise ValueError('HOST_TESTS_INVALID')
    return freeze_tests([FrozenTest(**case) for case in data['tests']])[0]


async def run(args):
    tests=load_tests(args.tests_file)
    preview=ApprovedExecutionService(ReadOnlyStore(args.db)).preview(args.tenant,args.run_id,args.node_id,
        args.expected_plan_digest,tests,max_output_tokens=args.max_output_tokens)
    if not args.execute:return preview
    if args.preview_digest!=preview['preview_digest']:raise ValueError('CURRENT_PREVIEW_REQUIRED')
    if not args.identities_file or not args.secrets_file or not args.sandbox_image:
        raise ValueError('PRIVATE_CONFIGURATION_AND_IMAGE_REQUIRED')
    identities=load_identities(args.identities_file)
    operators=[i for i in identities if i.tenant_id==args.tenant and 'operator' in i.roles]
    workers=[i for i in identities if i.tenant_id==args.tenant and 'worker' in i.roles]
    if len(operators)!=1 or len(workers)!=1:raise ValueError('UNAMBIGUOUS_OPERATOR_AND_WORKER_REQUIRED')
    routes,roles=load_model_config(args.model_config)
    secrets=load_project_secrets(args.secrets_file,allowed_names={r.key_env for r in routes})
    sandbox=DockerSandbox(SandboxConfig(image=args.sandbox_image))
    await asyncio.to_thread(sandbox.preflight)
    async with ModelGateway(routes,role_aliases=roles,environ=secrets) as gateway:
        readiness=gateway.readiness(preview['model_alias'])
        if not readiness.ready:raise GatewayError(readiness.reason)
        store=Store(args.db)
        admission=ApprovedExecutionService(store)
        claim=admission.claim(args.tenant,args.run_id,args.node_id,args.expected_plan_digest,tests,
            args.preview_digest,operator=operators[0],worker=workers[0],max_output_tokens=args.max_output_tokens)
        runner=LocalTaskRunner(store,gateway,sandbox)
        spec=runner.workflows.get_task_spec(args.tenant,args.run_id,claim.task_id)
        result=await (runner.run_python_cycle(claim,tests,max_output_tokens=args.max_output_tokens)
                      if spec.get('repair') else runner.run_python(claim,tests,max_output_tokens=args.max_output_tokens))
        events=store.events(args.tenant,args.run_id)
        with store.connect() as db:
            task_calls={row[0] for row in db.execute('SELECT id FROM effects WHERE tenant=? AND run_id=? AND task_id=?',(args.tenant,args.run_id,claim.task_id))}
        intents=[e for e in events if e['type']=='tool.intent' and e['payload'].get('call_id') in task_calls]
        call_ids={e['payload']['call_id'] for e in intents}
        responses=[e for e in events if e['type']=='tool.done' and e['payload'].get('call_id') in call_ids]
        snapshot=store.snapshot(args.tenant,args.run_id)
        return dict(passed=result['status']=='submitted' and result.get('hard_criteria_passed') is True,
            mode='existing-plan-execution',run_id=args.run_id,task_id=claim.task_id,creates_new_goal=False,
            plan_digest=args.expected_plan_digest,execution=result,model_dispatch_admissions=len(intents),
            model_responses_received=len(responses),real_model_used=bool(responses),
            account_cost_upper_microusd=snapshot['spent'],account_tokens=snapshot['tokens_spent'],remaining_reservation=snapshot['reserved'],
            full_workflow_completed=False,counts_as_success=False,independent_second_model='pending_configuration')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('db','tenant','run-id','node-id','expected-plan-digest'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--tests-file',type=Path,required=True,help='本机冻结的宿主终验文件，仅含tests数组')
    parser.add_argument('--max-output-tokens',type=int,default=768)
    parser.add_argument('--execute',action='store_true',help='显式允许模型与隔离执行；默认只读预览')
    parser.add_argument('--preview-digest')
    parser.add_argument('--identities-file',type=Path)
    parser.add_argument('--secrets-file',type=Path)
    parser.add_argument('--sandbox-image')
    parser.add_argument('--model-config',type=Path,default=Path('configs/models.example.json'))
    parser.add_argument('--report',type=Path,default=Path('artifacts/approved-task.json'))
    args=parser.parse_args()
    protected=[args.db,args.tests_file,args.identities_file,args.secrets_file,args.model_config]
    if any(args.report.resolve()==Path(p).resolve() for p in protected if p):parser.error('报告不能覆盖输入文件')
    if args.secrets_file and args.report.resolve().is_relative_to(args.secrets_file.resolve().parent):parser.error('报告不能放在私有配置目录')
    try:result=asyncio.run(run(args))
    except (LoomError,ValueError,TypeError,OSError) as error:
        result=dict(passed=False,error_code=getattr(error,'code','APPROVED_EXECUTION_FAILED'),message='预览或执行未通过，请核对计划、用例、身份及当前运行状态。')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0 if result.get('passed') or result.get('mode')=='existing-plan-preview' else 1


if __name__=='__main__':raise SystemExit(main())
