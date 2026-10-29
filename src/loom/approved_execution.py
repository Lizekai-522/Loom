"""Preview and claim one existing, approved workbench task; never create a Goal."""
from pathlib import Path
import json
import sqlite3

from .artifacts import ArtifactService, _hash
from .artifact_schemas import ARTIFACT_ROLES
from .auth import LocalIdentity
from .development_suites import DevelopmentSuiteService
from .execution import freeze_tests
from .model_pricing import role_tariff
from .store import Claim, Conflict, InvalidInput, Store, _Connection, digest


class ReadOnlyStore(Store):
    def __init__(self, path):
        supplied=Path(path)
        if not supplied.is_file():
            raise InvalidInput('existing database required')
        self.path=str(supplied.resolve())

    def connect(self):
        db=sqlite3.connect(Path(self.path).as_uri()+'?mode=ro',uri=True,factory=_Connection,isolation_level=None,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db


class ApprovedExecutionService:
    def __init__(self,store):
        self.store=store
        self.artifacts=ArtifactService(store,initialize=False)
        self.suites=DevelopmentSuiteService(store,artifacts=self.artifacts,initialize=False)

    def _check_in(self,db,tenant,run_id,node_id,plan_digest,tests,max_output_tokens):
        _hash(plan_digest)
        if type(max_output_tokens) is not int or not 1<=max_output_tokens<=1024:
            raise InvalidInput('output limit must be 1-1024 tokens')
        task_id=run_id+':'+node_id
        run=self.store._run(db,tenant,run_id)
        spec=self.artifacts._contract(db,tenant,run_id,task_id)
        role_data=spec['role'] in {'analyst','architect','tester'}
        reviewer=spec['role']=='reviewer'
        structured=role_data or reviewer
        if structured:
            if tests is not None:raise Conflict('structured role execution does not accept host tests')
            tests,evaluation_digest=(),None
        else:
            tests,evaluation_digest=freeze_tests(tests)
        if spec['plan_digest']!=plan_digest or spec['evaluation_digest']!=evaluation_digest:
            raise Conflict('approved plan or frozen host tests differ')
        review_fixer=spec['role']=='fixer' and spec.get('review_subject') is not None
        if run['status']!='open':raise Conflict('execution requires an open run')
        if role_data:
            output=spec.get('role_output') or {}
            if (ARTIFACT_ROLES.get(output.get('kind'))!=spec['role'] or spec.get('review_subject')
                    or not {'artifacts.read','artifacts.write'}<=set(spec['tools'])):
                raise Conflict('structured role requires an explicit typed output contract')
        elif reviewer:
            if (not spec.get('review_subject') or (spec.get('role_output') or {}).get('kind')!='review-findings'
                    or spec.get('input_artifacts') or not {'artifacts.read','artifacts.write'}<=set(spec['tools'])):
                raise Conflict('reviewer requires an explicit subject and findings contract')
        elif review_fixer:
            selectors=spec.get('input_artifacts') or []
            if (len(selectors)!=1 or selectors[0]['kind']!='review-findings' or not selectors[0]['required']
                    or not {'workspace.write','tests.run','artifacts.read'}<=set(spec['tools'])):
                raise Conflict('review fixer requires one explicitly approved findings input')
        elif spec['role']=='developer' and not spec.get('development_suite') and not spec.get('repair'):
            if not {'workspace.write','tests.run'}<=set(spec['tools']):
                raise Conflict('developer requires explicit executable capabilities')
        else:
            if (spec['role'] not in {'developer','fixer'} or not spec.get('development_suite')
                    or not spec.get('repair') or spec.get('input_artifacts')
                    or not {'workspace.write','tests.run','artifacts.read'}<=set(spec['tools'])):
                raise Conflict('only open reviewed-suite execution tasks without auxiliary selectors are supported')
            self.suites.resolve_reference_in(db,tenant,run_id,spec['development_suite'])
        task=db.execute('SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?',(tenant,run_id,task_id)).fetchone()
        if task['status']!='pending' or task['epoch']!=0:
            raise Conflict('task already started; no automatic repeat or recovery')
        done={row[0] for row in db.execute("SELECT id FROM tasks WHERE tenant=? AND run_id=? AND status='done'",(tenant,run_id))}
        if not set(json.loads(task['depends']))<=done:
            raise Conflict('hard dependencies remain blocked')
        if db.execute("SELECT 1 FROM effects WHERE tenant=? AND run_id=? AND (task_id=? OR state IN ('intent','indeterminate','partial'))",(tenant,run_id,task_id)).fetchone():
            raise Conflict('prior or unknown effect requires reconciliation')
        if db.execute('SELECT 1 FROM reservations WHERE tenant=? AND task_id=?',(tenant,task_id)).fetchone():
            raise Conflict('prior task reservation requires reconciliation')
        approval=db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? ORDER BY seq",(tenant,run_id)).fetchall()
        approvals=[json.loads(row[0]) for row in approval if json.loads(row[0])['type']=='workflow.plan_approved']
        if not approvals or not approvals[-1]['actor'].startswith('subject:'):
            raise Conflict('named operator plan approval required')
        review_fields={}
        if spec.get('review_subject') is not None or spec.get('input_artifacts'):
            context=self.artifacts._preview_context_in(db,tenant,run_id,task_id)
            if not context['ready']:raise Conflict('review findings require explicit operator input approval')
            review_fields=dict(input_context_digest=context['context_digest'],
                input_grants=[{key:item[key] for key in ('artifact_id','grant_id','source_epoch','revision','content_hash','approval_event_hash')}
                              for item in context['items']])
            if spec.get('review_subject') is not None:review_fields['review_subject']=spec['review_subject']
        tariff=role_tariff(spec)
        cost=tariff.cost(tariff.context_limit,max_output_tokens)
        tokens=tariff.context_limit+max_output_tokens
        limits=spec['limits']
        if (limits['cost_limit_microusd']<cost or limits['token_limit']<tokens
                or run['cost_limit']-run['spent']-run['reserved']<cost
                or run['token_limit']-run['tokens_spent']-run['tokens_reserved']<tokens):
            raise Conflict('approved task or run budget cannot cover initial reservation')
        report=dict(schema_version='loom.approved-execution-preview/v1',mode='existing-plan-preview',
            tenant_id=tenant,run_id=run_id,node_id=node_id,task_id=task_id,plan_digest=plan_digest,
            evaluation_digest=evaluation_digest,host_case_count=len(tests),development_suite=spec.get('development_suite'),
            model_alias=spec['model_alias'],role=spec['role'],execution_kind='structured-data' if structured else 'python-json',
            task_limits=limits,max_output_tokens=max_output_tokens,initial_cost_reservation_upper=cost,
            initial_token_reservation=tokens,run_cost_available=run['cost_limit']-run['spent']-run['reserved'],
            run_tokens_available=run['token_limit']-run['tokens_spent']-run['tokens_reserved'],
            creates_new_goal=False,network_requests=0,requires_explicit_execute=True,
            full_workflow_completed=False,counts_as_success=False,**review_fields)
        return spec,{**report,'preview_digest':digest(report)}

    def preview(self,tenant,run_id,node_id,plan_digest,tests,*,max_output_tokens=768):
        with self.store.connect() as db:
            db.execute('BEGIN')
            return self._check_in(db,tenant,run_id,node_id,plan_digest,tests,max_output_tokens)[1]

    def claim(self,tenant,run_id,node_id,plan_digest,tests,preview_digest,*,operator,worker,max_output_tokens=768):
        with self.store.transaction() as db:
            return self._claim_in(db,tenant,run_id,node_id,plan_digest,tests,preview_digest,
                operator=operator,worker=worker,max_output_tokens=max_output_tokens)

    def _claim_in(self,db,tenant,run_id,node_id,plan_digest,tests,preview_digest,*,operator,worker,max_output_tokens=768):
        _hash(preview_digest)
        if (not isinstance(operator,LocalIdentity) or not isinstance(worker,LocalIdentity)
                or operator.tenant_id!=tenant or worker.tenant_id!=tenant
                or 'operator' not in operator.roles or 'worker' not in worker.roles):
            raise InvalidInput('registered same-tenant operator and worker required')
        spec,current=self._check_in(db,tenant,run_id,node_id,plan_digest,tests,max_output_tokens)
        if current['preview_digest']!=preview_digest:
            raise Conflict('execution does not match the current preview')
        total=db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
        business=db.execute("SELECT count(*) FROM tasks WHERE status='running' AND channel='business'").fetchone()[0]
        if total>=self.store.max_workers or business>=self.store.max_workers-self.store.verify_reserve:
            raise Conflict('local execution capacity is full')
        task_id=current['task_id']
        task=db.execute('SELECT * FROM tasks WHERE tenant=? AND id=?',(tenant,task_id)).fetchone()
        until=self.store.now(db)+90
        db.execute("UPDATE tasks SET status='running',worker=?,epoch=1,lease_until=? WHERE tenant=? AND id=?",(worker.subject_id,until,tenant,task_id))
        self.store._event(db,tenant,run_id,'task.claimed',dict(task_id=task_id,worker=worker.subject_id,
            epoch=1,lease_until=until,channel=task['channel'],priority=task['priority']),'system')
        for item in current.get('input_grants',[]):
            self.artifacts._approve_input_in(db,tenant=tenant,run_id=run_id,target_task_id=task_id,
                artifact_id=item['artifact_id'],key='execution-input:'+task_id+':1',actor='subject:'+operator.subject_id,
                expected_plan_digest=plan_digest,expected_source_epoch=item['source_epoch'],expected_target_epoch=1,
                expected_revision=item['revision'],expected_content_hash=item['content_hash'])
        self.store._event(db,tenant,run_id,'execution.existing_plan_admitted',dict(task_id=task_id,
            plan_digest=plan_digest,evaluation_digest=current['evaluation_digest'],preview_digest=preview_digest,
            operator=operator.subject_id,worker=worker.subject_id),'subject:'+operator.subject_id)
        return Claim(tenant,run_id,task_id,worker.subject_id,1,until)
