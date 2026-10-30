"""Source and validator have distinct test executions, bindings and signatures."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from loom.approved_execution import ApprovedExecutionService
from loom.artifacts import ArtifactService,ExecutionContextError
from loom.demo import fixture_verification
from loom.operator import OperatorService
from loom.policy import ArtifactSubmission
from loom.review_inputs import ReviewInputService
from loom.store import Claim,Conflict,InvalidInput,digest
from loom.validator_execution import ValidatorTaskRunner
from loom.verification_jobs import VerificationJobs
from loom.verification_signatures import VerificationError
from loom.workflow import WorkflowDefinition
from tests.test_approved_execution import OP,WORKER
from tests.test_approved_node import node_case,script,execute as execute_source
from tests.test_execution import FakeSandbox
from tests.test_verification_jobs import verification_case,run_job


@pytest.fixture
def validation_case(node_case,tmp_path):
    def make(*,verified=True,source_sandbox=None,with_reviewer=False):
        def node(role,**kwargs):return node_case(role,with_validator=True,with_reviewer=with_reviewer,**kwargs)
        with patch('tests.test_verification_jobs.execute',side_effect=lambda b:execute_source(b,sandbox=source_sandbox)):
            b=verification_case.__wrapped__(node,tmp_path)
        if verified:
            result,_=asyncio.run(run_job(b));assert result['task_verified'] and not result['goal_accepted']
        b['source_task']=b['task'];b['validator_task']=b['run']+':verify'
        b['validator_args']=SimpleNamespace(**vars(b['args']))
        b['validator_args'].node_id='verify';b['validator_args'].execute=False;b['validator_args'].preview_digest=None
        b['validator_args'].secrets_file=None
        return b
    return make


def execute_validator(b,sandbox=None):
    args=b['validator_args'];args.execute=False
    preview=asyncio.run(script.run(args));args.preview_digest=preview['preview_digest'];args.execute=True
    args.identities_file=Path('fixture-identities-not-read.json');args.sandbox_image='sha256:'+'a'*64
    with patch.object(script,'load_identities',return_value=(OP,WORKER)),patch.object(script,'load_project_secrets',side_effect=AssertionError('validator must not read model secrets')),\
            patch.object(script,'load_model_config',side_effect=AssertionError('no generation routes needed')),\
            patch.object(script,'ModelGateway',side_effect=AssertionError('no generation request allowed')),\
            patch.object(script,'DockerSandbox',return_value=sandbox or FakeSandbox()):
        return asyncio.run(script.run(args)),preview


def validator_review_case(b,directory):
    jobs=b['jobs'];task=b['validator_task'];preview=jobs.preview('a',b['run'],task)
    request=jobs.request(OP,b['run'],task,'verify-validator',expected_preview_digest=preview['preview_digest'],reason='本轮终验必须获得新的任务绑定双签')
    return {**b,'task':task,'preview':preview,'job':request['job_id'],'directory':directory}


def test_validator_uses_exact_source_without_model_key_and_does_not_finish_itself(validation_case):
    b=validation_case();before=b['store'].snapshot('a',b['run']);sandbox=FakeSandbox()
    result,preview=execute_validator(b,sandbox)
    assert preview['execution_kind']=='python-validation'
    assert preview['initial_cost_reservation_upper']==preview['initial_token_reservation']==0
    assert result['passed'] and not result['execution']['generation_model_used'] and not result['verified']
    assert sandbox.execution_calls==1 and len(sandbox.inputs)==1
    with b['store'].connect() as db:
        source=json.loads(db.execute('SELECT output FROM tasks WHERE id=?',(b['source_task'],)).fetchone()[0])
        output=json.loads(db.execute('SELECT output FROM tasks WHERE id=?',(b['validator_task'],)).fetchone()[0])
        assert db.execute('SELECT count(*) FROM effects WHERE task_id=?',(b['validator_task'],)).fetchone()[0]==0
    assert output['source']==source['source'] and output['validation_source']['subject']['task_id']==b['source_task']
    assert sandbox.inputs[0][0]=={'main.py':source['source']}
    after=b['store'].snapshot('a',b['run']);assert before['spent']==after['spent'] and before['tokens_spent']==after['tokens_spent']
    assert next(t for t in after['tasks'] if t['id']==b['validator_task'])['status']=='submitted'
    assert after['status']=='open'


def test_fresh_validator_signatures_finish_goal_and_history_remains_readable(validation_case,tmp_path):
    b=validation_case();execute_validator(b)
    original=b['jobs'].attestations('a',b['job'])
    current=validator_review_case(b,tmp_path/'validator-verifiers')
    assert current['preview']['binding']['task_id']!=original[0]['body']['binding']['task_id']
    result,requests=asyncio.run(run_job(current))
    assert result['state']=='verified' and result['task_verified'] and result['goal_accepted'] and len(requests)==2
    assert b['store'].snapshot('a',b['run'])['status']=='succeeded'
    assert b['store'].verify_events(b['store'].events('a',b['run']))
    view=ReviewInputService(b['store']).preview('a',b['run'],b['validator_task'],include_source=True)
    assert view['execution_summary']['cases_executed']==1 and view['execution_summary']['hard_criteria_passed']
    with pytest.raises(Conflict):b['jobs'].preview('a',b['run'],b['validator_task'])


def test_source_signature_pair_cannot_be_reused_as_validator_signatures(validation_case,tmp_path):
    b=validation_case();execute_validator(b);current=validator_review_case(b,tmp_path/'no-reuse')
    b['jobs'].start('a',current['job'])
    with pytest.raises(VerificationError):
        b['jobs'].finish('a',current['job'],b['jobs'].attestations('a',b['job']),{k.key_id:k for k in b['trusted'].values()},actual_cost=0)
    assert b['jobs'].state('a',current['job'])['state']=='running'
    assert next(t for t in b['store'].snapshot('a',b['run'])['tasks'] if t['id']==b['validator_task'])['status']=='submitted'


@pytest.mark.parametrize('negative',[True,'both'])
def test_validator_model_disagreement_or_rejection_never_finishes_goal(validation_case,tmp_path,negative):
    b=validation_case();execute_validator(b);current=validator_review_case(b,tmp_path/'negative-validator')
    result,_=asyncio.run(run_job(current,negative=negative))
    assert result['state']==('disputed' if negative is True else 'rejected') and not result['task_verified']
    assert b['store'].snapshot('a',b['run'])['status']=='awaiting_human'
    assert ReviewInputService(b['store']).preview('a',b['run'],b['validator_task'],include_source=True)['files']['main.py']


def test_unsigned_source_cannot_authorize_validator(validation_case):
    b=validation_case(verified=False)
    with pytest.raises(Conflict):asyncio.run(script.run(b['validator_args']))
    claim=Claim('a',b['run'],b['source_task'],WORKER.subject_id,1,0)
    with b['store'].connect() as db:output=json.loads(db.execute('SELECT output FROM tasks WHERE id=?',(b['source_task'],)).fetchone()[0])
    b['store'].complete(claim,'legacy-complete',output,verification=fixture_verification(claim,output))
    with pytest.raises(Conflict):asyncio.run(script.run(b['validator_args']))


@pytest.mark.parametrize('mutation',['source','digest','dependency','role','attempts','self'])
def test_validator_plan_rejects_invalid_source_contract(validation_case,mutation):
    b=validation_case(verified=False);definition=deepcopy(b['workflow'].get_plan('a',b['run'])['definition']);node=definition['nodes'][-1]
    if mutation=='source':node['validation_source']['source_node']='absent'
    elif mutation=='digest':node['evaluation_digest']='0'*64
    elif mutation=='dependency':node['depends']=[]
    elif mutation=='role':node.update(role='developer',channel='business')
    elif mutation=='attempts':node['limits']['max_attempts']=2
    else:node['validation_source']['source_node']='verify';node['depends']=['verify']
    with pytest.raises(ValueError):WorkflowDefinition.model_validate(definition)


def test_validator_preview_change_after_source_invalidation_blocks_claim(validation_case):
    b=validation_case();args=b['validator_args'];preview=asyncio.run(script.run(args))
    b['store'].invalidate('a',b['run'],b['source_task'],'撤销源产物')
    with pytest.raises(Conflict):ApprovedExecutionService(b['store']).claim('a',b['run'],'verify',b['saved']['digest'],b['tests'],
        preview['preview_digest'],operator=OP,worker=WORKER,max_output_tokens=1024)


@pytest.mark.parametrize('failure',['tests','preflight','tool','binding'])
def test_validator_failure_is_held_for_human_without_auto_retry(validation_case,failure):
    b=validation_case();sandbox=FakeSandbox(passed=failure!='tests',preflight_error=failure=='preflight',execute_error=failure=='tool',
        bad_binding='source' if failure=='binding' else None)
    result,_=execute_validator(b,sandbox)
    assert not result['passed']
    after=b['store'].snapshot('a',b['run']);assert after['status']=='awaiting_human'
    assert next(t for t in after['tasks'] if t['id']==b['validator_task'])['status']=='awaiting_human'
    with pytest.raises(Conflict):asyncio.run(script.run(b['validator_args']))


def test_none_snapshot_cannot_submit_validator_output(validation_case):
    b=validation_case();service=ApprovedExecutionService(b['store']);preview=asyncio.run(script.run(b['validator_args']))
    claim=service.claim('a',b['run'],'verify',b['saved']['digest'],b['tests'],preview['preview_digest'],operator=OP,worker=WORKER,max_output_tokens=1024)
    with pytest.raises(ExecutionContextError):ArtifactService(b['store']).submit_execution_output(claim,'bad',{},None)


def test_validator_uses_reserved_channel_when_business_capacity_is_full(validation_case):
    b=validation_case();other=b['store'].create_run('a','busy',acceptance=['tests.pass'])['run_id']
    b['store'].add_task('a',other,'busy-task',task_id='busy',title='Occupy business capacity')
    assert b['store'].claim_next('a',other,'busy-worker')
    result,_=execute_validator(b)
    assert result['passed']


def configure_dispatch(b,directory):
    from loom.dispatch import DispatchService
    from loom.executor_config import LocalExecutorConfig
    config=LocalExecutorConfig('validator-local',directory/'absent-identities',directory/'absent-key',directory/'absent-routes',
        'sha256:'+'a'*64,{b['saved']['definition']['nodes'][0]['evaluation_digest']:b['args'].tests_file})
    service=DispatchService(b['store'],config=config,identities=(OP,WORKER))
    return config,service


def drain_validator(b,config,sandbox):
    from loom.local_executor import LocalExecutor
    with patch('loom.local_executor.load_identities',return_value=(OP,WORKER)),\
            patch('loom.local_executor.load_project_secrets',side_effect=AssertionError('no key read')) ,\
            patch('loom.local_executor.load_model_config',side_effect=AssertionError('no routes read')),\
            patch('loom.local_executor.ModelGateway',side_effect=AssertionError('no generation gateway')),\
            patch('loom.local_executor.DockerSandbox',return_value=sandbox):
        executor=LocalExecutor(b['store'],config)
        result=asyncio.run(executor.drain_one())
        assert asyncio.run(executor.drain_one()) is None
        return result


def test_http_queue_and_private_worker_validate_without_model_configuration(validation_case,tmp_path):
    from fastapi.testclient import TestClient
    from loom.api import create_app
    b=validation_case();config,dispatch=configure_dispatch(b,tmp_path)
    path='/v1/runs/'+b['run']+'/tasks/'+b['validator_task']
    headers={'Authorization':'Bearer '+OP.token,'Idempotency-Key':'validator-http'}
    with TestClient(create_app(b['store'],{},identities=(OP,WORKER),executor_config=config)) as client:
        progression=client.get('/v1/runs/'+b['run']+'/progression',headers=headers).json()
        node=next(n for n in progression['nodes'] if n['task_id']==b['validator_task'])
        assert node['requires_host_tests'] and node['stage']=='host_tests_required'
        review=client.get('/v1/runs/'+b['run']+'/review',headers=headers).json()
        assert next(t for t in review['tasks'] if t['id']==b['validator_task'])['validation_source']['source_node']=='work'
        preview=client.get(path+'/execution-preview',headers=headers).json()
        assert preview['ready'] and preview['approved_preview']['execution_kind']=='python-validation'
        body=dict(expected_dispatch_digest=preview['dispatch_digest'],reason='复验指定程序，生成费用零，之后仍需新的独立双签')
        assert client.post(path+'/execution-requests',headers={**headers,'Authorization':'Bearer '+WORKER.token},json=body).status_code==403
        response=client.post(path+'/execution-requests',headers=headers,json=body)
        assert response.status_code==201 and response.json()['state']=='queued'
        result=drain_validator(b,config,FakeSandbox())
        assert result['state']=='submitted' and result['result']['model_calls']==result['result']['settled_calls']==0
        assert result['result']['reserved_cost']==result['result']['reserved_tokens']==0
        assert not result['independently_verified'] and not result['counts_as_success']


@pytest.mark.parametrize('failure',['preflight','unknown','quota','pause','invalidation'])
def test_validator_worker_fences_failure_and_does_not_repeat(validation_case,tmp_path,failure):
    from loom.quotas import Limit,QuotaService
    from tests.test_quotas import configure,POLICY
    b=validation_case();config,dispatch=configure_dispatch(b,tmp_path)
    configure(b['store'],replace(POLICY,tool=Limit(0 if failure=='quota' else 10,1)))
    sandbox=FakeSandbox(preflight_error=failure=='preflight',execute_error=failure=='unknown')
    original=sandbox.run_python
    def controlled(files,tests):
        result=original(files,tests)
        if failure=='pause':OperatorService(b['store']).pause('a',b['run'],'pause-validator','subject:operator','暂停后不得提交终验')
        if failure=='invalidation':b['store'].invalidate('a',b['run'],b['source_task'],'终验期间撤销源程序')
        return result
    sandbox.run_python=controlled
    preview=dispatch.preview('a',b['run'],b['validator_task'])
    dispatch.enqueue('a',b['run'],b['validator_task'],'validator-request',actor='subject:operator',
        expected_dispatch_digest=preview['dispatch_digest'],reason='测试终验中断与恢复')
    result=drain_validator(b,config,sandbox)
    # Invalidating the source fences the descendant epoch while its tool is in
    # flight; a late receipt cannot settle that old epoch or release its slot.
    unknown=failure in {'unknown','invalidation'}
    assert result['state']==('blocked' if failure=='preflight' else 'indeterminate' if unknown else 'awaiting_human')
    assert sandbox.execution_calls==(0 if failure in {'preflight','quota'} else 1)
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=0 if failure in {'preflight','quota'} else 1,in_flight=1 if unknown else 0)
    with b['store'].connect() as db:
        assert db.execute('SELECT output FROM tasks WHERE id=?',(b['validator_task'],)).fetchone()[0] is None
