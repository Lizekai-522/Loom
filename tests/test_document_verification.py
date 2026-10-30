import asyncio
from dataclasses import replace
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from pydantic import ValidationError

from loom.artifact_schemas import ReviewSubjectReference,ReviewFindingsBody
from loom.review_inputs import ReviewInputService
from loom.store import Conflict,InvalidInput
from loom.verification_jobs import VerificationJobs
from loom.verification_signatures import TrustedVerifier
from tests.test_approved_execution import OP
from tests.test_approved_node import node_case,execute
from tests.test_verification_jobs import run_job

def document_case(node_case,tmp_path,role='analyst',*,acceptance=None):
    b=node_case(role,acceptance=acceptance);generated,_=execute(b)
    assert generated['execution']['status']=='submitted'
    jobs=VerificationJobs(b['store']);task=b['run']+':work';preview=jobs.preview('a',b['run'],task)
    req=jobs.request(OP,b['run'],task,'document-review',expected_preview_digest=preview['preview_digest'],reason='独立复核结构化产物，不能把schema有效当作语义正确')
    signing={model:Ed25519PrivateKey.generate() for model in ('deepseek-flash','deepseek-v4-pro')}
    trusted={model:TrustedVerifier(model,'verifier:'+model,model,key.public_key()) for model,key in signing.items()}
    b.update(jobs=jobs,job=req['job_id'],task=task,preview=preview,signing=signing,trusted=trusted,directory=tmp_path/'document-reviews')
    return b

@pytest.mark.parametrize('role',['analyst','architect','tester'])
def test_three_document_roles_need_real_crypto_and_do_not_fake_executed_tests(node_case,tmp_path,role):
    b=document_case(node_case,tmp_path,role)
    view=ReviewInputService(b['store']).preview('a',b['run'],b['task'],include_source=True)
    assert view['subject']['schema_version']=='loom.review-subject-ref/v2' and set(view['files'])=={'artifact.json'}
    assert view['execution_summary']['cases_executed']==0 and view['execution_summary']['evidence_source']=='local-executor-typed-schema'
    assert not view['independently_verified']
    result,requests=asyncio.run(run_job(b))
    assert result['task_verified'] and result['state']=='verified' and len(requests)==2
    assert not result['goal_accepted']  # Original Goal demanded tests.pass; zero tests cannot satisfy it.
    snapshot=b['store'].snapshot('a',b['run']);assert snapshot['status']=='open' and snapshot['tasks'][0]['status']=='done'
    assert '未发现明确问题' not in json.dumps(requests[1],ensure_ascii=False)  # Second model never receives the first verdict.
    assert ReviewInputService(b['store']).preview('a',b['run'],b['task'],include_source=True)['files']==view['files']

def test_document_goal_with_registered_schema_and_trace_criteria_can_finish(node_case,tmp_path):
    b=document_case(node_case,tmp_path,acceptance=['schema.valid','report.traceable'])
    result,_=asyncio.run(run_job(b));assert result['task_verified'] and result['goal_accepted']
    assert b['store'].snapshot('a',b['run'])['status']=='succeeded'
    assert ReviewInputService(b['store']).preview('a',b['run'],b['task'],include_source=True)['files']['artifact.json']
    from fastapi.testclient import TestClient
    from loom.api import create_app
    from tests.test_approved_execution import WORKER
    client=TestClient(create_app(b['store'],{},identities=(OP,WORKER)))
    review=client.get('/v1/runs/'+b['run']+'/review',headers={'Authorization':'Bearer '+OP.token}).json()
    assert review['tasks'][0]['artifact']['model_source_recorded']

def test_negative_semantic_review_preserves_document_and_requires_human(node_case,tmp_path):
    b=document_case(node_case,tmp_path)
    result,requests=asyncio.run(run_job(b,negative=True))
    assert result['state']=='disputed' and not result['task_verified'] and len(requests)==2
    assert b['store'].snapshot('a',b['run'])['status']=='awaiting_human'
    assert b['store'].snapshot('a',b['run'])['tasks'][0]['status']=='submitted'

def test_agreed_rejection_differs_from_dispute(node_case,tmp_path):
    b=document_case(node_case,tmp_path);result,_=asyncio.run(run_job(b,negative='both'))
    assert result['state']=='rejected' and result['reason']=='MODEL_REVIEW_REJECTED'
    assert not result['task_verified']

def test_document_schema_and_path_cannot_impersonate_program_evidence(node_case,tmp_path):
    b=document_case(node_case,tmp_path);ref=b['preview']['subject']
    with pytest.raises(ValidationError):ReviewSubjectReference.model_validate({**ref,'schema_version':'loom.review-subject-ref/v1'})
    with pytest.raises(ValidationError):ReviewSubjectReference.model_validate({k:v for k,v in ref.items() if k!='artifact_content_hash'})
    with pytest.raises(ValidationError):ReviewFindingsBody.model_validate(dict(schema_version='loom.review-findings/v1',subject=ref,summary='bad path',verdict='findings',findings=[dict(id='x',severity='major',path='main.py',start_line=1,end_line=1,description='x',recommendation='x')]))

def test_document_source_invalidation_prevents_calls(node_case,tmp_path):
    b=document_case(node_case,tmp_path);b['store'].invalidate('a',b['run'],b['task'],'new requirements')
    result,requests=asyncio.run(run_job(b));assert result['state']=='blocked' and not requests

def test_document_unknown_second_model_preserves_pool_and_never_replays(node_case,tmp_path):
    b=document_case(node_case,tmp_path);result,requests=asyncio.run(run_job(b,timeout=True))
    assert result['state']=='blocked' and result['billing_uncertain'] and len(requests)==2
    with pytest.raises(Conflict,match='NO_AUTOMATIC_RETRY'):asyncio.run(run_job(b))

def test_document_review_line_range_is_checked_against_exact_json(node_case,tmp_path):
    b=document_case(node_case,tmp_path);view=ReviewInputService(b['store']).preview('a',b['run'],b['task'],include_source=True)
    body=dict(schema_version='loom.review-findings/v1',subject=view['subject'],summary='line overflow',verdict='findings',findings=[dict(id='line',severity='major',path='artifact.json',start_line=1,end_line=262144,description='x',recommendation='x')])
    with pytest.raises(InvalidInput):ReviewInputService(b['store']).validate_findings('a',b['run'],b['task'],body)
