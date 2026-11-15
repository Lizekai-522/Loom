"""Private local worker. A durable claimed request is never executed twice."""
import asyncio
import logging
from pathlib import Path
import time

from .auth import load_identities
from .autopilot import UNAVAILABLE,SessionService
from .dispatch import DispatchService
from .execution import LocalTaskRunner
from .executor_status import ExecutorStatus
from .local_config import load_project_secrets
from .model_gateway import ModelGateway,load_model_config
from .role_execution import RoleTaskRunner
from .sandbox import DockerSandbox,SandboxConfig,SandboxRejected,SandboxUnavailable
from .store import Conflict, InvalidInput


class LocalExecutor:
    PROBE_INTERVAL = 2.0
    SANDBOX_PROBE_SECONDS = 60.0

    def __init__(self,store,config):
        self.store,self.config=store,config
        self.identities=load_identities(config.identities_file)
        self.requests=DispatchService(store,config=config,identities=self.identities)
        self.status=ExecutorStatus(store,config,self.identities)
        self._log=logging.getLogger('loom.executor')
        self._started_at=time.time()
        self._model_probe=None
        self._sandbox_probe=None
        verification=None
        if getattr(config,'verification_config',None) is not None:
            try:
                from .verification_config import load_verification_config
                from .verification_runtime import VerificationRuntime
                verification=VerificationRuntime(store,load_verification_config(config.verification_config))
            except Exception:
                # Configured but unloadable: sessions stop instead of delivering as unverified.
                verification=UNAVAILABLE
        self.autopilot=SessionService(store,executor_config=config,identities=self.identities,verification=verification)
        self.assistant=self._assistant()

    def _assistant(self):
        """Resident assistant lanes share this process: it alone holds model/search secrets."""
        from monsoon.runtime import AssistantRuntime
        from monsoon.safety import PolicyFile
        from monsoon.web import SearchConfig,Web
        search=SearchConfig.parse(getattr(self.config,'search',None))
        policy=PolicyFile(getattr(self.config,'safety_lexicon',None))
        self.search_provider=search.provider

        def gateway():
            routes,roles=load_model_config(self.config.model_config)
            values=load_project_secrets(self.config.secrets_file,allowed_names={r.key_env for r in routes})
            return ModelGateway(routes,role_aliases=roles,environ=values)

        def web():
            # The search key lives in its own private file (relative paths resolve
            # next to the executor configuration), never in the model secrets file.
            if not search.key_env:return Web(search)
            path=Path(search.secrets_file)
            if not path.is_absolute() and getattr(self.config,'source_path',None) is not None:
                path=Path(self.config.source_path).parent/path
            values=load_project_secrets(path,allowed_names={search.key_env})
            return Web(search,secrets=dict(values))
        try:route=load_model_config(self.config.model_config)[1].get('analyst','deepseek-flash')
        except Exception:route='deepseek-flash'
        runtime=AssistantRuntime(self.store,gateway_factory=gateway,web_factory=web,sessions=self.autopilot,
                                executor_id=self.config.executor_id,route_alias=route,policy=policy)
        self._mailbox(runtime)
        return runtime

    def _mailbox(self,runtime):
        """Attach the mail lane when configured; the password is read only when a connection opens."""
        from monsoon.mail import ImapMailbox,MailConfig,MailError,SmtpSender,SECRET_NAME
        from monsoon.mail_worker import MailWorker
        config=MailConfig.parse(getattr(self.config,'mail',None))
        if config is None:return None
        owner=[i for i in self.identities if i.subject_id==config.subject and 'user' in i.roles]
        if len(owner)!=1:raise InvalidInput('mail.subject must name exactly one human user in the identities file')
        path=Path(config.secrets_file)
        if not path.is_absolute() and getattr(self.config,'source_path',None) is not None:
            path=Path(self.config.source_path).parent/path
        def password():
            try:return load_project_secrets(path,allowed_names={SECRET_NAME})[SECRET_NAME]
            except Exception:raise MailError('MAIL_SECRET_MISSING') from None
        runtime.mail=MailWorker(runtime,config,tenant=owner[0].tenant_id,
                                mailbox_factory=lambda:ImapMailbox(config,password()),sender_factory=lambda:SmtpSender(config,password()))
        return runtime.mail

    async def run_assistant(self,stop=None):
        await self.assistant.run_forever(search_provider=self.search_provider,stop=stop)

    # ----- settings probes (clickable configuration feedback) --------------------
    async def run_probes(self,stop=None):
        """Observe configuration readiness for the settings page; runs no task.

        Cheap loads (model config + secrets) re-run when their file mtime changes.
        A real bounded model call runs only when the settings page drops a
        ``.probe-request`` marker next to the executor configuration. Sandbox
        preflight re-runs when the executor configuration changes or every minute.
        Results reach the API through the heartbeat payload within one interval.
        """
        loop=asyncio.get_running_loop()
        source=getattr(self.config,'source_path',None)
        request_path=source.parent/'.probe-request' if source is not None else None
        seen={}
        sandbox_at=0.0
        while stop is None or not stop.is_set():
            await asyncio.sleep(self.PROBE_INTERVAL)
            try:
                config_mtime=source.stat().st_mtime if source is not None else None
            except OSError:
                config_mtime=None
            real_call=None
            try:
                if request_path is not None and request_path.is_file():
                    request_path.unlink(missing_ok=True)
                    real_call=await self._probe_model_call()
                    self._log.info('model real-call probe: %s',real_call.get('state'))
            except Exception as error:
                code=getattr(error,'code',None) or 'MODEL_PROBE_FAILED'
                real_call=dict(state='error',code=str(code)[:120],checked_at=time.time())
                self._log.warning('model real-call probe failed: %s',code)
            changed=False
            for key,path in (('model_config',self.config.model_config),('secrets_file',self.config.secrets_file)):
                try:current=path.stat().st_mtime_ns
                except OSError:current=None
                if seen.get(key,object())!=current:
                    changed=True
                    seen[key]=current
            if changed or self._model_probe is None:
                self._model_probe=await loop.run_in_executor(None,self._probe_model_load)
                self._log.info('model config probe: %s',self._model_probe.get('state'))
            model_probe=self._model_probe
            if real_call is not None:
                model_probe=dict(model_probe or dict(state='unknown',checked_at=time.time()),real_call=real_call)
                self._model_probe=model_probe
            now=time.time()
            if self.config.sandbox_image and (config_mtime!=seen.get('_config') or now-sandbox_at>=self.SANDBOX_PROBE_SECONDS):
                self._sandbox_probe=await loop.run_in_executor(None,self._probe_sandbox)
                sandbox_at=now
                seen['_config']=config_mtime
                self._log.info('sandbox probe: %s',self._sandbox_probe.get('state'))
            self.status.set_runtime_info(model_probe=model_probe,sandbox_probe=self._sandbox_probe,
                                         config_mtime=config_mtime,started_at=self._started_at)

    def _probe_tenant(self):
        workers=[i for i in self.identities if 'worker' in i.roles]
        return workers[0].tenant_id if workers else None

    def _probe_model_load(self):
        """Load-only probe: no network, no cost, no secret value leaves the process."""
        try:
            routes,roles=load_model_config(self.config.model_config)
            load_project_secrets(self.config.secrets_file,allowed_names={r.key_env for r in routes})
            return dict(state='ok',alias=roles.get('analyst') or (routes[0].alias if routes else None),checked_at=time.time())
        except Exception as error:
            code=getattr(error,'code',None)
            if code is None:code='PRIVATE_CONFIG_INVALID' if error.__class__.__name__=='PrivateConfigError' else 'MODEL_CONFIG_INVALID'
            return dict(state='error',code=str(code)[:120],checked_at=time.time())

    async def _probe_model_call(self):
        """One explicit bounded real model call through the budgeted ledger path."""
        tenant=self._probe_tenant()
        if tenant is None:
            return dict(state='error',code='MODEL_PROBE_NO_WORKER',checked_at=time.time())
        async with self.assistant.gateway_factory() as gateway:
            result,info=await self.assistant._call(
                gateway,tenant,purpose='model-probe',owner='settings-probe',
                operation=f'probe:{int(time.time())}',
                messages=[dict(role='user',content='请只回复两个字：就绪')],
                max_output_tokens=16,budget=1_000_000)
        return dict(state='ok',cost_microusd=info['cost_microusd'],tokens=info['tokens'],
                    detail=result.content[:60],checked_at=time.time())

    def _probe_sandbox(self):
        try:
            info=DockerSandbox(SandboxConfig(self.config.sandbox_image)).preflight()
            return dict(state='ok',image_id=info.get('image_id'),docker_version=info.get('docker_version'),checked_at=time.time())
        except SandboxUnavailable:
            return dict(state='error',code='SANDBOX_UNAVAILABLE',checked_at=time.time())
        except SandboxRejected:
            return dict(state='error',code='SANDBOX_REJECTED',checked_at=time.time())
        except Exception:
            return dict(state='error',code='SANDBOX_UNAVAILABLE',checked_at=time.time())

    def advance_sessions(self):
        """Delegated audited steps only; never calls a model or the sandbox."""
        try:return self.autopilot.tick()
        except Exception:return []

    async def drain_one(self):
        # DB-only work, kept off the event loop so heartbeats stay fresh.
        await asyncio.to_thread(self.advance_sessions)
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
            if current['role']=='validator':
                from .validator_execution import ValidatorTaskRunner
                if not self.config.sandbox_image:raise Conflict('immutable image not configured')
                sandbox=DockerSandbox(SandboxConfig(self.config.sandbox_image))
                await asyncio.to_thread(sandbox.preflight)
                claim,tests,spec=self.requests.start(tenant,run_id,request_id,operator=operator,worker=worker)
                claimed=True
                await ValidatorTaskRunner(self.store,sandbox).run(claim,tests)
                return self.requests.finish(tenant,run_id,request_id)
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
