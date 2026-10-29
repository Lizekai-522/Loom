import asyncio
from dataclasses import replace
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.executor_status import ExecutorStatus
from loom.public_access import PublicPreview
from tests.test_dispatch import dispatch_case, enqueue
from tests.test_approved_node import node_case
from tests.test_approved_execution import OP, WORKER
from tests.test_public_access import CONFIG, ORIGIN, OP as PUBLIC_OP, login


def status_for(b, identities=(OP, WORKER)):
    return ExecutorStatus(b['store'],b['config'],identities)


def test_status_is_scoped_and_does_not_change_admission_digest_or_ledger(dispatch_case):
    b=dispatch_case();status=status_for(b)
    before=b['dispatch'].preview(*b['params']);events=b['store'].events('a',b['run'])
    assert status.read('a')['state']=='not_observed' and not status.directory.exists()
    with patch('loom.executor_status.time.time',return_value=1000):status.publish()
    with patch('loom.executor_status.time.time',return_value=1005):result=status.read('a')
    assert result['state']=='recent' and result['observed_age_seconds']==5
    assert b['dispatch'].preview(*b['params'])['dispatch_digest']==before['dispatch_digest']
    assert b['store'].events('a',b['run'])==events
    assert status.read('other-tenant')['state']=='unknown'
    other=ExecutorStatus(b['store'],replace(b['config'],executor_id='other-executor'),(OP,WORKER))
    assert other.read('a')['state']=='not_observed'
    assert enqueue(b)['state']=='queued'


@pytest.mark.parametrize('now,state,age',[(1015,'recent',15),(1015.01,'stale',15),(999,'unknown',None)])
def test_freshness_expires_and_clock_rollback_is_unknown(dispatch_case,now,state,age):
    status=status_for(dispatch_case())
    with patch('loom.executor_status.time.time',return_value=1000):status.publish()
    with patch('loom.executor_status.time.time',return_value=now):result=status.read('a')
    assert result['state']==state and result['observed_age_seconds']==age
    assert not result['provider_health_checked'] and not result['sandbox_health_checked']


@pytest.mark.parametrize('raw',[b'{"private":"DO_NOT_EXPOSE"}',b'x'*4097,b'{"scope":1,"scope":2}',b'[]'])
def test_corrupt_or_oversized_heartbeat_is_unknown_and_not_echoed(dispatch_case,raw):
    status=status_for(dispatch_case());status.directory.mkdir()
    (status.directory/(status._scope('a')+'.json')).write_bytes(raw)
    response=status.read('a')
    assert response['state']=='unknown' and 'DO_NOT_EXPOSE' not in json.dumps(response)


def test_heartbeat_payload_contains_no_identity_token_or_configuration_path(dispatch_case):
    b=dispatch_case();status=status_for(b);status.publish()
    raw=next(status.directory.glob('*.json')).read_text(encoding='utf-8')
    response=json.dumps(status.read('a'))
    for secret in (OP.token,WORKER.token,str(b['config'].secrets_file),str(b['config'].model_config)):
        assert secret not in raw+response
    assert len(list(status.directory.glob('*')))==1


def test_duplicate_worker_registration_does_not_publish_a_misleading_status(dispatch_case):
    b=dispatch_case();other=LocalIdentity('a','other-worker',frozenset({'worker'}),'other-worker-status-test-token-000000000')
    status=status_for(b,(OP,WORKER,other));status.publish()
    assert status.read('a')['state']=='unknown' and not list(status.directory.glob('*.json'))


def test_heartbeat_continues_during_async_work_and_expires_after_exit(dispatch_case):
    status=status_for(dispatch_case());observed=[]
    original=status.publish
    async def work():
        loop=asyncio.get_running_loop();second=asyncio.Event()
        def capture():
            original();observed.append(True)
            if len(observed)>=2:loop.call_soon_threadsafe(second.set)
        with patch.object(status,'publish',side_effect=capture):
            async with status.monitor(interval=.01):
                await asyncio.wait_for(second.wait(),timeout=2)
                assert len(observed)>=2 and status.read('a')['state']=='recent'
            count=len(observed)
            await asyncio.sleep(.04)
            assert len(observed)==count
    asyncio.run(work())
    data=json.loads(next(status.directory.glob('*.json')).read_bytes())
    with patch('loom.executor_status.time.time',return_value=data['observed_at']+16):
        assert status.read('a')['state']=='stale'


def test_failed_observation_does_not_prevent_work(dispatch_case):
    status=status_for(dispatch_case())
    async def work():
        with patch.object(status,'publish',side_effect=PermissionError('PRIVATE_PATH')):
            async with status.monitor(interval=.01):
                await asyncio.sleep(.025)
                assert status.read('a')['state']=='not_observed'
    asyncio.run(work())


def test_status_endpoint_requires_auth_and_has_no_mutation_route(dispatch_case):
    b=dispatch_case();status=status_for(b);status.publish()
    client=TestClient(create_app(b['store'],{},identities=(OP,WORKER),executor_config=b['config']))
    assert client.get('/v1/executor-status').status_code==401
    response=client.get('/v1/executor-status',headers={'Authorization':'Bearer '+OP.token})
    assert response.status_code==200 and response.json()['state']=='recent'
    assert response.headers['cache-control']=='no-store'
    assert client.post('/v1/executor-status',headers={'Authorization':'Bearer '+WORKER.token}).status_code==405


def test_public_status_requires_invite_and_does_not_allow_writing(dispatch_case):
    b=dispatch_case();worker=LocalIdentity('preview','preview-worker',frozenset({'worker'}),'public-status-fixture-worker-0000000000')
    status=ExecutorStatus(b['store'],b['config'],(PUBLIC_OP,worker));status.publish()
    app=PublicPreview(create_app(b['store'],{},identities=(PUBLIC_OP,worker),executor_config=b['config']),lambda:CONFIG)
    client=TestClient(app,base_url=ORIGIN)
    assert client.get('/v1/executor-status').status_code==401
    login(client)
    response=client.get('/v1/executor-status')
    assert response.status_code==200 and response.json()['tenant_id']=='preview' and response.json()['state']=='recent'
    assert client.post('/v1/executor-status',json={},headers={'Origin':ORIGIN}).status_code==403
