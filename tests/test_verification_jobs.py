import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest

from loom.auth import LocalIdentity
from loom.model_gateway import ModelGateway,ModelRoute
from loom.review_inputs import ReviewInputService
from loom.store import Conflict,InvalidInput,Store,canonical
from loom.verification_jobs import VerificationJobs
from loom.verification_signatures import TrustedVerifier
from loom.verification_worker import VerificationWorker
from tests.test_approved_execution import OP,WORKER
from tests.test_approved_node import node_case,execute

@pytest.fixture
def verification_case(node_case,tmp_path):
    b=node_case('developer');result,_=execute(b);assert result['execution']['status']=='submitted'
    jobs=VerificationJobs(b['store']);tenant='a';task=b['run']+':work'
    preview=jobs.preview(tenant,b['run'],task)
    request=jobs.request(OP,b['run'],task,'verify-one',expected_preview_digest=preview['preview_digest'],reason='独立双模型复核，另设具名项目预算，原生成预算保持')
    signing={model:Ed25519PrivateKey.generate() for model in ('deepseek-flash','deepseek-v4-pro')}
    trusted={model:TrustedVerifier(model,'verifier:'+model,model,key.public_key()) for model,key in signing.items()}
    b.update(jobs=jobs,job=request['job_id'],task=task,preview=preview,signing=signing,trusted=trusted,directory=tmp_path/'private-verification')
    return b

async def run_job(b,*,negative=False,timeout=False,callback=None):
    requests=[]
    def response(request):
        payload=json.loads(request.content);requests.append(payload)
        if callback:callback(len(requests))
        if timeout and payload['model']=='deepseek-v4-pro':raise httpx.ReadTimeout('synthetic verifier timeout',request=request)
        if negative and payload['model']=='deepseek-v4-pro':
            body=dict(schema_version='loom.review-findings/v1',subject=b['preview']['subject'],summary='发现问题',verdict='findings',
                findings=[dict(id='one',severity='minor',path='main.py',start_line=1,end_line=1,description='模拟意见',recommendation='核对原始需求')])
        else:body=dict(schema_version='loom.review-findings/v1',subject=b['preview']['subject'],summary='未发现明确问题',verdict='no_findings',findings=[])
        return httpx.Response(200,json=dict(id='verified-'+payload['model'],object='chat.completion',created=1792700000,model=payload['model'],
            choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content=canonical(body)))],
            usage=dict(prompt_tokens=25,completion_tokens=10,total_tokens=35)))
    async with ModelGateway([ModelRoute(model,model=model) for model in b['signing']],transport=httpx.MockTransport(response),environ={'DEEPSEEK_API_KEY':'verification-public-fixture-only'}) as gateway:
        worker=VerificationWorker(b['jobs'],gateway,directory=b['directory'],operator=OP,worker=WORKER,signing_keys=b['signing'],trusted_keys=b['trusted'])
        result=await worker.process('a',b['job'])
    return result,requests

def test_real_signatures_complete_source_and_registered_goal_from_known_model_receipts(verification_case):
    b=verification_case;before=b['store'].snapshot('a',b['run']);result,requests=asyncio.run(run_job(b))
    assert result['state']=='verified' and result['task_verified'] and result['goal_accepted'] and len(requests)==2
    after=b['store'].snapshot('a',b['run'])
    assert after['status']=='succeeded' and after['tasks'][0]['status']=='done'
    assert after['spent']==before['spent'] and after['reserved']==0
    assert result['actual_cost_microusd']==93
    assert b['store'].verify_events(b['store'].events('a',b['run']))
    # A verified implementation remains readable after its owner is cleared.
    with b['store'].connect() as db:
        view=ReviewInputService(b['store'])._preview_in(db,'a',b['run'],b['task'],True)
    assert view['execution_summary']['hard_criteria_passed']

def test_negative_pro_verdict_preserves_candidate_and_stops_goal(verification_case):
    b=verification_case
    with b['store'].connect() as db:original=db.execute('SELECT output FROM tasks WHERE tenant=? AND id=?',('a',b['task'])).fetchone()[0]
    result,requests=asyncio.run(run_job(b,negative=True))
    assert result['state']=='rejected' and not result['task_verified'] and len(requests)==2
    after=b['store'].snapshot('a',b['run'])
    assert after['status']=='awaiting_human' and after['tasks'][0]['status']=='submitted'
    with b['store'].connect() as db:assert db.execute('SELECT output FROM tasks WHERE tenant=? AND id=?',('a',b['task'])).fetchone()[0]==original

def test_unknown_pro_call_keeps_pool_reserved_and_is_not_automatically_retried(verification_case):
    b=verification_case;result,requests=asyncio.run(run_job(b,timeout=True))
    assert result['state']=='blocked' and result['billing_uncertain'] and len(requests)==2
    with b['store'].connect() as db:assert b['jobs']._pool(db,'a')['reserved_microusd']==b['jobs'].MAX_RESERVATION
    with pytest.raises(Conflict,match='NO_AUTOMATIC_RETRY'):asyncio.run(run_job(b))
    assert b['store'].snapshot('a',b['run'])['tasks'][0]['status']=='submitted'

def test_pause_during_review_blocks_source_completion_without_repeating(verification_case):
    from loom.operator import OperatorService
    b=verification_case
    def pause(n):
        if n==1:OperatorService(b['store']).pause('a',b['run'],'pause-verify','subject:operator','暂停复核')
    result,requests=asyncio.run(run_job(b,callback=pause))
    assert result['state']=='blocked' and not result['billing_uncertain'] and len(requests)==1
    assert b['store'].snapshot('a',b['run'])['status']=='paused'

def test_request_is_exact_idempotent_role_bound_and_new_keys_cannot_repeat(verification_case):
    b=verification_case;events=b['store'].events('a',b['run'])
    result=b['jobs'].request(OP,b['run'],b['task'],'verify-one',expected_preview_digest=b['preview']['preview_digest'],reason='独立双模型复核，另设具名项目预算，原生成预算保持')
    assert result['job_id']==b['job'] and b['store'].events('a',b['run'])==events
    with pytest.raises(Conflict):b['jobs'].request(OP,b['run'],b['task'],'another',expected_preview_digest=b['preview']['preview_digest'],reason='再次调用')
    with pytest.raises(InvalidInput):b['jobs'].request(WORKER,b['run'],b['task'],'worker',expected_preview_digest=b['preview']['preview_digest'],reason='无权')

def test_parallel_start_only_one_worker_and_budget_admission_is_atomic(verification_case):
    b=verification_case
    def start(_):
        try:return VerificationJobs(Store(b['store'].path)).start('a',b['job'])
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(start,range(2)))
    assert sum(result is not None for result in results)==1
    assert b['jobs'].state('a',b['job'])['state']=='running'

def test_insufficient_project_pool_never_starts_or_calls_models(verification_case):
    b=verification_case;b['jobs'].pool_limit=1
    with pytest.raises(Conflict,match='PROJECT_BUDGET'):asyncio.run(run_job(b))
    assert b['jobs'].state('a',b['job'])['state']=='queued'

def test_source_replacement_after_request_cannot_consume_old_verification(verification_case):
    b=verification_case;b['store'].invalidate('a',b['run'],b['task'],'来源已改变')
    with pytest.raises(Conflict):asyncio.run(run_job(b))
    assert b['jobs'].state('a',b['job'])['state']=='queued'

def test_attestation_and_task_completion_are_rolled_back_if_final_audit_fails(verification_case):
    b=verification_case;original=b['store']._event
    def fail(db,tenant,run,kind,*args,**kwargs):
        if kind=='verification.finished':raise RuntimeError('synthetic final audit write failure')
        return original(db,tenant,run,kind,*args,**kwargs)
    with patch.object(b['store'],'_event',side_effect=fail):result,requests=asyncio.run(run_job(b))
    assert result['state']=='blocked' and not result['billing_uncertain'] and len(requests)==2
    assert b['store'].snapshot('a',b['run'])['tasks'][0]['status']=='submitted'
    with b['store'].connect() as db:assert db.execute('SELECT count(*) FROM verification_attestations').fetchone()[0]==0

def test_unknown_accounting_is_not_erased_by_process_restart(verification_case):
    b=verification_case;result,_=asyncio.run(run_job(b,timeout=True))
    restarted=VerificationJobs(Store(b['store'].path))
    assert restarted.state('a',b['job'])['billing_uncertain']
    with restarted.store.connect() as db:assert restarted._pool(db,'a')['reserved_microusd']==1850000
    assert not restarted.pending('a')

def test_cross_tenant_state_does_not_expose_request(verification_case):
    from loom.store import NotFound
    b=verification_case
    with pytest.raises(NotFound):b['jobs'].state('b',b['job'])

def test_request_pointer_corruption_is_rejected_before_model_admission(verification_case):
    b=verification_case
    with b['store'].transaction() as db:
        db.execute('DROP TRIGGER verification_requests_no_update')
        other=db.execute('SELECT id FROM events WHERE tenant=? AND run_id=? ORDER BY seq LIMIT 1',('a',b['run'])).fetchone()[0]
        db.execute('UPDATE verification_requests SET event_id=? WHERE tenant=? AND id=?',(other,'a',b['job']))
    with pytest.raises(Conflict,match='LEDGER_INVALID'):asyncio.run(run_job(b))

def test_persisted_signed_proof_is_audited_again_when_read(verification_case):
    b=verification_case;result,_=asyncio.run(run_job(b))
    assert result['state']=='verified' and len(b['jobs'].attestations('a',b['job']))==2
    with b['store'].transaction() as db:
        db.execute('DROP TRIGGER verification_attestations_no_update')
        db.execute("UPDATE verification_attestations SET record='{}' WHERE model_id='deepseek-v4-pro'")
    with pytest.raises(Conflict,match='LEDGER_INVALID'):b['jobs'].attestations('a',b['job'])
