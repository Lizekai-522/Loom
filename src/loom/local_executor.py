"""Private local worker. A durable claimed request is never executed twice."""
import asyncio

from .auth import load_identities
from .dispatch import DispatchService
from .execution import LocalTaskRunner
from .executor_status import ExecutorStatus
from .local_config import load_project_secrets
from .model_gateway import ModelGateway,load_model_config
from .role_execution import RoleTaskRunner
from .sandbox import DockerSandbox,SandboxConfig
from .store import Conflict


class LocalExecutor:
    def __init__(self,store,config):
        self.store,self.config=store,config
        self.identities=load_identities(config.identities_file)
        self.requests=DispatchService(store,config=config,identities=self.identities)
        self.status=ExecutorStatus(store,config,self.identities)

    async def drain_one(self):
        states=self.requests.pending()
        # A previous process may have committed output before saving its request
        # receipt. Recover only from the ledger, never by repeating a model call.
        for state in states:
            if state['state']=='running':self.requests.finish(state['tenant_id'],state['run_id'],state['request_id'],recover=True)
        current=next((s for s in states if s['state']=='queued'),None)
        if current is None:return None
        tenant,run_id,request_id=current['tenant_id'],current['run_id'],current['request_id']
        claimed=False
        try:
            operator=self.requests._identity(tenant,'operator',current['operator_id'])
            worker=self.requests._identity(tenant,'worker',current['worker_id'])
            with self.store.connect() as db:spec=self.requests.artifacts._contract(db,tenant,run_id,current['task_id'])
            if self.config.binding(spec['role'],spec['evaluation_digest'])!=current['runtime_binding']:raise Conflict('local policy changed')
            routes,roles=load_model_config(self.config.model_config)
            if self.config.binding(spec['role'],spec['evaluation_digest'])!=current['runtime_binding']:raise Conflict('local policy changed')
            secrets=load_project_secrets(self.config.secrets_file,allowed_names={r.key_env for r in routes})
            sandbox=None
            if current['role'] in {'developer','fixer'}:
                if not self.config.sandbox_image:raise Conflict('immutable image not configured')
                sandbox=DockerSandbox(SandboxConfig(self.config.sandbox_image))
                await asyncio.to_thread(sandbox.preflight)
            async with ModelGateway(routes,role_aliases=roles,environ=secrets) as gateway:
                with self.store.connect() as db:
                    spec=self.requests.artifacts._contract(db,tenant,run_id,current['task_id'])
                if not gateway.readiness(spec['model_alias']).ready:raise Conflict('model route is unavailable')
                claim,tests,spec=self.requests.start(tenant,run_id,request_id,operator=operator,worker=worker)
                claimed=True
                if current['role'] in {'analyst','architect','tester','reviewer'}:
                    await RoleTaskRunner(self.store,gateway).run_role(claim,max_output_tokens=1024)
                else:
                    runner=LocalTaskRunner(self.store,gateway,sandbox)
                    await (runner.run_python_cycle(claim,tests,max_output_tokens=1024) if spec.get('repair') else runner.run_python(claim,tests,max_output_tokens=1024))
            return self.requests.finish(tenant,run_id,request_id)
        except Exception:
            # Fixed metadata only: exception messages can contain private paths
            # or provider data. Both preflight and in-flight failures are fenced.
            if claimed:return self.requests.finish(tenant,run_id,request_id)
            return self.requests.block_queued(tenant,run_id,request_id)
