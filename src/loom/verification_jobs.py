"""Durable verification requests and exact cryptographic result consumption.

Generation keeps its original Goal budget. Independent reviews use a separate,
operator-configured project pool and private review ledgers; public callers
cannot change that pool, upload signatures, or mark their own output verified.
"""
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .artifacts import ArtifactService
from .auth import LocalIdentity,Principal
from .operator import _text
from .policy import ArtifactSubmission
from .review_inputs import ReviewInputService
from .store import Claim,Conflict,InvalidInput,NotFound,canonical,digest
from .verification_signatures import VerificationBinding,verify_pair

SCHEMA='''
CREATE TABLE IF NOT EXISTS verification_requests (
 tenant TEXT NOT NULL,id TEXT NOT NULL,run_id TEXT NOT NULL,task_id TEXT NOT NULL,epoch INTEGER NOT NULL,
 command_key TEXT NOT NULL,record TEXT NOT NULL,event_id TEXT NOT NULL,event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,id),UNIQUE(tenant,command_key),UNIQUE(tenant,task_id,epoch),FOREIGN KEY(event_id) REFERENCES events(id));
CREATE TABLE IF NOT EXISTS verification_attestations (
 tenant TEXT NOT NULL,job_id TEXT NOT NULL,model_id TEXT NOT NULL,record TEXT NOT NULL,event_id TEXT NOT NULL,event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,job_id,model_id),FOREIGN KEY(event_id) REFERENCES events(id));
CREATE TRIGGER IF NOT EXISTS verification_requests_no_update BEFORE UPDATE ON verification_requests
 BEGIN SELECT RAISE(ABORT,'verification requests are append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_requests_no_delete BEFORE DELETE ON verification_requests
 BEGIN SELECT RAISE(ABORT,'verification requests are append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_attestations_no_update BEFORE UPDATE ON verification_attestations
 BEGIN SELECT RAISE(ABORT,'verification attestations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS verification_attestations_no_delete BEFORE DELETE ON verification_attestations
 BEGIN SELECT RAISE(ABORT,'verification attestations are append-only'); END;
'''

class VerificationJobs:
    MAX_RESERVATION=1850000
    def __init__(self,store,*,pool_limit=2000000,initialize=True):
        if type(pool_limit) is not int or not 0<=pool_limit<=10000000:raise InvalidInput('invalid verification project pool')
        self.store=store;self.pool_limit=pool_limit;self.reviews=ReviewInputService(store)
        if initialize:
            with store.transaction() as db:
                statement=''
                for line in SCHEMA.splitlines(True):
                    statement+=line
                    if sqlite3.complete_statement(statement):db.execute(statement);statement=''

    def _binding_in(self,db,tenant,run_id,task_id):
        run=self.store._run(db,tenant,run_id)
        if run['status']!='open':raise Conflict('VERIFICATION_RUN_NOT_OPEN')
        view=self.reviews._preview_in(db,tenant,run_id,task_id,True)
        if view['execution_summary']['hard_criteria_passed'] is not True:raise Conflict('VERIFICATION_HOST_TESTS_NOT_PASSED')
        if db.execute("SELECT 1 FROM reservations WHERE tenant=? AND task_id=? AND state='reserved'",(tenant,task_id)).fetchone():
            raise Conflict('VERIFICATION_SOURCE_BILLING_UNSETTLED')
        spec=self.reviews.artifacts._contract(db,tenant,run_id,task_id)
        goal=db.execute('SELECT description FROM workflow_goals WHERE tenant=? AND run_id=?',(tenant,run_id)).fetchone()
        requirements=dict(description=goal['description'] if goal else '',instruction=spec['instruction'],acceptance=json.loads(
            db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? AND seq=1",(tenant,run_id)).fetchone()[0])['payload']['acceptance'])
        ref=view['subject']
        binding=VerificationBinding(tenant_id=tenant,run_id=run_id,task_id=task_id,epoch=ref['epoch'],
            plan_digest=ref['plan_digest'],content_hash=ref['content_hash'],ruleset_version=run['ruleset'],
            evaluation_digest=ref['evaluation_digest'],evaluation_event_hash=ref['evaluation_event_hash'],
            submission_event_hash=ref['submission_event_hash'],input_digest=digest(dict(review_input=view['input_digest'],requirements=requirements)))
        return binding,view,requirements

    def _available(self,db):return db.execute("SELECT 1 FROM sqlite_master WHERE name='verification_requests'").fetchone() is not None

    def _record(self,db,row):
        try:
            record=json.loads(row['record']);binding=VerificationBinding.model_validate(record['binding'])
            if (record['job_id']!=row['id'] or binding.tenant_id!=row['tenant'] or binding.run_id!=row['run_id']
                or binding.task_id!=row['task_id'] or binding.epoch!=row['epoch'] or record['command_key']!=row['command_key']
                or record['max_cost_microusd']!=self.MAX_RESERVATION):raise ValueError
            event=db.execute('SELECT tenant,run_id,hash FROM events WHERE id=?',(row['event_id'],)).fetchone()
            if event is None or (event['tenant'],event['run_id'],event['hash'])!=(row['tenant'],row['run_id'],row['event_hash']):raise ValueError
            self.reviews.artifacts._event_matches(db,row['tenant'],row['run_id'],row['event_hash'],'verification.requested',record['actor'],dict(record=record,record_digest=digest(record),counts_as_success=False))
            return record
        except (ValueError,KeyError,TypeError):raise Conflict('VERIFICATION_LEDGER_INVALID') from None

    def _row(self,db,tenant,job_id):
        row=db.execute('SELECT * FROM verification_requests WHERE tenant=? AND id=?',(tenant,job_id)).fetchone()
        if row is None:raise NotFound('verification request not found')
        return row

    def _state(self,db,row):
        record=self._record(db,row);state=dict(state='queued',actual_cost_microusd=0,billing_uncertain=False,reason=None,counts_as_success=False)
        for event in self.reviews._events_in(db,row['tenant'],row['run_id']):
            if event['type'] in {'verification.started','verification.finished','verification.blocked'} and event['payload'].get('job_id')==row['id']:
                if event['actor']!='verification-engine' or event['payload'].get('binding_digest')!=digest(record['binding']):raise Conflict('VERIFICATION_LEDGER_INVALID')
                state=event['payload']
        if (state['state'] not in {'queued','running','verified','rejected','blocked'} or type(state['actual_cost_microusd']) is not int
            or state['actual_cost_microusd']<0 or type(state['billing_uncertain']) is not bool):raise Conflict('VERIFICATION_LEDGER_INVALID')
        return {**record,**state}

    def _pool(self,db,tenant):
        spent=held=0
        if self._available(db):
            for row in db.execute('SELECT * FROM verification_requests WHERE tenant=?',(tenant,)):
                state=self._state(db,row);spent+=state['actual_cost_microusd']
                if state['state']=='running' or state['billing_uncertain']:held+=state['max_cost_microusd']
        return dict(limit_microusd=self.pool_limit,spent_microusd=spent,reserved_microusd=held)

    def preview(self,tenant,run_id,task_id):
        with self.store.connect() as db:
            db.execute('BEGIN');binding,view,_=self._binding_in(db,tenant,run_id,task_id)
            task=db.execute('SELECT status FROM tasks WHERE tenant=? AND id=?',(tenant,task_id)).fetchone()
            existing=[] if not self._available(db) else [self._state(db,row) for row in db.execute('SELECT * FROM verification_requests WHERE tenant=? AND task_id=?',(tenant,task_id))]
            result=dict(schema_version='loom.verification-preview/v1',binding=binding.model_dump(),subject=view['subject'],
                ready=task['status']=='submitted' and not existing,existing_request=existing[-1] if existing else None,
                models=['deepseek-flash','deepseek-v4-pro'],max_cost_microusd=self.MAX_RESERVATION,project_pool=self._pool(db,tenant),
                generation_budget_unchanged=True,requires_explicit_request=True,model_dispatched=False,counts_as_success=False)
            return {**result,'preview_digest':digest(result)}

    def request(self,identity,run_id,task_id,key,*,expected_preview_digest,reason):
        if not isinstance(identity,(LocalIdentity,Principal)) or 'operator' not in identity.roles:raise InvalidInput('named operator required')
        _text(key,200,'command key');_text(reason,2000,'reason')
        args=dict(run_id=run_id,task_id=task_id,actor='subject:'+identity.subject_id,expected_preview_digest=expected_preview_digest,reason=reason)
        def apply(db):
            binding,view,_=self._binding_in(db,identity.tenant_id,run_id,task_id)
            task=db.execute('SELECT status FROM tasks WHERE tenant=? AND id=?',(identity.tenant_id,task_id)).fetchone()
            existing=[self._state(db,row) for row in db.execute('SELECT * FROM verification_requests WHERE tenant=? AND task_id=?',(identity.tenant_id,task_id))]
            preview=dict(schema_version='loom.verification-preview/v1',binding=binding.model_dump(),subject=view['subject'],ready=task['status']=='submitted' and not existing,
                existing_request=existing[-1] if existing else None,models=['deepseek-flash','deepseek-v4-pro'],max_cost_microusd=self.MAX_RESERVATION,
                project_pool=self._pool(db,identity.tenant_id),generation_budget_unchanged=True,requires_explicit_request=True,model_dispatched=False,counts_as_success=False)
            if digest(preview)!=expected_preview_digest or not preview['ready']:raise Conflict('VERIFICATION_PREVIEW_CHANGED')
            record=dict(job_id=str(uuid4()),binding=binding.model_dump(),subject=view['subject'],actor=args['actor'],command_key=key,reason=reason,max_cost_microusd=self.MAX_RESERVATION)
            event=self.store._event(db,identity.tenant_id,run_id,'verification.requested',dict(record=record,record_digest=digest(record),counts_as_success=False),args['actor'],key)
            db.execute('INSERT INTO verification_requests VALUES(?,?,?,?,?,?,?,?,?)',(identity.tenant_id,record['job_id'],run_id,task_id,binding.epoch,key,canonical(record),event['event_id'],event['hash']))
            return dict(job_id=record['job_id'],state='queued',counts_as_success=False,model_dispatched=False)
        return self.store._command(identity.tenant_id,key,'verification.request',args,apply)

    def state(self,tenant,job_id):
        with self.store.connect() as db:db.execute('BEGIN');return self._state(db,self._row(db,tenant,job_id))

    def attestations(self,tenant,job_id):
        with self.store.connect() as db:
            db.execute('BEGIN');row=self._row(db,tenant,job_id);state=self._state(db,row);result=[]
            for item in db.execute('SELECT * FROM verification_attestations WHERE tenant=? AND job_id=?',(tenant,job_id)):
                try:
                    signed=json.loads(item['record']);event=db.execute('SELECT hash FROM events WHERE id=?',(item['event_id'],)).fetchone()
                    if event is None or event['hash']!=item['event_hash'] or signed['body']['binding']!=state['binding'] or signed['body']['receipt']['model_id']!=item['model_id']:raise ValueError
                    self.reviews.artifacts._event_matches(db,tenant,row['run_id'],item['event_hash'],'verification.attested','verification-engine',
                        dict(job_id=job_id,attestation=signed,attestation_digest=digest(signed)))
                    result.append(signed)
                except (ValueError,KeyError,TypeError):raise Conflict('VERIFICATION_LEDGER_INVALID') from None
            return result

    def pending(self,tenant):
        with self.store.connect() as db:
            db.execute('BEGIN')
            return [state for row in db.execute('SELECT * FROM verification_requests WHERE tenant=? ORDER BY rowid',(tenant,)) if (state:=self._state(db,row))['state']=='queued']

    def start(self,tenant,job_id):
        with self.store.transaction() as db:
            row=self._row(db,tenant,job_id);state=self._state(db,row)
            if state['state']!='queued':raise Conflict('VERIFICATION_NO_AUTOMATIC_RETRY')
            binding,view,requirements=self._binding_in(db,tenant,row['run_id'],row['task_id'])
            if binding.model_dump()!=state['binding']:raise Conflict('VERIFICATION_SOURCE_CHANGED')
            pool=self._pool(db,tenant)
            if pool['spent_microusd']+pool['reserved_microusd']+self.MAX_RESERVATION>self.pool_limit:raise Conflict('VERIFICATION_PROJECT_BUDGET_EXHAUSTED')
            data=dict(job_id=job_id,binding_digest=digest(state['binding']),state='running',actual_cost_microusd=0,billing_uncertain=False,reason=None,counts_as_success=False)
            self.store._event(db,tenant,row['run_id'],'verification.started',data,'verification-engine')
            return state,view,requirements

    def block_pending(self,tenant,job_id,reason):
        """A preflight failure is durable and never takes over a started call."""
        if not isinstance(reason,str) or not reason.startswith('VERIFICATION_') or len(reason)>200:raise InvalidInput('invalid verification reason')
        with self.store.transaction() as db:
            row=self._row(db,tenant,job_id);state=self._state(db,row)
            if state['state']!='queued':raise Conflict('VERIFICATION_NO_AUTOMATIC_RETRY')
            result=dict(job_id=job_id,binding_digest=digest(state['binding']),state='blocked',actual_cost_microusd=0,billing_uncertain=False,reason=reason,counts_as_success=False)
            self.store._event(db,tenant,row['run_id'],'verification.blocked',result,'verification-engine')
            return result

    def block(self,tenant,job_id,*,reason,actual_cost,billing_uncertain):
        if type(actual_cost) is not int or actual_cost<0 or type(billing_uncertain) is not bool:raise InvalidInput('invalid verification accounting')
        with self.store.transaction() as db:
            row=self._row(db,tenant,job_id);state=self._state(db,row)
            if state['state']!='running':raise Conflict('verification request is not running')
            data=dict(job_id=job_id,binding_digest=digest(state['binding']),state='blocked',actual_cost_microusd=actual_cost,
                billing_uncertain=billing_uncertain,reason=reason,counts_as_success=False)
            self.store._event(db,tenant,row['run_id'],'verification.blocked',data,'verification-engine')
            return data

    def finish(self,tenant,job_id,verdicts,trusted_keys,*,actual_cost):
        if type(actual_cost) is not int or not 0<=actual_cost<=self.MAX_RESERVATION:raise InvalidInput('invalid verification accounting')
        with self.store.transaction() as db:
            row=self._row(db,tenant,job_id);state=self._state(db,row)
            if state['state']!='running':raise Conflict('VERIFICATION_NO_AUTOMATIC_RETRY')
            binding,view,_=self._binding_in(db,tenant,row['run_id'],row['task_id'])
            if binding.model_dump()!=state['binding']:raise Conflict('VERIFICATION_SOURCE_CHANGED')
            signatures=verify_pair(verdicts,binding,trusted_keys,now=int(self.store.now(db)))
            for signed in verdicts:
                model=signed['body']['receipt']['model_id']
                event=self.store._event(db,tenant,row['run_id'],'verification.attested',dict(job_id=job_id,attestation=signed,attestation_digest=digest(signed)),'verification-engine')
                db.execute('INSERT INTO verification_attestations VALUES(?,?,?,?,?,?)',(tenant,job_id,model,canonical(signed),event['event_id'],event['hash']))
            passed=all(signature.passed for signature in signatures)
            if passed:
                task=db.execute('SELECT * FROM tasks WHERE tenant=? AND id=?',(tenant,row['task_id'])).fetchone()
                claim=Claim(tenant,row['run_id'],row['task_id'],task['worker'],task['epoch'],0)
                self.store._complete_in(db,claim,'verification:'+job_id,json.loads(task['output']),ArtifactSubmission(
                    binding.content_hash,binding.ruleset_version,f'{binding.task_id}:{binding.epoch}',
                    'verification://'+job_id,True,True,'signed',signatures))
            else:
                db.execute("UPDATE runs SET status='awaiting_human' WHERE tenant=? AND id=? AND status='open'",(tenant,row['run_id']))
            result=dict(job_id=job_id,binding_digest=digest(state['binding']),state='verified' if passed else 'rejected',actual_cost_microusd=actual_cost,
                billing_uncertain=False,reason=None if passed else 'MODEL_REVIEW_REJECTED',counts_as_success=False,task_verified=passed)
            self.store._event(db,tenant,row['run_id'],'verification.finished',result,'verification-engine')
            return result

    def accept_goal(self,tenant,run_id):
        """Registered Goal criteria only, after every task has a signed verification job."""
        with self.store.connect() as db:
            db.execute('BEGIN');run=self.store._run(db,tenant,run_id)
            tasks=db.execute('SELECT * FROM tasks WHERE tenant=? AND run_id=?',(tenant,run_id)).fetchall()
            if not tasks or any(task['status']!='done' for task in tasks):return False
            for task in tasks:
                rows=db.execute('SELECT * FROM verification_requests WHERE tenant=? AND task_id=? AND epoch=?',(tenant,task['id'],task['epoch'])).fetchall()
                if len(rows)!=1:return False
                state=self._state(db,rows[0])
                if state['state']!='verified' or state['binding']['content_hash']!=digest(json.loads(task['output'])):return False
                signed=self.attestations(tenant,rows[0]['id'])
                if len(signed)!=2 or {item['body']['receipt']['model_id'] for item in signed}!={'deepseek-flash','deepseek-v4-pro'}:return False
            root=json.loads(db.execute('SELECT envelope FROM events WHERE tenant=? AND run_id=? AND seq=1',(tenant,run_id)).fetchone()[0])['payload']
            criteria=root['acceptance']
            if not set(criteria)<={'schema.valid','tests.pass','report.traceable'}:return False
        self.store.finish_run(tenant,run_id,'verified-goal:'+run_id,{criterion:True for criterion in criteria})
        return True
