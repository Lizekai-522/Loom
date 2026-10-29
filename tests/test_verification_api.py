import asyncio
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.executor_config import LocalExecutorConfig
from loom.public_access import PublicPreview
from loom.verification_config import load_verification_config
from loom.verification_runtime import VerificationRuntime
from tests.test_approved_execution import OP,WORKER
from tests.test_approved_node import node_case,execute,ROOT
from tests.test_verification_config import script
from tests.test_verification_jobs import verification_case,run_job

@pytest.fixture
def verification_api(node_case,tmp_path):
    b=node_case('developer');execute(b)
    identities=tmp_path/'identities.json'
    identities.write_text(json.dumps(dict(schema_version='loom.local-identities/v1',identities=[dict(tenant_id=i.tenant_id,subject_id=i.subject_id,roles=sorted(i.roles),token=i.token) for i in (OP,WORKER)])),encoding='utf-8')
    path=script.configure(tmp_path/'keys',tenant='a',identities=identities,model_config=ROOT/'configs/models.dual.example.json',model_secrets=tmp_path/'missing-model-key.json',db=b['store'].path,pool_limit=2000000)
    config=LocalExecutorConfig('fixture',identities,tmp_path/'missing-model-key.json',ROOT/'configs/models.dual.example.json',None,{},verification_config=path)
    b.update(config=config,client=TestClient(create_app(b['store'],{'a':'legacy-test-key-long-enough'},identities=(OP,WORKER),executor_config=config)),task=b['run']+':work',verification_config=load_verification_config(path))
    return b

def headers(identity=OP,key=None):return {'Authorization':'Bearer '+identity.token,**({'Idempotency-Key':key} if key else {})}

def path(b,suffix):return f"/v1/runs/{b['run']}/tasks/{b['task']}/"+suffix

def test_readiness_and_history_do_not_read_private_signers_or_model_keys(verification_api,monkeypatch):
    b=verification_api
    def forbidden(*args,**kwargs):raise AssertionError('private material read')
    monkeypatch.setattr(type(b['verification_config']),'load_signers',forbidden)
    cap=b['client'].get('/v1/verification-capabilities',headers=headers()).json()
    assert cap['configured'] and cap['completion_connected'] and not cap['private_key_health_checked'] and not cap['model_dispatched']
    assert cap['executor']['state']=='not_observed' and cap['project_pool']['limit_microusd']==2000000
    result=b['client'].get(path(b,'verification'),headers=headers()).json()
    assert not result['requests'] and len(result['public_keys'])==2
    assert 'PRIVATE KEY' not in json.dumps(result) and str(b['verification_config'].path) not in json.dumps(cap)

def test_only_named_operator_can_request_exact_idempotent_verification(verification_api):
    b=verification_api;c=b['client'];url=path(b,'verification-requests')
    preview=c.get(path(b,'verification-preview'),headers=headers()).json();body=dict(expected_preview_digest=preview['preview_digest'],reason='明确申请额外双模型复核')
    assert c.post(url,json=body,headers={'Idempotency-Key':'verify'}).status_code==401
    assert c.post(url,json=body,headers=headers(WORKER,'verify')).status_code==403
    assert c.post(url,json=body,headers={'Authorization':'Bearer legacy-test-key-long-enough','Idempotency-Key':'verify'}).status_code==403
    first=c.post(url,json=body,headers=headers(key='verify'));assert first.status_code==200 and first.json()['state']=='queued'
    assert c.post(url,json=body,headers=headers(key='verify')).json()==first.json()
    assert c.post(url,json=body,headers=headers(key='new-key')).status_code==409
    assert c.post(url,json={**body,'pool_limit_microusd':9999999},headers=headers(key='bad')).status_code==422
    state=c.get(path(b,'verification'),headers=headers()).json();assert len(state['requests'])==1 and not state['requests'][0]['task_verified']

def test_verification_preview_and_history_are_tenant_and_task_bound(verification_api):
    b=verification_api
    assert b['client'].get(path(b,'verification').replace(b['task'],'foreign-task'),headers=headers()).status_code==404
    assert b['client'].get(path(b,'verification-preview').replace(b['run'],'unknown-run',1),headers=headers()).status_code==404
    before=b['client'].get('/v1/verification-capabilities',headers=headers()).json()
    b['verification_config'].path.write_text('{}',encoding='utf-8')
    assert before['configured'] and b['client'].get('/v1/verification-capabilities',headers=headers()).status_code==409

def test_public_boundary_allows_only_request_not_signature_or_key_upload():
    assert PublicPreview.allowed('POST','/v1/runs/run/tasks/run:work/verification-requests')
    for endpoint in ('verification/finish','verification-attestations','verification-keys','verification-pool'):
        assert not PublicPreview.allowed('POST','/v1/runs/run/tasks/run:work/'+endpoint)
    assert not PublicPreview.allowed('POST','/v1/verification-capabilities')

def test_runtime_historical_proofs_are_cryptographically_checked(verification_case,tmp_path):
    b=verification_case
    identities=tmp_path/'proof-identities.json';identities.write_text(json.dumps(dict(schema_version='loom.local-identities/v1',identities=[dict(tenant_id=i.tenant_id,subject_id=i.subject_id,roles=sorted(i.roles),token=i.token) for i in (OP,WORKER)])),encoding='utf-8')
    path_=script.configure(tmp_path/'proof-keys',tenant='a',identities=identities,model_config=ROOT/'configs/models.dual.example.json',model_secrets=tmp_path/'missing-project-key.json',db=b['store'].path,pool_limit=2000000)
    config=load_verification_config(path_);b['signing'],b['trusted']=config.load_signers()
    result,_=asyncio.run(run_job(b));assert result['state']=='verified'
    runtime=VerificationRuntime(b['store'],config);proof=runtime.history('a',b['run'],b['task'])
    assert proof['requests'][0]['task_verified'] and proof['requests'][0]['signatures_authenticated']
    assert proof['task_status']=='done' and len(proof['public_keys'])==2
    runtime.status.publish();assert runtime.capabilities('a')['executor']['state']=='recent'
