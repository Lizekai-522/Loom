"""Audited local requests. HTTP queues intent; only the private worker executes."""
from dataclasses import asdict
import json
from typing import Literal
from uuid import uuid4

from pydantic import Field,ValidationError

from .approved_execution import ApprovedExecutionService
from .artifact_schemas import ArtifactModel,Hash,ReviewIdentifier
from .artifacts import ArtifactService,_hash,_text
from .auth import LocalIdentity
from .review_inputs import ReviewInputService
from .store import Claim,Conflict,InvalidInput,NotFound,canonical,digest

SCHEMA='''
CREATE TABLE IF NOT EXISTS execution_requests (
 tenant TEXT NOT NULL,run_id TEXT NOT NULL,task_id TEXT NOT NULL,id TEXT NOT NULL,
 record TEXT NOT NULL,event_hash TEXT NOT NULL,PRIMARY KEY(tenant,id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id),
 FOREIGN KEY(tenant,task_id) REFERENCES tasks(tenant,id));
CREATE INDEX IF NOT EXISTS execution_requests_task ON execution_requests(tenant,run_id,task_id);
CREATE TRIGGER IF NOT EXISTS execution_requests_no_update BEFORE UPDATE ON execution_requests
 BEGIN SELECT RAISE(ABORT,'execution requests are append-only'); END;
CREATE TRIGGER IF NOT EXISTS execution_requests_no_delete BEFORE DELETE ON execution_requests
 BEGIN SELECT RAISE(ABORT,'execution requests are append-only'); END;
'''
FINAL={'submitted','blocked','awaiting_human','indeterminate','cancelled'}
MAX_REQUESTS=8


class RequestRecord(ArtifactModel):
    schema_version:Literal['loom.execution-request/v1']
    request_id:ReviewIdentifier
    tenant_id:ReviewIdentifier
    run_id:ReviewIdentifier
    task_id:ReviewIdentifier
    node_id:ReviewIdentifier
    role:ReviewIdentifier
    plan_digest:Hash
    approved_preview_digest:Hash
    runtime_binding:Hash
    dispatch_digest:Hash
    executor_id:ReviewIdentifier
    operator_id:ReviewIdentifier
    worker_id:ReviewIdentifier
    reason:str=Field(min_length=1,max_length=2000)


class ClaimRecord(ArtifactModel):
    tenant_id:ReviewIdentifier
    run_id:ReviewIdentifier
    task_id:ReviewIdentifier
    worker_id:ReviewIdentifier
    epoch:int=Field(ge=1)
    lease_until:float


class Summary(ArtifactModel):
    task_status:ReviewIdentifier
    epoch:int=Field(ge=0)
    content_hash:Hash|None=None
    model_calls:int=Field(ge=0)
    settled_calls:int=Field(ge=0)
    reserved_cost:int=Field(ge=0)
    reserved_tokens:int=Field(ge=0)
    run_status:ReviewIdentifier


class Transition(ArtifactModel):
    request_id:ReviewIdentifier
    record_digest:Hash
    state:Literal['running','submitted','blocked','awaiting_human','indeterminate','cancelled']
    claim:ClaimRecord|None=None
    result:Summary|None=None
    reason:str|None=Field(default=None,max_length=2000)
    counts_as_success:Literal[False]=False
    independently_verified:Literal[False]=False
    automatic_retry:Literal[False]=False


class DispatchService:
    def __init__(self,store,*,config=None,identities=(),initialize=True):
        self.store,self.config,self.identities=store,config,tuple(identities)
        self.artifacts=ArtifactService(store,initialize=False)
        self.admission=ApprovedExecutionService(store)
        if initialize:
            with store.connect() as db:db.executescript(SCHEMA)

    @staticmethod
    def flags():return dict(counts_as_success=False,independently_verified=False,automatic_retry=False)

    def _identity(self,tenant,role,subject=None):
        found=[i for i in self.identities if isinstance(i,LocalIdentity) and i.tenant_id==tenant and role in i.roles and (subject is None or i.subject_id==subject)]
        if len(found)!=1:raise InvalidInput('unique registered local identity required')
        return found[0]

    def _task(self,db,tenant,run_id,task_id):
        self.store._run(db,tenant,run_id)
        task=db.execute('SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?',(tenant,run_id,task_id)).fetchone()
        if task is None:raise NotFound('task not found in tenant/run')
        return task

    def _record(self,db,row):
        try:
            raw=json.loads(row['record']);record=RequestRecord.model_validate(raw).model_dump(mode='json')
            if canonical(raw)!=canonical(record) or (record['tenant_id'],record['run_id'],record['task_id'],record['request_id'])!=(row['tenant'],row['run_id'],row['task_id'],row['id']):raise ValueError
        except (ValidationError,ValueError,TypeError,RecursionError):raise Conflict('execution request binding is inconsistent') from None
        self.artifacts._event_matches(db,row['tenant'],row['run_id'],row['event_hash'],'execution.requested','subject:'+record['operator_id'],{'record':record,'record_digest':digest(record),**self.flags()})
        return record

    def _state(self,db,row):
        record=self._record(db,row);state='queued';claim=None;result=None;reason=None;last=row['event_hash']
        rows=db.execute("SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.payload.request_id')=? AND json_extract(envelope,'$.type') IN ('execution.request_claimed','execution.request_finished','execution.request_cancelled') ORDER BY seq",(row['tenant'],row['run_id'],row['id'])).fetchall()
        for event_row in rows:
            try:
                event=json.loads(event_row['envelope']);raw=event['payload'];payload=Transition.model_validate(raw).model_dump(mode='json')
                if canonical(raw)!=canonical(payload) or payload['record_digest']!=digest(record):raise ValueError
                next_claim=payload['claim']
                if next_claim is not None and any(next_claim[k]!=record[k] for k in ('tenant_id','run_id','task_id','worker_id')):raise ValueError
                if event['type']=='execution.request_claimed':
                    if state!='queued' or payload['state']!='running' or next_claim is None or next_claim['epoch']!=1 or payload['result'] is not None:raise ValueError
                    expected_actor='subject:'+record['worker_id']
                elif event['type']=='execution.request_cancelled':
                    if state!='queued' or payload['state']!='cancelled' or next_claim is not None or payload['result'] is not None or not event['actor'].startswith('subject:'):raise ValueError
                    expected_actor=event['actor']
                else:
                    if state not in {'queued','running'} or payload['state'] not in FINAL or (state=='queued' and payload['state']!='blocked') or next_claim!=claim:raise ValueError
                    expected_actor='subject:'+record['worker_id']
                self.artifacts._event_matches(db,row['tenant'],row['run_id'],event_row['hash'],event['type'],expected_actor,payload)
                state,claim,result,reason,last=payload['state'],next_claim,payload['result'],payload['reason'],event_row['hash']
            except (ValidationError,ValueError,TypeError,KeyError,RecursionError):raise Conflict('execution request history is inconsistent') from None
        return {**record,'state':state,'claim':claim,'result':result,'state_reason':reason,'event_hash':row['event_hash'],'last_event_hash':last,**self.flags()}

    def _history(self,db,tenant,run_id,task_id):
        rows=db.execute('SELECT * FROM execution_requests WHERE tenant=? AND run_id=? AND task_id=? ORDER BY rowid',(tenant,run_id,task_id)).fetchall()
        if len(rows)>MAX_REQUESTS:raise Conflict('execution request history exceeds bound')
        events=db.execute("SELECT hash FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='execution.requested' AND json_extract(envelope,'$.payload.record.task_id')=?",(tenant,run_id,task_id)).fetchall()
        if {e[0] for e in events}!={r['event_hash'] for r in rows}:raise Conflict('execution requests differ from audit history')
        return [self._state(db,r) for r in rows]

    def history(self,tenant,run_id,task_id):
        with self.store.connect() as db:
            db.execute('BEGIN');self._task(db,tenant,run_id,task_id)
            return dict(items=self._history(db,tenant,run_id,task_id),**self.flags())

    def _preview(self,db,tenant,run_id,task_id):
        task=self._task(db,tenant,run_id,task_id);history=self._history(db,tenant,run_id,task_id)
        latest=history[-1] if history else None
        data=dict(schema_version='loom.dispatch-preview/v1',tenant_id=tenant,run_id=run_id,task_id=task_id,
            executor_configured=self.config is not None,ready=False,reason=None,approved_preview=None,runtime_binding=None,
            latest_request=latest,history_count=len(history),max_requests=MAX_REQUESTS,requires_explicit_request=True,model_dispatched=False,**self.flags())
        if self.config is None:data['reason']='EXECUTOR_NOT_CONFIGURED'
        elif latest and latest['state'] in {'queued','running'}:data['reason']='REQUEST_ALREADY_ACTIVE'
        elif len(history)>=MAX_REQUESTS:data['reason']='REQUEST_LIMIT_REACHED'
        else:
            try:
                self._identity(tenant,'worker')
                spec=self.artifacts._contract(db,tenant,run_id,task_id)
                if spec['role']=='validator':raise InvalidInput('INDEPENDENT_MODEL_PENDING')
                tests=self.config.tests_for(spec)
                preview=self.admission._check_in(db,tenant,run_id,spec['node_id'],spec['plan_digest'],tests,1024)[1]
                binding=self.config.binding(spec['role'],spec['evaluation_digest'])
                data.update(ready=True,approved_preview=preview,runtime_binding=binding,executor_id=self.config.executor_id)
            except (Conflict,InvalidInput,NotFound,OSError) as e:
                known={'HOST_TESTS_NOT_REGISTERED','HOST_TESTS_CHANGED','INDEPENDENT_MODEL_PENDING'}
                data['reason']=str(e) if str(e) in known else 'ADMISSION_NOT_READY'
        return {**data,'dispatch_digest':digest(data)}

    def preview(self,tenant,run_id,task_id):
        with self.store.connect() as db:db.execute('BEGIN');return self._preview(db,tenant,run_id,task_id)

    def enqueue(self,tenant,run_id,task_id,key,*,actor,expected_dispatch_digest,reason):
        _hash(expected_dispatch_digest);_text(reason,'request reason',2000)
        if '\x00' in reason:raise InvalidInput('invalid request reason')
        try:reason.encode('utf-8')
        except UnicodeError:raise InvalidInput('invalid request reason encoding') from None
        if not isinstance(actor,str) or not actor.startswith('subject:'):raise InvalidInput('named operator required')
        operator=self._identity(tenant,'operator',actor[8:]);worker=self._identity(tenant,'worker')
        args=dict(run_id=run_id,task_id=task_id,actor=actor,expected_dispatch_digest=expected_dispatch_digest,reason=reason)
        def apply(db):
            preview=self._preview(db,tenant,run_id,task_id)
            if not preview['ready'] or preview['dispatch_digest']!=expected_dispatch_digest:raise Conflict('execution request does not match current preview')
            p=preview['approved_preview']
            record=RequestRecord(schema_version='loom.execution-request/v1',request_id=str(uuid4()),tenant_id=tenant,run_id=run_id,task_id=task_id,node_id=p['node_id'],role=p['role'],
                plan_digest=p['plan_digest'],approved_preview_digest=p['preview_digest'],runtime_binding=preview['runtime_binding'],dispatch_digest=expected_dispatch_digest,
                executor_id=self.config.executor_id,operator_id=operator.subject_id,worker_id=worker.subject_id,reason=reason).model_dump(mode='json')
            event=self.store._event(db,tenant,run_id,'execution.requested',{'record':record,'record_digest':digest(record),**self.flags()},actor,key)
            db.execute('INSERT INTO execution_requests VALUES(?,?,?,?,?,?)',(tenant,run_id,task_id,record['request_id'],canonical(record),event['hash']))
            return {**record,'state':'queued','event_hash':event['hash'],**self.flags()}
        return self.store._command(tenant,key,'execution.enqueue',args,apply)

    def _row(self,db,tenant,run_id,request_id):
        self.store._run(db,tenant,run_id)
        row=db.execute('SELECT * FROM execution_requests WHERE tenant=? AND run_id=? AND id=?',(tenant,run_id,request_id)).fetchone()
        if row is None:raise NotFound('execution request not found in tenant/run')
        return row

    def _transition(self,db,row,state,claim=None,result=None,reason=None,*,event_type='execution.request_finished',actor=None,key=None):
        record=self._record(db,row)
        payload=Transition(request_id=record['request_id'],record_digest=digest(record),state=state,claim=claim,result=result,reason=reason).model_dump(mode='json')
        self.store._event(db,row['tenant'],row['run_id'],event_type,payload,actor or 'subject:'+record['worker_id'],key)
        return self._state(db,row)

    def cancel(self,tenant,run_id,request_id,key,*,actor,reason):
        if not isinstance(actor,str) or not actor.startswith('subject:'):raise InvalidInput('named operator required')
        self._identity(tenant,'operator',actor[8:]);_text(reason,'cancel reason',2000)
        if '\x00' in reason:raise InvalidInput('invalid cancel reason')
        try:reason.encode('utf-8')
        except UnicodeError:raise InvalidInput('invalid cancel reason encoding') from None
        args=dict(run_id=run_id,request_id=request_id,actor=actor,reason=reason)
        def apply(db):
            row=self._row(db,tenant,run_id,request_id);current=self._state(db,row)
            if current['state']!='queued':raise Conflict('only queued requests can be cancelled; use run controls for active work')
            return self._transition(db,row,'cancelled',reason=reason,event_type='execution.request_cancelled',actor=actor,key=key)
        return self.store._command(tenant,key,'execution.cancel',args,apply)

    def start(self,tenant,run_id,request_id,*,operator,worker):
        if self.config is None:raise Conflict('local executor is not configured')
        with self.store.transaction() as db:
            row=self._row(db,tenant,run_id,request_id);current=self._state(db,row)
            if current['state']!='queued':raise Conflict('execution request is no longer queued')
            if (operator!=self._identity(tenant,'operator',current['operator_id']) or worker!=self._identity(tenant,'worker',current['worker_id'])):raise InvalidInput('local worker identity differs')
            spec=self.artifacts._contract(db,tenant,run_id,current['task_id']);tests=self.config.tests_for(spec)
            if self.config.executor_id!=current['executor_id'] or self.config.binding(spec['role'],spec['evaluation_digest'])!=current['runtime_binding']:raise Conflict('local execution policy changed')
            claim=self.admission._claim_in(db,tenant,run_id,current['node_id'],current['plan_digest'],tests,current['approved_preview_digest'],operator=operator,worker=worker,max_output_tokens=1024)
            self._transition(db,row,'running',asdict(claim),event_type='execution.request_claimed')
            return claim,tests,spec

    def block_queued(self,tenant,run_id,request_id,reason='LOCAL_PRECONDITION_FAILED'):
        with self.store.transaction() as db:
            row=self._row(db,tenant,run_id,request_id);state=self._state(db,row)
            if state['state']!='queued':return state
            return self._transition(db,row,'blocked',reason=reason)

    def finish(self,tenant,run_id,request_id,*,recover=False):
        with self.store.transaction() as db:
            row=self._row(db,tenant,run_id,request_id);current=self._state(db,row)
            if current['state']!='running':return current
            task=self._task(db,tenant,run_id,current['task_id']);run=self.store._run(db,tenant,run_id)
            if recover and task['status']=='running' and task['lease_until'] and task['lease_until']>self.store.now(db):return current
            effects=db.execute('SELECT state FROM effects WHERE tenant=? AND run_id=? AND task_id=? AND epoch=?',(tenant,run_id,task['id'],current['claim']['epoch'])).fetchall()
            held=db.execute("SELECT coalesce(sum(cost),0),coalesce(sum(tokens),0) FROM reservations WHERE tenant=? AND run_id=? AND task_id=? AND state='reserved'",(tenant,run_id,task['id'])).fetchone()
            state='indeterminate' if any(e['state'] in {'intent','indeterminate','partial'} for e in effects) or held[0] or held[1] else 'awaiting_human'
            if task['status']=='running' and task['epoch']==current['claim']['epoch']:
                # A dead worker must not occupy capacity forever. Fencing this
                # expired/returned attempt never permits another model attempt;
                # epoch remains1 and unknown cost/token holds remain untouched.
                stopped='indeterminate' if state=='indeterminate' else 'pending'
                db.execute('UPDATE tasks SET status=?,worker=NULL,lease_until=NULL WHERE tenant=? AND id=?',(stopped,tenant,task['id']))
                if run['status']=='open':db.execute("UPDATE runs SET status='awaiting_human' WHERE tenant=? AND id=?",(tenant,run_id))
                self.store._event(db,tenant,run_id,'execution.request_recovery_stopped',dict(request_id=request_id,task_id=task['id'],epoch=task['epoch'],
                    state=stopped,reserved_cost=held[0],reserved_tokens=held[1],**self.flags()),'subject:'+current['worker_id'])
                task=self._task(db,tenant,run_id,task['id']);run=self.store._run(db,tenant,run_id)
            content=None
            if task['status']=='submitted' and state!='indeterminate' and task['epoch']==current['claim']['epoch']:
                spec=self.artifacts._contract(db,tenant,run_id,task['id'])
                output=json.loads(task['output']);content=digest(output)
                if spec.get('role_output'):
                    candidate,_=self.artifacts._candidate(db,tenant,run_id,output['artifact_id'])
                    if candidate['source_task_id']!=task['id'] or candidate['source_epoch']!=task['epoch']:raise Conflict('completed request candidate differs')
                else:ReviewInputService(self.store)._preview_in(db,tenant,run_id,task['id'],False)
                state='submitted'
            elif run['status']=='cancelled' and state!='indeterminate':state='cancelled'
            summary=Summary(task_status=task['status'],epoch=task['epoch'],content_hash=content,model_calls=len(effects),settled_calls=sum(e['state']=='done' for e in effects),
                reserved_cost=held[0],reserved_tokens=held[1],run_status=run['status']).model_dump(mode='json')
            return self._transition(db,row,state,current['claim'],summary,'RECOVERED_FROM_LEDGER' if recover else None)

    def pending(self):
        with self.store.connect() as db:
            db.execute('BEGIN');rows=db.execute('SELECT * FROM execution_requests ORDER BY rowid').fetchall()
            return [self._state(db,r) for r in rows if json.loads(r['record'])['executor_id']==getattr(self.config,'executor_id',None)]
