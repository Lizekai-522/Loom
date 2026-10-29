"""Private, single-attempt review worker; only known gateway receipts are signed."""
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .approved_execution import ApprovedExecutionService
from .artifacts import ArtifactService
from .model_pricing import FLASH,PRO
from .role_execution import RoleTaskRunner
from .store import Conflict,InvalidInput,Store,canonical,digest
from .verification_signatures import GatewayReceipt,VerdictBody,VerificationBinding,sign_verdict
from .workflow import WorkflowService

class VerificationWorker:
    def __init__(self,jobs,gateway,*,directory,operator,worker,signing_keys,trusted_keys):
        self.jobs=jobs;self.gateway=gateway;self.directory=Path(directory).resolve();self.operator=operator;self.worker=worker
        self.signing_keys=signing_keys;self.trusted_keys=trusted_keys
        if (self.directory.is_relative_to(Path(__file__).resolve().parents[2]) or operator.tenant_id!=worker.tenant_id
            or 'operator' not in operator.roles or 'worker' not in worker.roles or operator.subject_id==worker.subject_id):
            raise InvalidInput('private verification workspace and separate named identities required')
        self.directory.mkdir(parents=True,exist_ok=True)
        for model in ('deepseek-flash','deepseek-v4-pro'):
            route=gateway.routes.get(model)
            if route is None or route.model!=model or route.thinking!='disabled' or not gateway.readiness(model).ready:
                raise InvalidInput('both canonical non-thinking verifier models must be configured')
            key=trusted_keys.get(model)
            if key is None or key.model_id!=model or model not in signing_keys:raise InvalidInput('model-bound verifier keys required')

    def _check_source(self,state):
        b=state['binding']
        with self.jobs.store.connect() as db:
            db.execute('BEGIN');current,_,_=self.jobs._binding_in(db,b['tenant_id'],b['run_id'],b['task_id'])
            if current.model_dump()!=b:raise Conflict('VERIFICATION_SOURCE_CHANGED')

    def _prepare_review(self,shadow,state,requirements,model):
        workflows=WorkflowService(shadow)
        definition=json.loads((Path(__file__).resolve().parents[2]/'packs/software-dev/workflow-v1.json').read_bytes())
        next(role for role in definition['roles'] if role['id']=='reviewer')['model_alias']=model
        limit=350000 if model=='deepseek-flash' else 1500000
        definition['nodes']=[dict(id='review',title=model+'独立复核',role='reviewer',
            instruction='独立核对main.py是否满足冻结的原始需求。不得执行代码。只报告明确可定位的问题；无问题用no_findings及空findings。subject逐字段照抄平台引用。用中文说明结论。',
            review_subject=state['subject'],role_output=dict(kind='review-findings',schema_version='loom.review-findings/v1'),
            limits=dict(cost_limit_microusd=limit,token_limit=1100000,max_attempts=1,context_limit_bytes=65536))]
        description=canonical(dict(original_requirements=requirements,policy='只读独立复核；不得自报签名或自行批准验收。'))
        if len(description)>32000:raise InvalidInput('verification requirements exceed limit')
        tenant=self.operator.tenant_id;prefix=state['job_id']+':'+model
        goal=workflows.create_goal(tenant,prefix+':goal',title='独立复核 '+model,description=description,
            acceptance=['report.traceable'],cost_limit=limit,token_limit=1100000)
        run=goal['run_id'];actor='subject:'+self.operator.subject_id
        plan=workflows.save_plan(tenant,run,prefix+':plan',definition,0,actor=actor)
        workflows.approve_plan(tenant,run,prefix+':approve',plan['revision'],plan['digest'],actor=actor)
        params=(tenant,run,'review',plan['digest'],None)
        admission=ApprovedExecutionService(shadow)
        preview=admission.preview(*params,max_output_tokens=1024)
        claim=admission.claim(*params,preview['preview_digest'],operator=self.operator,worker=self.worker,max_output_tokens=1024)
        return run,claim

    def _sign_known_candidate(self,shadow,state,result,model):
        artifact=ArtifactService(shadow).get_artifact(self.operator.tenant_id,result['run_id'],result['artifact_id'])
        provenance=artifact.get('model_provenance');body=artifact['body']
        if (not artifact.get('model_source_recorded') or not provenance or provenance['provider']!='deepseek'
            or provenance['model']!=model or provenance['route']!=model or body['subject']!=state['subject']
            or artifact['stale'] or body['verdict'] not in {'findings','no_findings'}):raise Conflict('VERIFICATION_MODEL_EVIDENCE_INVALID')
        key=self.trusted_keys[model]
        with self.jobs.store.connect() as db:now=int(self.jobs.store.now(db))
        receipt=GatewayReceipt(provider='deepseek',model_id=model,call_id=provenance['call_id'],request_hash=provenance['request_hash'],
            response_hash=provenance['model_result_hash'],candidate_hash=artifact['content_hash'])
        verdict=VerdictBody(key_id=key.key_id,verifier_id=key.verifier_id,binding=VerificationBinding.model_validate(state['binding']),receipt=receipt,
            passed=body['verdict']=='no_findings' and not body['findings'],issued_at=now,expires_at=now+3600)
        return sign_verdict(verdict,self.signing_keys[model])

    def _accounting(self,shadow,runs):
        if shadow is None:return 0,False
        cost=0;uncertain=False
        with shadow.connect() as db:
            for run in runs:
                row=shadow._run(db,self.operator.tenant_id,run);cost+=row['spent']
                uncertain=uncertain or bool(row['reserved'] or db.execute("SELECT 1 FROM effects WHERE tenant=? AND run_id=? AND state IN ('intent','indeterminate','partial')",(self.operator.tenant_id,run)).fetchone())
        return cost,uncertain

    async def process(self,tenant,job_id):
        if tenant!=self.operator.tenant_id:raise InvalidInput('verification tenant differs from runtime')
        state,_,requirements=self.jobs.start(tenant,job_id)
        shadow=None;runs=[];verdicts=[]
        try:
            directory=self.directory/job_id
            directory.mkdir()  # Existing workspace is never replayed or overwritten.
            path=directory/'reviews.db'
            with sqlite3.connect(Path(self.jobs.store.path).resolve().as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(path) as target:source.backup(target)
            shadow=Store(path)
            for model in ('deepseek-flash','deepseek-v4-pro'):
                self._check_source(state)
                run,claim=self._prepare_review(shadow,state,requirements,model);runs.append(run)
                (directory/'review-runs.json').write_text(json.dumps(runs),encoding='utf-8')
                result=await RoleTaskRunner(shadow,self.gateway).run_role(claim,max_output_tokens=1024);result['run_id']=run
                if result.get('status')!='submitted':raise Conflict('VERIFICATION_MODEL_RESPONSE_UNAVAILABLE')
                self._check_source(state)
                verdicts.append(self._sign_known_candidate(shadow,state,result,model))
            cost,uncertain=self._accounting(shadow,runs)
            if uncertain:raise Conflict('VERIFICATION_MODEL_BILLING_UNCERTAIN')
            trusted={key.key_id:key for key in self.trusted_keys.values()}
            result=self.jobs.finish(tenant,job_id,verdicts,trusted,actual_cost=cost)
        except Exception as error:
            cost,uncertain=self._accounting(shadow,runs)
            reason=str(error) if isinstance(error,Conflict) and str(error).startswith('VERIFICATION_') else 'VERIFICATION_REQUIRES_HUMAN_REVIEW'
            return self.jobs.block(tenant,job_id,reason=reason,actual_cost=cost,billing_uncertain=uncertain)
        if result['task_verified']:
            try:result['goal_accepted']=self.jobs.accept_goal(tenant,state['binding']['run_id'])
            except Conflict:result['goal_accepted']=False
        return result
