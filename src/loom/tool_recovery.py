"""Named operator resolution of registered tool uncertainty; never billing or success."""
import re

from .auth import LocalIdentity, Principal
from .operator import _text
from .quotas import QuotaService
from .store import Conflict, InvalidInput, NotFound


class ToolRecoveryService:
    def __init__(self,store):self.store=store

    def reconcile(self,identity,run_id,call_id,key,*,expected_epoch,expected_request_hash,decision,evidence):
        if (not isinstance(identity,(LocalIdentity,Principal)) or 'operator' not in identity.roles
            or identity.subject_id.startswith('legacy:')):
            raise InvalidInput('named operator required for tool reconciliation')
        for value,maximum,label in ((run_id,200,'run'),(key,200,'command key')):_text(value,maximum,label)
        if (type(expected_epoch) is not int or expected_epoch<1
            or any(not isinstance(v,str) or re.fullmatch(r'[0-9a-f]{64}',v) is None for v in (call_id,expected_request_hash))
            or decision not in ('completed','not_executed') or type(evidence) is not dict
            or set(evidence)!={'reference','summary'}):raise InvalidInput('invalid tool reconciliation')
        _text(evidence['reference'],500,'evidence reference');_text(evidence['summary'],2000,'evidence summary')
        tenant=identity.tenant_id;actor='subject:'+identity.subject_id
        args=dict(run_id=run_id,call_id=call_id,actor=actor,expected_epoch=expected_epoch,
            expected_request_hash=expected_request_hash,decision=decision,evidence=dict(evidence))
        def apply(db):
            run=self.store._run(db,tenant,run_id)
            if run['status'] not in {'open','paused','awaiting_human','cancelled'}:
                raise Conflict('run is not in a reconcilable state')
            effect=db.execute('SELECT * FROM effects WHERE tenant=? AND run_id=? AND id=?',(tenant,run_id,call_id)).fetchone()
            if effect is None:raise NotFound('tool effect not found')
            if effect['epoch']!=expected_epoch or effect['request_hash']!=expected_request_hash:
                raise Conflict('tool reconciliation binding changed')
            quotas=QuotaService(self.store)
            if quotas._policy(db,tenant) is None:raise Conflict('registered tool quota contract required')
            record=next((r for r in quotas._records(db,tenant) if r['call_id']==call_id),None)
            if (record is None or record['kind']!='tool' or record['resource']!='tests.run'
                or record['run_id']!=run_id or record['task_id']!=effect['task_id'] or record['epoch']!=expected_epoch):
                raise Conflict('registered tool quota contract required')
            if effect['state'] not in {'intent','indeterminate','partial'}:
                raise Conflict('tool effect is already known or reconciled')
            if decision=='not_executed' and (effect['state']=='partial' or effect['result'] is not None):
                raise Conflict('not-executed decision contradicts recorded tool evidence')
            task=db.execute('SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?',(tenant,run_id,effect['task_id'])).fetchone()
            if task is None:raise Conflict('tool task is unavailable')
            if task['lease_until'] is not None and task['lease_until']>self.store.now(db):
                raise Conflict('active task owner must stop before tool reconciliation')
            state='reconciled' if decision=='completed' else 'not_sent'
            status='awaiting_human' if run['status']=='open' else run['status']
            result=dict(**args,task_id=effect['task_id'],epoch=expected_epoch,state=state,run_status=status,
                kind='registered-tool',tool_id='tests.run',counts_as_success=False,verified=False,
                model_dispatched=False,billing_changed=False,automatic_retry=False)
            # Preserve recorded tool evidence. This declaration is never a host
            # test receipt, a model response, or proof that any criteria passed.
            db.execute('UPDATE effects SET state=? WHERE tenant=? AND id=?',(state,tenant,call_id))
            db.execute('UPDATE runs SET status=? WHERE tenant=? AND id=?',(status,tenant,run_id))
            self.store._event(db,tenant,run_id,'effect.reconciled',result,actor,key)
            return result
        return self.store._command(tenant,key,'operator.reconcile_tool',args,apply)
