from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.quotas import Limit,QuotaPolicy,QuotaService,QuotaExceeded
from loom.store import Store,BudgetExceeded,InvalidInput,Conflict
from tests.test_approved_execution import OP,WORKER
from tests.test_approved_node import node_case,execute
from tests.test_public_access import public,login,ORIGIN


POLICY=QuotaPolicy(60,Limit(20,3),Limit(10,1),Limit(10,1))


def configure(store,policy=POLICY,tenant='a',revision=0,key='quota-one'):
    identity=OP if tenant=='a' else LocalIdentity(tenant,'operator',frozenset({'operator'}),'other-quota-operator-fixture-0000000000')
    return QuotaService(store).configure(identity,policy,key=key,expected_revision=revision,reason='明确的隔离测试配额')


def claim_for(store,tenant='a'):
    unique=uuid4().hex
    run=store.create_run(tenant,'run-'+unique,acceptance=['tests.pass'],cost_limit=10000,token_limit=10000)['run_id']
    store.add_task(tenant,run,'task-'+unique,task_id=unique,title='quota fixture')
    return store.claim_next(tenant,run,'worker-'+unique,lease_seconds=300)


def admit(store,claim,*,model='deepseek-flash',route='flash'):
    return store.reserve_and_begin_effect(claim,'model:'+claim.task_id,max_cost=1,max_tokens=1,
        operation_id='model-generation:'+str(claim.epoch),request=dict(provider='deepseek',model=model,route=route))


def sizes(store):
    with store.connect() as db:
        return tuple(db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('effects','reservations','quota_admissions'))


@pytest.mark.parametrize('field,value',[('window_seconds',True),('window_seconds',0),('window_seconds',3601)])
def test_policy_rejects_unbounded_or_non_integer_window(field,value):
    with pytest.raises(InvalidInput):replace(POLICY,**{field:value})


@pytest.mark.parametrize('value',[True,-1,10001,1.5,'20'])
def test_rate_limits_are_strict_integers(value):
    with pytest.raises(InvalidInput):Limit(value,1)


def test_configuration_is_operator_only_revision_bound_and_idempotent(tmp_path):
    store=Store(tmp_path/'quota.db');service=QuotaService(store)
    with pytest.raises(InvalidInput):service.configure(WORKER,POLICY,key='bad',expected_revision=0,reason='no')
    first=configure(store);assert first['revision']==1
    assert configure(store)['policy_hash']==first['policy_hash'] and not configure(store)['changed']
    with pytest.raises(Conflict):configure(store,replace(POLICY,model=Limit(1,1)))
    with pytest.raises(Conflict):configure(store,revision=0,key='wrong-revision')
    assert QuotaService(Store(store.path)).read('a')['policy_hash']==first['policy_hash']


def test_failed_initial_configuration_rolls_back_schema_and_marker(tmp_path):
    store=Store(tmp_path/'quota.db')
    with pytest.raises(Conflict):configure(store,revision=1)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'quota_%'").fetchone()[0]==0
        assert db.execute("SELECT 1 FROM settings WHERE key='quotas_enabled'").fetchone() is None
    assert not QuotaService(store).read('a')['configured']


@pytest.mark.parametrize('scope',['tenant','model'])
def test_rate_denial_rolls_back_effect_budget_and_admission(tmp_path,scope):
    store=Store(tmp_path/'quota.db');configure(store,replace(POLICY,**{scope:Limit(0,1)}));claim=claim_for(store)
    before=sizes(store)
    with pytest.raises(QuotaExceeded,match='QUOTA_'+scope.upper()+'_RATE_LIMIT'):admit(store,claim)
    assert sizes(store)==before==(0,0,0) and store.snapshot('a',claim.run_id)['reserved']==0


def test_concurrent_process_adapters_have_one_model_slot(tmp_path):
    store=Store(tmp_path/'quota.db');configure(store);claims=[claim_for(store),claim_for(store)]
    def call(claim):
        try:return admit(Store(store.path),claim)
        except QuotaExceeded as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(call,claims))
    assert sum(isinstance(r,dict) for r in results)==1 and 'QUOTA_MODEL_IN_FLIGHT_LIMIT' in results
    assert sizes(store)==(1,1,1)


def test_restart_and_replay_never_reset_or_double_charge(tmp_path):
    store=Store(tmp_path/'quota.db');configure(store);claim=claim_for(store);first=admit(store,claim)
    reopened=Store(store.path);again=admit(reopened,claim)
    assert first['dispatch'] and not again['dispatch'] and sizes(store)==(1,1,1)
    assert QuotaService(reopened).read('a')['tenant_usage']==dict(calls=1,in_flight=1)


def test_unknown_call_keeps_concurrency_after_window_and_policy_change(tmp_path):
    store=Store(tmp_path/'quota.db');configure(store);first=claim_for(store);admit(store,first);second=claim_for(store)
    configure(store,revision=1,key='quota-two')
    with store.connect() as db:now=store.now(db)
    with patch.object(store,'now',return_value=now+61):
        assert QuotaService(store).read('a')['tenant_usage']==dict(calls=0,in_flight=1)
        with pytest.raises(QuotaExceeded,match='QUOTA_MODEL_IN_FLIGHT_LIMIT'):admit(store,second)
    assert sizes(store)==(1,1,1)


def test_alias_change_does_not_reset_actual_model_rate(tmp_path):
    store=Store(tmp_path/'quota.db');configure(store,replace(POLICY,model=Limit(1,1)))
    first=claim_for(store);grant=admit(store,first,route='first-alias');store.finish_effect(first,grant['call_id'],{'known':True})
    second=claim_for(store)
    with pytest.raises(QuotaExceeded,match='QUOTA_MODEL_RATE_LIMIT'):admit(store,second,route='second-alias')
    assert QuotaService(store).read('a')['tenant_usage']==dict(calls=1,in_flight=0)


def test_legacy_unknown_effect_is_conservatively_counted(tmp_path):
    store=Store(tmp_path/'quota.db');first=claim_for(store);store.begin_effect(first,'legacy-unknown',{'private':'not shown'})
    configure(store);second=claim_for(store)
    with pytest.raises(QuotaExceeded,match='QUOTA_MODEL_IN_FLIGHT_LIMIT'):admit(store,second)
    result=QuotaService(store).read('a');assert result['legacy_unknown_in_flight']==1
    assert 'not shown' not in str(result)


def test_audit_failure_and_budget_failure_leave_no_admission(tmp_path):
    store=Store(tmp_path/'quota.db');configure(store);claim=claim_for(store);original=store._event
    def fail(*args,**kwargs):
        if args[3]=='quota.admitted':raise RuntimeError('synthetic failure')
        return original(*args,**kwargs)
    with patch.object(store,'_event',side_effect=fail),pytest.raises(RuntimeError):admit(store,claim)
    assert sizes(store)==(0,0,0)
    with pytest.raises(BudgetExceeded):
        store.reserve_and_begin_effect(claim,'large',max_cost=10001,max_tokens=1,operation_id='model-generation:1',request={'model':'deepseek-flash'})
    assert sizes(store)==(0,0,0)


def test_clock_rollback_and_missing_enabled_policy_fail_closed(tmp_path):
    store=Store(tmp_path/'quota.db');configure(store);first=claim_for(store);admit(store,first);second=claim_for(store)
    with store.connect() as db:now=store.now(db)
    with patch.object(store,'now',return_value=now-10),pytest.raises(QuotaExceeded,match='QUOTA_CLOCK_UNCERTAIN'):admit(store,second)
    with store.transaction() as db:db.execute("DELETE FROM settings WHERE key='quotas_enabled'")
    with pytest.raises(QuotaExceeded,match='QUOTA_POLICY_INVALID'):admit(store,second)
    assert sizes(store)==(1,1,1)


def test_tenant_isolation_and_unconfigured_tenant_denial(tmp_path):
    store=Store(tmp_path/'quota.db');configure(store,replace(POLICY,tenant=Limit(0,1)))
    b=claim_for(store,'b')
    with pytest.raises(QuotaExceeded,match='QUOTA_POLICY_NOT_CONFIGURED'):admit(store,b)
    configure(store,tenant='b');assert admit(store,b)['dispatch']
    assert QuotaService(store).read('a')['resources']==[]
    assert QuotaService(store).read('b')['tenant_usage']['calls']==1


@pytest.mark.parametrize('role',['developer','analyst','architect','tester'])
def test_real_execution_entry_reports_quota_rejection_without_mock_provider_calls(node_case,role):
    b=node_case(role);configure(b['store'],replace(POLICY,model=Limit(0,1)))
    result,requests=execute(b)
    assert result['execution']['reason']=='QUOTA_MODEL_RATE_LIMIT' and result['execution']['status']=='awaiting_human'
    assert not requests and sizes(b['store'])==(0,0,0)


def test_read_only_status_is_tenant_scoped_and_cannot_configure_over_http(tmp_path,public):
    store=Store(tmp_path/'quota.db');configure(store)
    viewer=LocalIdentity('a','quota-viewer',frozenset({'viewer'}),'quota-viewer-fixture-token-00000000000')
    with TestClient(create_app(store,{},identities=(viewer,))) as client:
        assert client.get('/v1/quotas').status_code==401
        headers={'Authorization':'Bearer '+viewer.token};result=client.get('/v1/quotas',headers=headers)
        assert result.status_code==200 and result.json()['tenant_id']=='a' and viewer.token not in result.text
        assert client.post('/v1/quotas',json={},headers=headers).status_code==405
    client,_,_=public;login(client)
    assert not client.get('/v1/quotas').json()['configured']
    assert client.post('/v1/quotas',json={},headers={'Origin':ORIGIN}).status_code==403
