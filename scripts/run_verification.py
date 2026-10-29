"""预览/请求/执行受限双模型复核；未知调用不自动重试。"""
import argparse
import asyncio
import json
from pathlib import Path

from loom.approved_execution import ReadOnlyStore
from loom.local_config import load_project_secrets
from loom.model_gateway import ModelGateway,load_model_config
from loom.store import LoomError,Store
from loom.verification_config import load_verification_config
from loom.verification_jobs import VerificationJobs
from loom.verification_worker import VerificationWorker
from loom.verification_runtime import VerificationRuntime

async def drain(db,config):
    jobs=VerificationJobs(Store(db),pool_limit=config.pool_limit_microusd)
    pending=jobs.pending(config.tenant_id)
    if not pending:return None
    operator,worker=config.identities();signing,trusted=config.load_signers()
    routes,roles=load_model_config(config.model_config)
    secrets=load_project_secrets(config.model_secrets_file,allowed_names={route.key_env for route in routes})
    async with ModelGateway(routes,role_aliases=roles,environ=secrets) as gateway:
        return await VerificationWorker(jobs,gateway,directory=config.workspace,operator=operator,worker=worker,signing_keys=signing,trusted_keys=trusted).process(config.tenant_id,pending[0]['job_id'])

async def run(args):
    if not Path(args.db).is_file():raise ValueError('existing database required')
    config=load_verification_config(args.config)
    if args.mode=='drain':
        runtime=VerificationRuntime(Store(args.db),config)
        async with runtime.status.monitor():
            while True:
                runtime.check(config.tenant_id)
                result=await drain(args.db,config)
                if result is not None:print(json.dumps(result),flush=True)
                if not args.watch:return dict(passed=True,result=result,automatic_retry=False)
                await asyncio.sleep(1)
    jobs=VerificationJobs(ReadOnlyStore(args.db),pool_limit=config.pool_limit_microusd,initialize=False) if args.mode=='preview' else VerificationJobs(Store(args.db),pool_limit=config.pool_limit_microusd)
    if args.mode=='preview':return jobs.preview(config.tenant_id,args.run_id,args.task_id)
    if args.mode=='status':return jobs.state(config.tenant_id,args.job_id)
    operator,_=config.identities()
    return jobs.request(operator,args.run_id,args.task_id,args.key,expected_preview_digest=args.expected_preview_digest,reason=args.reason)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=('preview','enqueue','status','drain'))
    p.add_argument('--db',required=True);p.add_argument('--config',required=True)
    for name in ('run-id','task-id','job-id','key','expected-preview-digest','reason'):p.add_argument('--'+name)
    p.add_argument('--watch',action='store_true');a=p.parse_args()
    try:print(json.dumps(asyncio.run(run(a)),ensure_ascii=True));return 0
    except (LoomError,OSError,ValueError,TypeError):print(json.dumps(dict(passed=False,code='VERIFICATION_ACTION_FAILED',automatic_retry=False)));return 1
if __name__=='__main__':raise SystemExit(main())
