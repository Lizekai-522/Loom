from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import Principal
from loom.operator import OperatorService
from loom.quotas import QuotaService
from loom.store import Store,Conflict,InvalidInput,NotFound
from loom.tool_recovery import ToolRecoveryService
from tests.test_approved_node import node_case
from tests.test_approved_execution import OP,WORKER
from tests.test_dispatch import dispatch_case,enqueue,drain
from tests.test_execution import FakeSandbox
from tests.test_quotas import configure,claim_for,admit
from tests.test_tool_quotas import tool
from tests.test_public_access import public,login,HEADERS

EVIDENCE=dict(reference='private-tool-log://fixture/one',summary='隔离测试：操作员已核对工具进程和日志，不证明测试通过')

def setup(store,*,state='indeterminate'):
    configure(store);claim=claim_for(store);grant=tool(store,claim)
    with store.transaction() as db:
        db.execute('UPDATE effects SET state=? WHERE id=?',(state,grant['call_id']))
        db.execute("UPDATE tasks SET status='indeterminate',lease_until=NULL WHERE id=?",(claim.task_id,))
        request_hash=db.execute('SELECT request_hash FROM effects WHERE id=?',(grant['call_id'],)).fetchone()[0]
    return claim,grant['call_id'],dict(expected_epoch=claim.epoch,expected_request_hash=request_hash,decision='completed',evidence=EVIDENCE)

def call(store,claim,call_id,kwargs,*,identity=OP,key='tool-resolution'):
    return ToolRecoveryService(store).reconcile(identity,claim.run_id,call_id,key,**kwargs)

@pytest.mark.parametrize('decision,state',[('completed','reconciled'),('not_executed','not_sent')])
def test_resolution_releases_only_tool_slot_and_keeps_rate_tasks_and_budget(tmp_path,decision,state):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store);kwargs['decision']=decision
    before=store.snapshot('a',claim.run_id)
    result=call(store,claim,identifier,kwargs)
    assert result['state']==state and not any(result[k] for k in ('verified','counts_as_success','billing_changed','automatic_retry','model_dispatched'))
    assert QuotaService(Store(store.path)).read('a')['tenant_usage']==dict(calls=1,in_flight=0)
    after=store.snapshot('a',claim.run_id)
    assert after['tasks']==before['tasks'] and after['reserved']==before['reserved'] and after['spent']==before['spent']
    assert after['status']=='awaiting_human' and store.replay('a',claim.run_id)['status']=='awaiting_human'
    events=store.events('a',claim.run_id)
    assert call(Store(store.path),claim,identifier,kwargs)==result and store.events('a',claim.run_id)==events
    with pytest.raises(Conflict):call(store,claim,identifier,{**kwargs,'evidence':{**EVIDENCE,'summary':'different'}})
    with pytest.raises(Conflict):call(store,claim,identifier,kwargs,key='new-command')

@pytest.mark.parametrize('roles',[{'worker'},{'viewer'},{'developer'}])
def test_non_operator_cannot_resolve(tmp_path,roles):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store)
    with pytest.raises(InvalidInput):call(store,claim,identifier,kwargs,identity=Principal('a','test',frozenset(roles)))
    assert QuotaService(store).read('a')['tenant_usage']['in_flight']==1

def test_active_owner_stale_binding_and_audit_failure_roll_back(tmp_path):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store)
    with store.transaction() as db:db.execute("UPDATE tasks SET status='running',lease_until=? WHERE id=?",(store.now(db)+100,claim.task_id))
    with pytest.raises(Conflict,match='active task'):call(store,claim,identifier,kwargs)
    with store.transaction() as db:db.execute("UPDATE tasks SET status='indeterminate',lease_until=NULL WHERE id=?",(claim.task_id,))
    for changed in ({'expected_epoch':2},{'expected_request_hash':'0'*64}):
        with pytest.raises(Conflict):call(store,claim,identifier,{**kwargs,**changed})
    before=store.events('a',claim.run_id)
    with patch.object(store,'_event',side_effect=RuntimeError('synthetic audit failure')),pytest.raises(RuntimeError):call(store,claim,identifier,kwargs)
    assert store.events('a',claim.run_id)==before and QuotaService(store).read('a')['tenant_usage']['in_flight']==1
    assert call(store,claim,identifier,kwargs)['state']=='reconciled'

@pytest.mark.parametrize('partial', [True,False])
def test_contradictory_not_executed_rejected_and_partial_result_preserved(tmp_path,partial):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store,state='partial' if partial else 'indeterminate')
    with store.transaction() as db:db.execute('UPDATE effects SET result=? WHERE id=?',('{"partial_evidence":true}',identifier))
    with pytest.raises(Conflict):call(store,claim,identifier,{**kwargs,'decision':'not_executed'})
    call(store,claim,identifier,kwargs)
    with store.connect() as db:assert db.execute('SELECT result FROM effects WHERE id=?',(identifier,)).fetchone()[0]=='{"partial_evidence":true}'

def test_different_command_concurrency_resolves_once(tmp_path):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store)
    def attempt(number):
        try:return call(Store(store.path),claim,identifier,kwargs,key='resolution-'+str(number))
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(r is not None for r in results)==1
    assert len([e for e in store.events('a',claim.run_id) if e['type']=='effect.reconciled'])==1

def test_model_effect_cannot_be_resolved_as_tool(tmp_path):
    store=Store(tmp_path/'resolve.db');configure(store);claim=claim_for(store);grant=admit(store,claim)
    with store.connect() as db:request_hash=db.execute('SELECT request_hash FROM effects WHERE id=?',(grant['call_id'],)).fetchone()[0]
    with pytest.raises(Conflict,match='tool quota contract'):call(store,claim,grant['call_id'],dict(expected_epoch=claim.epoch,expected_request_hash=request_hash,decision='completed',evidence=EVIDENCE))
    assert store.snapshot('a',claim.run_id)['reserved']==1

def test_dispatch_unknown_tool_resolution_does_not_repeat_or_change_model_billing(dispatch_case):
    b=dispatch_case('developer');configure(b['store']);enqueue(b)
    result,requests=drain(b,sandbox=FakeSandbox(execute_error=True));before=b['store'].snapshot('a',b['run'])
    with b['store'].connect() as db:
        identifier=next(iter(b['store']._tool_call_ids(db,'a')))
        effect=dict(db.execute('SELECT * FROM effects WHERE id=?',(identifier,)).fetchone())
    ToolRecoveryService(b['store']).reconcile(OP,b['run'],identifier,'resolve-worker-tool',expected_epoch=effect['epoch'],expected_request_hash=effect['request_hash'],decision='completed',evidence=EVIDENCE)
    again,new_requests=drain(b)
    assert again is None and not new_requests and len(requests)==1
    after=b['store'].snapshot('a',b['run'])
    assert after['tasks']==before['tasks'] and after['spent']==before['spent'] and after['reserved']==0
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=2,in_flight=0)

def test_api_role_tenant_strict_input_and_review_classification(tmp_path):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store)
    with TestClient(create_app(store,{'a':'legacy-fixture-only-0000'},identities=(OP,WORKER))) as client:
        path=f'/v1/runs/{claim.run_id}/tool-effects/{identifier}/reconcile'
        for headers in ({},{'Authorization':'Bearer '+WORKER.token},{'Authorization':'Bearer legacy-fixture-only-0000'}):
            assert client.post(path,json=kwargs,headers={**headers,'Idempotency-Key':'denied'}).status_code in (401,403)
        headers={'Authorization':'Bearer '+OP.token,'Idempotency-Key':'api-tool'}
        effect=client.get(f'/v1/runs/{claim.run_id}/review',headers=headers).json()['effects'][0]
        assert effect['registered_tool'] and not effect['model_generation'] and effect['request_hash']==kwargs['expected_request_hash']
        assert client.post(path,json={**kwargs,'actual_cost_microusd':0},headers=headers).status_code==422
        assert client.post(path,json=kwargs,headers=headers).status_code==200

def test_public_boundary_denies_tool_reconciliation(public):
    client,_,_=public;login(client)
    assert client.post('/v1/runs/x/tool-effects/'+('0'*64)+'/reconcile',json={},headers=HEADERS).status_code==403


def test_other_tenant_and_wrong_run_cannot_resolve(tmp_path):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store)
    with pytest.raises(NotFound):call(store,claim,identifier,kwargs,identity=Principal('b','operator',frozenset({'operator'})))
    other=claim_for(store)
    with pytest.raises(NotFound):ToolRecoveryService(store).reconcile(OP,other.run_id,identifier,'wrong-run',**kwargs)
    assert QuotaService(store).read('a')['tenant_usage']['in_flight']==1


def test_cancelled_run_resolution_preserves_cancel_and_unrelated_model_budget(tmp_path):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store)
    other=claim_for(store);admit(store,other)
    OperatorService(store).terminate('a',claim.run_id,'cancel-before-tool','subject:operator','停止后核对工具')
    assert call(store,claim,identifier,kwargs)['run_status']=='cancelled'
    assert store.snapshot('a',other.run_id)['reserved']==1
    assert store.snapshot('a',claim.run_id)['tasks'][0]['status']=='cancelled'
    assert QuotaService(store).read('a')['tenant_usage']==dict(calls=2,in_flight=1)


@pytest.mark.parametrize('changed',[{'expected_epoch':True},{'expected_epoch':0},{'decision':'passed'},{'evidence':{'reference':'x','summary':'y','passed':True}}])
def test_invalid_resolution_inputs_cannot_release_slot(tmp_path,changed):
    store=Store(tmp_path/'resolve.db');claim,identifier,kwargs=setup(store)
    with pytest.raises(InvalidInput):call(store,claim,identifier,{**kwargs,**changed})
    assert QuotaService(store).read('a')['tenant_usage']['in_flight']==1
