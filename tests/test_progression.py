import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.progression import ProgressionService,ROLES
from loom.review_decisions import ReviewDecisionService
from loom.store import Conflict,NotFound
from tests.test_approved_execution import OP,WORKER
from tests.test_approved_node import node_case,execute
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff,generate,reviewer_claim


@pytest.mark.parametrize('role',['analyst','architect','tester','developer','validator'])
def test_role_guidance_is_read_only_and_keeps_all_seven_roles_visible(node_case,role):
    b=node_case(role);before=b['store'].events('a',b['run'])
    data=ProgressionService(b['store']).read('a',b['run'])
    assert [r['role'] for r in data['roles']]==list(ROLES)
    assert sum(r['task_count'] for r in data['roles'])==1
    assert data['nodes'][0]['stage']==('independent_model_pending' if role=='validator' else 'host_tests_required' if role=='developer' else 'local_execution_ready')
    assert not data['counts_as_success'] and not data['model_dispatched']
    assert b['store'].events('a',b['run'])==before


def test_review_findings_guide_to_precise_decision_and_approved_fixer(handoff):
    b=handoff;result=asyncio.run(generate(b,reviewer_claim(b)));ReviewDecisionService(b['store']);service=ProgressionService(b['store'])
    nodes={n['role']:n for n in service.read('a',b['target'])['nodes']}
    assert nodes['reviewer']['action']=='review_decision' and nodes['reviewer']['artifact_id']==result['artifact_id']
    assert nodes['fixer']['stage']=='input_approval_required' and nodes['fixer']['suggested_inputs'][0]['artifact_id']==result['artifact_id']
    decisions=ReviewDecisionService(b['store']);p=decisions.preview('a',b['target'],result['artifact_id'])
    decisions.decide('a',b['target'],result['artifact_id'],'progress-route',actor='subject:operator',expected_preview_digest=p['preview_digest'],decision='approve_fixer',reason='核对意见后准备单次修复',target_task_id=b['target']+':fix')
    nodes={n['role']:n for n in service.read('a',b['target'])['nodes']}
    assert nodes['reviewer']['stage']=='fixer_input_approved' and nodes['fixer']['stage']=='host_tests_required'
    assert nodes['fixer']['status']=='pending' and nodes['validator']['blocked_by']


def test_no_findings_does_not_offer_a_fixer_approval(handoff):
    b=handoff;b['body'].update(verdict='no_findings',findings=[]);asyncio.run(generate(b,reviewer_claim(b)));ReviewDecisionService(b['store'])
    nodes={n['role']:n for n in ProgressionService(b['store']).read('a',b['target'])['nodes']}
    assert nodes['fixer']['stage']=='no_fix_findings' and nodes['fixer']['suggested_inputs']==[]
    assert not nodes['fixer']['ready_for_preview'] and nodes['fixer']['status']=='pending'


@pytest.mark.parametrize('state',['pause','unknown','reserved','dependency','epoch','budget','corrupt-contract'])
def test_guidance_never_labels_blocked_task_ready(node_case,state):
    b=node_case('analyst')
    with b['store'].transaction() as db:
        if state=='pause':db.execute("UPDATE runs SET status='paused'")
        elif state=='unknown':db.execute("INSERT INTO effects(tenant,id,run_id,task_id,epoch,request_hash,state) VALUES('a','unknown',?,?,0,?,'indeterminate')",(b['run'],b['run']+':work','0'*64))
        elif state=='reserved':db.execute("INSERT INTO reservations(tenant,id,run_id,task_id,epoch,cost,tokens,state) VALUES('a','hold',?,?,0,1,1,'reserved')",(b['run'],b['run']+':work'))
        elif state=='dependency':db.execute("UPDATE tasks SET depends='[\"missing\"]'")
        elif state=='epoch':db.execute('UPDATE tasks SET epoch=1')
        elif state=='budget':db.execute('UPDATE runs SET cost_limit=0')
        else:db.execute("UPDATE workflow_tasks SET contract='{}'")
    assert not ProgressionService(b['store']).read('a',b['run'])['nodes'][0]['ready_for_preview']


def test_metadata_has_no_candidate_bodies_code_prompts_or_hidden_tests(node_case):
    b=node_case('analyst');execute(b)
    data=ProgressionService(b['store']).read('a',b['run']);raw=json.dumps(data,ensure_ascii=False)
    for marker in ('需求候选','private-host','加一','prompt','secrets_file'):assert marker not in raw
    assert data['nodes'][0]['stage']=='independent_verification_required'


def test_progression_auth_tenant_bound_and_get_only(node_case):
    b=node_case('analyst');viewer=LocalIdentity('a','viewer',frozenset({'viewer'}),'progress-viewer-public-fixture-00000000')
    with TestClient(create_app(b['store'],{'b':'progress-other-public-fixture-00000000'},identities=(OP,WORKER,viewer))) as client:
        path='/v1/runs/'+b['run']+'/progression'
        assert client.get(path).status_code==401
        assert client.get(path,headers={'Authorization':'Bearer progress-other-public-fixture-00000000'}).status_code==404
        for identity in (OP,WORKER,viewer):
            assert client.get(path,headers={'Authorization':'Bearer '+identity.token}).status_code==200
            assert client.post(path,headers={'Authorization':'Bearer '+identity.token},json={}).status_code==405
