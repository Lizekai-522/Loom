"""Same-Goal selectors resolve to exact signed inputs, never mutable aliases."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from loom.approved_execution import ApprovedExecutionService
from loom.artifacts import ArtifactService,ExecutionContextError
from loom.operator import OperatorService
from loom.review_inputs import ReviewInputService
from loom.store import Conflict
from loom.workflow import WorkflowDefinition
from tests.test_approved_execution import OP,WORKER
from tests.test_approved_node import node_case,execute
from tests.test_validator_execution import validation_case,execute_validator,validator_review_case
from tests.test_verification_jobs import run_job


def review_body(b):
    subject=ReviewInputService(b['store']).preview('a',b['run'],b['source_task'],include_source=True)['subject']
    return dict(schema_version='loom.review-findings/v1',subject=subject,summary='已核对指定源码和原始需求',verdict='no_findings',findings=[])


def generate_review(b,body=None):
    args=SimpleNamespace(**vars(b['args']));args.node_id='review';args.tests_file=None;args.execute=False
    return execute({**b,'args':args},body=body or review_body(b))


def request_review_quality(b,directory):
    task=b['run']+':review';preview=b['jobs'].preview('a',b['run'],task)
    request=b['jobs'].request(OP,b['run'],task,'review-quality',expected_preview_digest=preview['preview_digest'],reason='独立核对审查意见与其原始材料')
    return {**b,'task':task,'preview':preview,'job':request['job_id'],'directory':directory}


def test_plan_can_be_approved_before_reviewed_program_exists(validation_case):
    b=validation_case(verified=False,with_reviewer=True)
    plan=b['workflow'].get_plan('a',b['run']);assert plan['status']=='approved'
    node=next(n for n in plan['definition']['nodes'] if n['id']=='review')
    assert 'review_subject' not in node and node['review_source']['source_node']=='work'
    with pytest.raises(Conflict):ApprovedExecutionService(b['store']).preview('a',b['run'],'review',b['saved']['digest'],None)


def test_review_reads_exact_same_goal_program_and_preserves_independent_gate(validation_case):
    b=validation_case(with_reviewer=True);result,calls=generate_review(b)
    assert result['passed'] and result['execution']['model_source_recorded'] and len(calls)==1
    incoming=json.loads(calls[0]['messages'][1]['content'])['auxiliary_inputs']['review_input']
    source=ReviewInputService(b['store']).preview('a',b['run'],b['source_task'],include_source=True)
    assert incoming['subject']==source['subject'] and incoming['files']==source['files']
    assert json.loads(calls[0]['messages'][1]['content'])['auxiliary_inputs']['original_requirements']==dict(
        description='加一',instruction='只处理批准需求',acceptance=['tests.pass'])
    assert 'private-host' not in json.dumps(calls) and 'expected' not in incoming
    view=ReviewInputService(b['store']).preview('a',b['run'],b['run']+':review',include_source=True)
    context=json.loads(view['files']['review-context.json'])
    assert context['subject']==source['subject'] and context['files']==source['files']
    assert context['original_requirements']['description']=='加一' and context['execution_summary']['cases_executed']==1
    assert view['execution_summary']['cases_executed']==0 and not view['independently_verified']
    assert next(t for t in b['store'].snapshot('a',b['run'])['tasks'] if t['id'].endswith(':review'))['status']=='submitted'


def test_program_review_and_validator_finish_same_goal_with_separate_signatures(validation_case,tmp_path):
    b=validation_case(with_reviewer=True);generate_review(b)
    with pytest.raises(Conflict):execute_validator(b)
    review=request_review_quality(b,tmp_path/'review-signatures');result,calls=asyncio.run(run_job(review))
    assert result['task_verified'] and not result['goal_accepted'] and len(calls)==2
    assert execute_validator(b)[0]['passed']
    validator=validator_review_case(b,tmp_path/'validator-signatures');result,_=asyncio.run(run_job(validator))
    assert result['goal_accepted'] and b['store'].snapshot('a',b['run'])['status']=='succeeded'
    assert len(b['store'].snapshot('a',b['run'])['tasks'])==3
    assert ReviewInputService(b['store']).preview('a',b['run'],b['run']+':review',include_source=True)['files']['review-context.json']


@pytest.mark.parametrize('mutation',['source','dependency','role','attempts','output','static','self','doc'])
def test_review_selector_rejects_invalid_or_ambiguous_plan(validation_case,mutation):
    b=validation_case(with_reviewer=True);definition=deepcopy(b['saved']['definition']);node=definition['nodes'][1]
    if mutation=='source':node['review_source']['source_node']='absent'
    elif mutation=='dependency':node['depends']=[]
    elif mutation=='role':node.update(role='validator',channel='verification')
    elif mutation=='attempts':node['limits']['max_attempts']=2
    elif mutation=='output':node.pop('role_output')
    elif mutation=='static':node['review_subject']=review_body(b)['subject']
    elif mutation=='self':node['review_source']['source_node']='review';node['depends']=['review']
    else:definition['nodes'][0].update(role='analyst',evaluation_digest=None,role_output=dict(kind='requirements',schema_version='loom.requirements/v1'))
    with pytest.raises(ValueError):WorkflowDefinition.model_validate(definition)


@pytest.mark.parametrize('change',['hash','task','epoch'])
def test_model_cannot_substitute_another_review_subject(validation_case,change):
    b=validation_case(with_reviewer=True);body=review_body(b)
    if change=='hash':body['subject']['content_hash']='0'*64
    elif change=='task':body['subject']['task_id']=b['run']+':verify'
    else:body['subject']['epoch']+=1
    result,calls=generate_review(b,body)
    assert not result['passed'] and len(calls)==1 and result['execution']['reason']=='MODEL_ROLE_OUTPUT_INVALID'


def test_source_revocation_between_preview_and_claim_rejects_without_generation(validation_case):
    b=validation_case(with_reviewer=True);service=ApprovedExecutionService(b['store']);params=('a',b['run'],'review',b['saved']['digest'],None)
    preview=service.preview(*params);b['store'].invalidate('a',b['run'],b['source_task'],'撤销源程序')
    with pytest.raises(Conflict):service.claim(*params,preview['preview_digest'],operator=OP,worker=WORKER)


def test_source_revocation_during_generation_does_not_submit_review(validation_case):
    from tests.test_approved_node import gateway_for,script
    b=validation_case(with_reviewer=True);original=gateway_for
    def gateway(body,calls):
        instance=original(body,calls);transport=instance._client._transport
        handle=transport.handler
        def revoke(request):
            result=handle(request);b['store'].invalidate('a',b['run'],b['source_task'],'生成期间撤销源程序');return result
        transport.handler=revoke;return instance
    with patch('tests.test_approved_node.gateway_for',side_effect=gateway):result,calls=generate_review(b)
    assert not result['passed'] and len(calls)==1
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM artifact_candidates WHERE source_task_id=?',(b['run']+':review',)).fetchone()[0]==0


@pytest.mark.parametrize('negative',[True,'both'])
def test_review_quality_dispute_keeps_validator_blocked(validation_case,tmp_path,negative):
    b=validation_case(with_reviewer=True);generate_review(b)
    current=request_review_quality(b,tmp_path/'disputed-review');result,_=asyncio.run(run_job(current,negative=negative))
    assert result['state']==('disputed' if negative is True else 'rejected') and not result['task_verified']
    assert b['store'].snapshot('a',b['run'])['status']=='awaiting_human'
    with pytest.raises(Conflict):execute_validator(b)


def test_review_cannot_omit_frozen_execution_snapshot(validation_case):
    b=validation_case(with_reviewer=True);service=ApprovedExecutionService(b['store']);params=('a',b['run'],'review',b['saved']['digest'],None)
    preview=service.preview(*params);claim=service.claim(*params,preview['preview_digest'],operator=OP,worker=WORKER)
    with pytest.raises(ExecutionContextError):ArtifactService(b['store']).submit_execution_output(claim,'no-snapshot',{},None)


def test_stale_same_goal_review_remains_readable_but_is_not_verifiable(validation_case):
    b=validation_case(with_reviewer=True);result,_=generate_review(b);artifact=result['execution']['artifact_id']
    before=ArtifactService(b['store']).get_artifact('a',b['run'],artifact)
    b['store'].invalidate('a',b['run'],b['source_task'],'撤销源后历史意见仍可审计')
    stale=ArtifactService(b['store']).get_artifact('a',b['run'],artifact)
    assert stale['stale'] and stale['body']==before['body']
    with pytest.raises(Conflict):b['jobs'].preview('a',b['run'],b['run']+':review')
