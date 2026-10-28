import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.dispatch import DispatchService
from loom.executor_config import LocalExecutorConfig,load_executor_config
from loom.local_executor import LocalExecutor
from loom.model_gateway import ModelGateway,ModelRoute
from loom.store import Conflict,InvalidInput
from tests.test_approved_node import node_case,gateway_for,BODIES,ROOT
from tests.test_approved_execution import OP,WORKER
from tests.test_execution import FakeSandbox


@pytest.fixture
def dispatch_case(node_case,tmp_path):
    def make(role='analyst'):
        b=node_case(role);model=tmp_path/'model.json';model.write_bytes((ROOT/'configs/models.example.json').read_bytes())
        config=LocalExecutorConfig('test-local',tmp_path/'private-identities',tmp_path/'private-model-key',model,'sha256:'+'1'*64,
            {b['saved']['definition']['nodes'][0]['evaluation_digest']:b['args'].tests_file} if role=='developer' else {})
        b.update(config=config,dispatch=DispatchService(b['store'],config=config,identities=(OP,WORKER)),params=('a',b['run'],b['run']+':work'))
        return b
    return make


def enqueue(b,key='request'):
    p=b['dispatch'].preview(*b['params'])
    return b['dispatch'].enqueue(*b['params'],key,actor='subject:operator',expected_dispatch_digest=p['dispatch_digest'],reason='已核对计划、输入与预算，显式请求本机执行')


def worker_for(b):
    with patch('loom.local_executor.load_identities',return_value=(OP,WORKER)):return LocalExecutor(b['store'],b['config'])


def drain(b,*,gateway=None,sandbox=None):
    requests=[];gateway=gateway or gateway_for(b['body'],requests)
    with patch('loom.local_executor.load_project_secrets',return_value={'DEEPSEEK_API_KEY':'dispatch-public-fixture-only'}),\
         patch('loom.local_executor.load_model_config',return_value=([ModelRoute('deepseek-flash')],{})),\
         patch('loom.local_executor.ModelGateway',return_value=gateway),patch('loom.local_executor.DockerSandbox',return_value=sandbox or FakeSandbox()):
        result=asyncio.run(worker_for(b).drain_one())
    return result,requests


def test_queueing_and_preview_never_load_secret_model_or_docker(dispatch_case):
    b=dispatch_case();before=b['store'].snapshot('a',b['run'])
    with patch('loom.local_executor.load_project_secrets',side_effect=AssertionError('secret')),patch('loom.local_executor.ModelGateway',side_effect=AssertionError('model')):
        p=b['dispatch'].preview(*b['params']);assert p['ready']
        result=enqueue(b)
    assert result['state']=='queued' and b['store'].snapshot('a',b['run'])==before
    assert len(b['dispatch'].history(*b['params'])['items'])==1


def test_exact_idempotency_replay_and_changed_arguments_are_rejected(dispatch_case):
    b=dispatch_case();p=b['dispatch'].preview(*b['params']);kwargs=dict(actor='subject:operator',expected_dispatch_digest=p['dispatch_digest'],reason='明确请求')
    first=b['dispatch'].enqueue(*b['params'],'one',**kwargs);events=b['store'].events('a',b['run'])
    assert b['dispatch'].enqueue(*b['params'],'one',**kwargs)==first
    with pytest.raises(Conflict):b['dispatch'].enqueue(*b['params'],'one',**{**kwargs,'reason':'不同请求'})
    assert b['store'].events('a',b['run'])==events


def test_concurrent_enqueue_has_one_request(dispatch_case):
    b=dispatch_case();p=b['dispatch'].preview(*b['params'])
    def attempt(i):
        try:return b['dispatch'].enqueue(*b['params'],'request-'+str(i),actor='subject:operator',expected_dispatch_digest=p['dispatch_digest'],reason='并发测试')
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(r is not None for r in results)==1


def test_cancel_before_claim_allows_new_explicit_request_and_preserves_history(dispatch_case):
    b=dispatch_case();first=enqueue(b)
    cancelled=b['dispatch'].cancel('a',b['run'],first['request_id'],'cancel',actor='subject:operator',reason='排队阶段取消')
    assert cancelled['state']=='cancelled'
    assert b['dispatch'].cancel('a',b['run'],first['request_id'],'cancel',actor='subject:operator',reason='排队阶段取消')==cancelled
    second=enqueue(b,'another-request');assert second['request_id']!=first['request_id']
    assert [s['state'] for s in b['dispatch'].history(*b['params'])['items']]==['cancelled','queued']
    assert b['store'].snapshot('a',b['run'])['reserved']==0


def test_start_claim_and_request_audit_are_one_atomic_commit(dispatch_case):
    b=dispatch_case();r=enqueue(b);original=b['store']._event
    def fail(*args,**kwargs):
        if args[3]=='execution.request_claimed':raise RuntimeError('synthetic audit failure')
        return original(*args,**kwargs)
    before=b['store'].events('a',b['run'])
    with patch.object(b['store'],'_event',side_effect=fail),pytest.raises(RuntimeError):b['dispatch'].start('a',b['run'],r['request_id'],operator=OP,worker=WORKER)
    assert b['store'].events('a',b['run'])==before and b['store'].snapshot('a',b['run'])['tasks'][0]['epoch']==0
    assert b['dispatch'].history(*b['params'])['items'][0]['state']=='queued'


def test_start_concurrency_and_running_cancel_do_not_repeat_claim(dispatch_case):
    b=dispatch_case();r=enqueue(b)
    def attempt(_):
        try:return b['dispatch'].start('a',b['run'],r['request_id'],operator=OP,worker=WORKER)
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(x is not None for x in results)==1
    with pytest.raises(Conflict):b['dispatch'].cancel('a',b['run'],r['request_id'],'cancel',actor='subject:operator',reason='正在执行，不可队列取消')


@pytest.mark.parametrize('change',['pause','epoch','budget','unknown','contract'])
def test_changed_preview_has_no_partial_request(dispatch_case,change):
    b=dispatch_case();p=b['dispatch'].preview(*b['params'])
    with b['store'].transaction() as db:
        if change=='pause':db.execute("UPDATE runs SET status='paused'")
        elif change=='epoch':db.execute('UPDATE tasks SET epoch=1')
        elif change=='budget':db.execute('UPDATE runs SET cost_limit=0')
        elif change=='unknown':db.execute("INSERT INTO effects(tenant,id,run_id,task_id,epoch,request_hash,state) VALUES('a','unknown',?,?,0,?,'indeterminate')",(b['run'],b['params'][2],'0'*64))
        else:db.execute("UPDATE workflow_tasks SET contract='{}'")
    before=b['store'].events('a',b['run'])
    with pytest.raises(Conflict):b['dispatch'].enqueue(*b['params'],'stale',actor='subject:operator',expected_dispatch_digest=p['dispatch_digest'],reason='旧预览必须拒绝')
    assert b['store'].events('a',b['run'])==before and b['dispatch'].history(*b['params'])['items']==[]


@pytest.mark.parametrize('role',['analyst','architect','tester','developer'])
def test_private_worker_executes_once_and_persists_safe_result(dispatch_case,role):
    b=dispatch_case(role);enqueue(b);result,requests=drain(b)
    assert result['state']=='submitted' and len(requests)==1
    assert result['result']['model_calls']==result['result']['settled_calls']==1 and result['result']['reserved_cost']==0
    assert not result['counts_as_success'] and not result['independently_verified']
    again,extra=drain(b);assert again is None and extra==[]
    raw=json.dumps(b['dispatch'].history(*b['params']),ensure_ascii=False)
    for marker in ('private-host','DEEPSEEK_API_KEY','private-model-key','需求候选'):assert marker not in raw


def test_queued_model_policy_change_blocks_before_any_request(dispatch_case):
    b=dispatch_case();enqueue(b);b['config'].model_config.write_text('{}',encoding='utf-8')
    result,requests=drain(b)
    assert result['state']=='blocked' and requests==[]
    assert b['store'].snapshot('a',b['run'])['tasks'][0]['epoch']==0


def test_queued_task_projection_change_blocks_before_any_provider_call(dispatch_case):
    b=dispatch_case();enqueue(b)
    with b['store'].transaction() as db:
        db.execute('UPDATE tasks SET priority=999 WHERE tenant=? AND id=?',('a',b['params'][2]))
    result,requests=drain(b)
    assert result['state']=='blocked' and requests==[]
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM effects').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==0


def test_known_submission_is_recovered_without_repeating_provider(dispatch_case):
    b=dispatch_case();enqueue(b);original=b['dispatch'].finish
    with patch.object(DispatchService,'finish',side_effect=RuntimeError('simulated process exit after output commit')):
        with pytest.raises(RuntimeError):drain(b)
    with patch('loom.local_executor.ModelGateway',side_effect=AssertionError('must not call provider')):
        assert asyncio.run(worker_for(b).drain_one()) is None
    assert b['dispatch'].history(*b['params'])['items'][0]['state']=='submitted'


def test_unknown_response_preserves_reservation_and_cannot_restart(dispatch_case):
    b=dispatch_case();enqueue(b);requests=[]
    def timeout(request):requests.append(request);raise httpx.ReadTimeout('simulated unknown',request=request)
    gateway=ModelGateway([ModelRoute('deepseek-flash')],transport=httpx.MockTransport(timeout),environ={'DEEPSEEK_API_KEY':'dispatch-public-fixture-only'})
    result,_=drain(b,gateway=gateway)
    assert result['state']=='indeterminate' and len(requests)==1 and result['result']['reserved_cost']>0
    assert not b['dispatch'].preview(*b['params'])['ready']
    assert asyncio.run(worker_for(b).drain_one()) is None


def test_running_request_recovery_waits_for_lease_and_never_reissues(dispatch_case):
    b=dispatch_case();r=enqueue(b);b['dispatch'].start('a',b['run'],r['request_id'],operator=OP,worker=WORKER)
    assert b['dispatch'].finish('a',b['run'],r['request_id'],recover=True)['state']=='running'
    with patch.object(b['store'],'now',return_value=10**12):
        state=b['dispatch'].finish('a',b['run'],r['request_id'],recover=True)
    assert state['state'] in {'awaiting_human','indeterminate'} and not b['dispatch'].preview(*b['params'])['ready']
    with b['store'].connect() as db:
        task=db.execute('SELECT * FROM tasks WHERE id=?',(b['params'][2],)).fetchone()
        assert task['status']!='running' and task['worker'] is None and task['epoch']==1


def test_request_facts_are_append_only_and_orphan_audit_is_detected(dispatch_case):
    b=dispatch_case();enqueue(b)
    with b['store'].connect() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM execution_requests')
    with b['store'].transaction() as db:
        db.execute('DROP TRIGGER execution_requests_no_delete');db.execute('DELETE FROM execution_requests')
    with pytest.raises(Conflict,match='audit history'):b['dispatch'].preview(*b['params'])


def test_history_has_eight_request_bound_and_no_automatic_retry(dispatch_case):
    b=dispatch_case()
    for i in range(8):
        r=enqueue(b,'enqueue-'+str(i));b['dispatch'].cancel('a',b['run'],r['request_id'],'cancel-'+str(i),actor='subject:operator',reason='排队撤销测试')
    assert not b['dispatch'].preview(*b['params'])['ready']
    with pytest.raises(Conflict):enqueue(b,'overflow')


def test_http_role_barriers_and_disabled_default(dispatch_case):
    b=dispatch_case();viewer=LocalIdentity('a','viewer',frozenset({'viewer'}),'dispatch-viewer-public-fixture-00000000')
    path='/v1/runs/'+b['run']+'/tasks/'+b['params'][2]
    with TestClient(create_app(b['store'],{'a':'dispatch-legacy-public-fixture-00000000'},identities=(OP,WORKER,viewer),executor_config=b['config'])) as client:
        header=lambda i:{'Authorization':'Bearer '+i.token,'Idempotency-Key':'http-request'}
        assert client.get(path+'/execution-preview').status_code==401
        p=client.get(path+'/execution-preview',headers=header(viewer)).json();assert p['ready']
        body=dict(expected_dispatch_digest=p['dispatch_digest'],reason='显式请求')
        for identity in (viewer,WORKER):assert client.post(path+'/execution-requests',headers=header(identity),json=body).status_code==403
        assert client.post(path+'/execution-requests',headers=header(OP),json={**body,'secrets_file':'injected'}).status_code==422
        assert client.post(path+'/execution-requests',headers=header(OP),json=body).status_code==201
    with TestClient(create_app(b['store'],{},identities=(OP,WORKER))) as client:
        p=client.get(path+'/execution-preview',headers=header(OP)).json();assert not p['ready'] and p['reason']=='EXECUTOR_NOT_CONFIGURED'
