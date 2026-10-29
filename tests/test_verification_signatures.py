from copy import deepcopy
from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.policy import ArtifactSubmission,GateRules,gate_v
from loom.store import Store
from loom.verification_signatures import TrustedVerifier,VerificationBinding,VerificationError,sign_verdict,verify_pair
from tests.test_approved_execution import OP,WORKER
from tests.test_public_access import public,login

@pytest.fixture
def pair():
    binding=VerificationBinding(tenant_id='a',run_id='run',task_id='run:task',epoch=1,plan_digest='1'*64,
        content_hash='2'*64,ruleset_version='kernel-v1',evaluation_digest='3'*64,evaluation_event_hash='4'*64,
        submission_event_hash='5'*64,input_digest='6'*64)
    private=[Ed25519PrivateKey.generate() for _ in range(2)];models=('deepseek-flash','deepseek-v4-pro')
    keys={};verdicts=[]
    for n,(key,model) in enumerate(zip(private,models)):
        key_id='key-'+str(n);identity='verifier-'+str(n)
        keys[key_id]=TrustedVerifier(key_id,identity,model,key.public_key())
        body=dict(key_id=key_id,verifier_id=identity,binding=binding.model_dump(),
            receipt=dict(provider='deepseek',model_id=model,call_id=str(7+n)*64,request_hash='a'*64,response_hash='b'*64,candidate_hash='c'*64),
            passed=True,issued_at=1000,expires_at=1300)
        verdicts.append(sign_verdict(body,key))
    return binding,private,keys,verdicts

def test_valid_pair_cryptographically_authenticates_exact_gate_v_fields(pair):
    binding,_,keys,verdicts=pair;signatures=verify_pair(verdicts,binding,keys,now=1000)
    assert all(s.authenticated for s in signatures)
    artifact=ArtifactSubmission(binding.content_hash,binding.ruleset_version,'run:task:1','local-host://frozen-evidence',True,True,'signed',signatures)
    assert gate_v(artifact,rules=GateRules(version=binding.ruleset_version)).passed

@pytest.mark.parametrize('field,value',[('tenant_id','b'),('run_id','other'),('task_id','run:other'),('epoch',2),('plan_digest','0'*64),('content_hash','0'*64),('ruleset_version','other'),('evaluation_digest','0'*64),('evaluation_event_hash','0'*64),('submission_event_hash','0'*64),('input_digest','0'*64)])
def test_every_binding_field_is_exact_and_signed(pair,field,value):
    binding,_,keys,verdicts=pair
    changed=binding.model_dump();changed[field]=value
    with pytest.raises(VerificationError):verify_pair(verdicts,changed,keys,now=1100)
    tampered=deepcopy(verdicts);tampered[0]['body']['binding'][field]=value
    with pytest.raises(VerificationError):verify_pair(tampered,changed,keys,now=1100)

@pytest.mark.parametrize('change',['signature','passed','model','request','unknown-field','authenticated'])
def test_claim_mutation_and_caller_authentication_flags_are_rejected(pair,change):
    binding,_,keys,verdicts=pair;changed=deepcopy(verdicts)
    if change=='signature':changed[0]['signature']='A'*88
    elif change=='passed':changed[0]['body']['passed']=False
    elif change=='model':changed[0]['body']['receipt']['model_id']='deepseek-v4-pro'
    elif change=='request':changed[0]['body']['receipt']['request_hash']='d'*64
    elif change=='unknown-field':changed[0]['body']['extra']=True
    else:changed[0]['body']['authenticated']=True
    with pytest.raises(VerificationError):verify_pair(changed,binding,keys,now=1100)

@pytest.mark.parametrize('now',[999,1300,1500,True])
def test_expired_future_or_non_integer_clock_is_rejected(pair,now):
    binding,_,keys,verdicts=pair
    with pytest.raises(VerificationError):verify_pair(verdicts,binding,keys,now=now)

def test_revoked_unknown_and_wrong_model_key_are_rejected(pair):
    binding,_,keys,verdicts=pair
    for changed in ({},{**keys,'key-0':replace(keys['key-0'],revoked=True)},
        {**keys,'key-0':replace(keys['key-0'],model_id='deepseek-v4-pro')},
        {**keys,'key-0':replace(keys['key-0'],signing_not_before=1001)}):
        with pytest.raises(VerificationError):verify_pair(verdicts,binding,changed,now=1100)

def test_same_key_material_or_model_cannot_claim_independence(pair):
    binding,private,keys,verdicts=pair
    same_key={**keys,'key-1':replace(keys['key-1'],public_key=private[0].public_key())}
    changed=[verdicts[0],sign_verdict(verdicts[1]['body'],private[0])]
    with pytest.raises(VerificationError):verify_pair(changed,binding,same_key,now=1100)
    changed=deepcopy(verdicts);changed[1]['body']['receipt']['model_id']='deepseek-flash'
    changed[1]=sign_verdict(changed[1]['body'],private[1])
    with pytest.raises(VerificationError):verify_pair(changed,binding,{**keys,'key-1':replace(keys['key-1'],model_id='deepseek-flash')},now=1100)

def test_authentic_negative_verdict_is_not_a_gate_pass(pair):
    binding,private,keys,verdicts=pair;body=deepcopy(verdicts[1]['body']);body['passed']=False
    verdicts[1]=sign_verdict(body,private[1]);signatures=verify_pair(verdicts,binding,keys,now=1100)
    assert not signatures[1].passed
    result=gate_v(ArtifactSubmission(binding.content_hash,'kernel-v1','run:task:1','host://evidence',True,True,'signed',signatures),rules=GateRules(version='kernel-v1'))
    assert not result.passed and result.reason=='verification_rejected'

def test_public_capability_is_authenticated_read_only_and_not_configured(tmp_path):
    with TestClient(create_app(Store(tmp_path/'caps.db'),{},identities=(OP,WORKER))) as client:
        assert client.get('/v1/verification-capabilities').status_code==401
        response=client.get('/v1/verification-capabilities',headers={'Authorization':'Bearer '+OP.token})
        assert response.status_code==200
        data=response.json();assert data['algorithm']=='Ed25519' and not data['completion_connected'] and not data['configured']
        assert not data['model_dispatched'] and not data['counts_as_success']

def test_invited_public_capability_read_never_grants_registration_or_completion(public):
    client,_,_=public;login(client)
    assert client.get('/v1/verification-capabilities').status_code==200
    assert client.post('/v1/verification-capabilities',json={},headers={'Origin':str(client.base_url).rstrip('/')}).status_code==403
