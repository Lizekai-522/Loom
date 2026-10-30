"""Primary review quality is independently verified against its original source."""
import asyncio
from copy import deepcopy
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from loom.review_inputs import ReviewInputService
from loom.approved_execution import ApprovedExecutionService
from loom.builtin_resources import software_workflow
from loom.store import Conflict, digest
from loom.verification_jobs import VerificationJobs
from loom.verification_signatures import TrustedVerifier
from tests.test_approved_execution import OP
from tests.test_reviewer_handoff import handoff, reviewer_claim, generate, approve_fix
from tests.test_review_inputs import review_case
from tests.test_verification_jobs import run_job
from tests.test_approved_node import node_case, execute


def prepare_reviewer_verification(case, directory, *, no_findings=False):
    original_run=case['run']
    if no_findings:case['body'].update(verdict='no_findings',findings=[],summary='初次审查无明确问题')
    result=asyncio.run(generate(case,reviewer_claim(case)))
    assert result['status']=='submitted'
    jobs=VerificationJobs(case['store']);run=case['target'];task=run+':review'
    preview=jobs.preview('a',run,task)
    request=jobs.request(OP,run,task,'reviewer-verification',expected_preview_digest=preview['preview_digest'],reason='核对原审查是否遗漏或误报，保留原始材料')
    signing={model:Ed25519PrivateKey.generate() for model in ('deepseek-flash','deepseek-v4-pro')}
    trusted={model:TrustedVerifier(model,'verifier:'+model,model,key.public_key()) for model,key in signing.items()}
    case.update(run=run,original_run=original_run,jobs=jobs,task=task,job=request['job_id'],preview=preview,
        signing=signing,trusted=trusted,directory=directory)
    return case


@pytest.mark.parametrize('no_findings',[False,True])
def test_review_quality_requires_both_models_and_complete_original_material(handoff,tmp_path,no_findings):
    b=prepare_reviewer_verification(handoff,tmp_path/'primary-review-verifiers',no_findings=no_findings)
    view=ReviewInputService(b['store']).preview('a',b['run'],b['task'],include_source=True)
    assert view['subject']['artifact_kind']=='review-findings' and view['subject']['schema_version']=='loom.review-subject-ref/v2'
    assert set(view['files'])=={'artifact.json','review-context.json'}
    assert view['subject']['source_sha256']==digest(view['files'])
    original=json.loads(view['files']['review-context.json'])
    assert original['subject']==b['body']['subject'] and original['subject']['run_id']==b['original_run']
    assert original['files']['main.py']
    assert original['original_requirements']['description']=='只审查已提交的代码'
    assert original['original_requirements']['instruction']=='加一'
    assert original['execution_summary']['cases_executed']==1
    assert view['execution_summary']['cases_executed']==0  # Reviewing text never executes the original program again.
    for marker in ('hidden-name-marker','hidden-input','secret-golden'):
        assert marker not in json.dumps(view,ensure_ascii=False)
    before=b['store'].snapshot('a',b['run'])
    result,requests=asyncio.run(run_job(b))
    assert result['state']=='verified' and result['task_verified'] and not result['goal_accepted']
    assert len(requests)==2
    for request in requests:
        text=json.dumps(request,ensure_ascii=False)
        assert 'review-context.json' in text and '只审查已提交的代码' in text
        assert 'hidden-input' not in text and 'secret-golden' not in text
    assert '未发现明确问题' not in json.dumps(requests[1],ensure_ascii=False)
    after=b['store'].snapshot('a',b['run'])
    assert after['spent']==before['spent'] and after['reserved']==0
    assert {task['id']:task['status'] for task in after['tasks']}[b['task']]=='done'
    assert any(task['status']=='pending' for task in after['tasks'])


def test_disputed_review_quality_keeps_report_submitted_and_does_not_change_original(handoff,tmp_path):
    b=prepare_reviewer_verification(handoff,tmp_path/'disputed-review')
    original=b['store'].snapshot('a',b['original_run'])
    result,requests=asyncio.run(run_job(b,negative=True))
    assert result['state']=='disputed' and not result['task_verified'] and len(requests)==2
    assert b['store'].snapshot('a',b['run'])['status']=='awaiting_human'
    assert b['store'].snapshot('a',b['original_run'])==original


def test_changed_original_source_blocks_before_any_independent_model_call(handoff,tmp_path):
    b=prepare_reviewer_verification(handoff,tmp_path/'changed-original')
    b['store'].invalidate('a',b['original_run'],b['original_run']+':work','来源修订')
    result,requests=asyncio.run(run_job(b))
    assert result['state']=='blocked' and not requests and not result.get('task_verified',False)
    assert next(t for t in b['store'].snapshot('a',b['run'])['tasks'] if t['id']==b['task'])['status']=='submitted'


def test_changed_original_goal_projection_cannot_change_review_requirements(handoff,tmp_path):
    b=prepare_reviewer_verification(handoff,tmp_path/'changed-goal')
    with b['store'].transaction() as db:
        db.execute("UPDATE workflow_goals SET description='rewritten requirements' WHERE tenant='a' AND run_id=?",(b['original_run'],))
    result,requests=asyncio.run(run_job(b))
    assert result['state']=='blocked' and not requests


def test_revoking_original_after_first_review_stops_second_model(handoff,tmp_path):
    b=prepare_reviewer_verification(handoff,tmp_path/'mid-review-revoke')
    def revoke(count):
        if count==1:b['store'].invalidate('a',b['original_run'],b['original_run']+':work','来源撤销')
    result,requests=asyncio.run(run_job(b,callback=revoke))
    assert result['state']=='blocked' and len(requests)==1 and not result.get('task_verified',False)


def test_unknown_second_review_call_keeps_hold_and_forbids_replay(handoff,tmp_path):
    b=prepare_reviewer_verification(handoff,tmp_path/'unknown-review')
    result,requests=asyncio.run(run_job(b,timeout=True))
    assert result['state']=='blocked' and result['billing_uncertain'] and len(requests)==2
    with b['store'].connect() as db:assert b['jobs']._pool(db,'a')['reserved_microusd']==b['jobs'].MAX_RESERVATION
    with pytest.raises(Conflict,match='NO_AUTOMATIC_RETRY'):asyncio.run(run_job(b))


def test_verified_review_still_needs_explicit_fixer_input_approval(handoff,tmp_path):
    b=prepare_reviewer_verification(handoff,tmp_path/'verified-fix-input')
    result,_=asyncio.run(run_job(b));assert result['task_verified']
    artifact=b['artifacts'].list_artifacts('a',b['target'])[0]
    assert artifact['stale'] is False
    with pytest.raises(Conflict):b['admission'].preview('a',b['target'],'fix',b['saved']['digest'],b['suite'])
    approve_fix(b,artifact)
    preview=b['admission'].preview('a',b['target'],'fix',b['saved']['digest'],b['suite'])
    assert preview['role']=='fixer'
    assert next(t for t in b['store'].snapshot('a',b['target'])['tasks'] if t['id']==b['target']+':fix')['status']=='pending'


def test_recursive_review_of_review_does_not_expand_without_bound(handoff,tmp_path):
    b=prepare_reviewer_verification(handoff,tmp_path/'bounded-review')
    definition=deepcopy(b['definition'])
    definition['nodes']=[dict(id='review',title='再次审查',instruction='只核对原报告',role='reviewer',
        review_subject=b['preview']['subject'],role_output=dict(kind='review-findings',schema_version='loom.review-findings/v1'),
        limits=dict(cost_limit_microusd=1000000,token_limit=2000000,max_attempts=1))]
    goal=b['workflow'].create_goal('a','nested-goal',title='Bounded',description='Only one primary report',acceptance=['schema.valid'],cost_limit=1000000,token_limit=2000000)
    run=goal['run_id'];saved=b['workflow'].save_plan('a',run,'nested-plan',definition,0,actor='subject:operator')
    b['workflow'].approve_plan('a',run,'nested-approve',saved['revision'],saved['digest'],actor='subject:operator')
    nested={**b,'target':run,'saved':saved,'requests':[],'body':dict(schema_version='loom.review-findings/v1',subject=b['preview']['subject'],
        summary='对报告的再次检查',verdict='no_findings',findings=[])}
    generated=asyncio.run(generate(nested,reviewer_claim(nested)))
    assert generated['status']=='submitted'
    with pytest.raises(Conflict,match='nested review reports'):
        ReviewInputService(b['store']).preview('a',run,run+':review',include_source=True)


def test_review_of_typed_document_keeps_its_source_and_can_finish_schema_goal(node_case,tmp_path):
    b=node_case('analyst',acceptance=['schema.valid','report.traceable']);execute(b)
    subject=ReviewInputService(b['store']).preview('a',b['run'],b['run']+':work')['subject']
    definition=software_workflow();definition['nodes']=[dict(id='review',title='审查需求',instruction='核对原始需求',role='reviewer',
        review_subject=subject,role_output=dict(kind='review-findings',schema_version='loom.review-findings/v1'),
        limits=dict(cost_limit_microusd=1000000,token_limit=2000000,max_attempts=1))]
    goal=b['workflow'].create_goal('a','doc-review',title='需求审查',description='审查文档质量',acceptance=['schema.valid','report.traceable'],cost_limit=1000000,token_limit=2000000)
    saved=b['workflow'].save_plan('a',goal['run_id'],'doc-review-plan',definition,0,actor='subject:operator')
    b['workflow'].approve_plan('a',goal['run_id'],'doc-review-approve',saved['revision'],saved['digest'],actor='subject:operator')
    b.update(target=goal['run_id'],saved=saved,definition=definition,admission=ApprovedExecutionService(b['store']),requests=[],callback=None,
        body=dict(schema_version='loom.review-findings/v1',subject=subject,summary='核对需求',verdict='no_findings',findings=[]))
    b=prepare_reviewer_verification(b,tmp_path/'typed-document-review')
    view=ReviewInputService(b['store']).preview('a',b['run'],b['task'],include_source=True)
    original=json.loads(view['files']['review-context.json'])
    assert set(original['files'])=={'artifact.json'} and original['subject']['artifact_kind']=='requirements'
    assert original['execution_summary']['cases_executed']==0
    result,requests=asyncio.run(run_job(b))
    assert result['task_verified'] and result['goal_accepted'] and len(requests)==2
    assert b['store'].snapshot('a',b['run'])['status']=='succeeded'
    assert ReviewInputService(b['store']).preview('a',b['run'],b['task'],include_source=True)['files']==view['files']
