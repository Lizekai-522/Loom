import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from loom.approved_execution import ApprovedExecutionService
from loom.artifacts import ArtifactService
from loom.execution import freeze_tests
from loom.model_gateway import ModelGateway,ModelRoute
from loom.sandbox import FrozenTest
from loom.store import Store,Conflict,InvalidInput
from loom.workflow import WorkflowService
from tests.test_approved_execution import OP,WORKER
from tests.test_execution import FakeSandbox,SOURCE
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff,reviewer_claim,generate
from loom.review_decisions import ReviewDecisionService

ROOT=Path(__file__).resolve().parents[1]
module=importlib.util.spec_from_file_location('node_cli',ROOT/'scripts/run_approved_node.py')
script=importlib.util.module_from_spec(module);module.loader.exec_module(script)
BODIES={
 'analyst':dict(schema_version='loom.requirements/v1',summary='需求候选',items=[dict(id='one',text='加一')],uncertainties=[]),
 'architect':dict(schema_version='loom.design/v1',overview='设计候选',components=[dict(id='core',responsibility='计算')],steps=[],risks=[]),
 'tester':dict(schema_version='loom.public-checks/v1',visibility='development',cases=[dict(name='one',input={'value':1},expected={'answer':2})])}
KINDS={'analyst':'requirements','architect':'design','tester':'public-checks'}


@pytest.fixture
def node_case(tmp_path):
    def make(role,*,with_input=False,actor='subject:operator'):
        store=Store(tmp_path/'node.db');workflow=WorkflowService(store);ArtifactService(store)
        tests=(FrozenTest('private-host',{'value':1},{'answer':2}),)
        goal=workflow.create_goal('a','goal',title='统一节点测试',description='加一',acceptance=['tests.pass'],cost_limit=4000000,token_limit=8000000)
        run=goal['run_id'];definition=json.loads((ROOT/'packs/software-dev/workflow-v1.json').read_bytes())
        node=dict(id='work',title='执行节点',instruction='只处理批准需求',role=role,limits=dict(cost_limit_microusd=1000000,token_limit=2000000,max_attempts=1))
        if role in BODIES:node['role_output']=dict(kind=KINDS[role],schema_version=BODIES[role]['schema_version'])
        elif role=='developer':node['evaluation_digest']=freeze_tests(tests)[1]
        elif role=='validator':node['channel']='verification'
        definition['nodes']=[node]
        if with_input:
            node['input_artifacts']=[dict(source_node='supply',kind='requirements',schema_version='loom.requirements/v1',required=True)]
            definition['nodes'].insert(0,dict(id='supply',title='输入来源',instruction='产出需求',role='analyst',role_output=dict(kind='requirements',schema_version='loom.requirements/v1'),limits=node['limits']))
        saved=workflow.save_plan('a',run,'save',definition,0,actor=actor)
        workflow.approve_plan('a',run,'approve',saved['revision'],saved['digest'],actor=actor)
        path=tmp_path/'host.json';path.write_text(json.dumps({'tests':[dict(name=t.name,input=t.input,expected=t.expected) for t in tests]}),encoding='utf-8')
        args=SimpleNamespace(db=store.path,tenant='a',run_id=run,node_id='work',expected_plan_digest=saved['digest'],tests_file=path if role=='developer' else None,
            max_output_tokens=1024,execute=False,preview_digest=None,identities_file=None,secrets_file=None,sandbox_image=None,model_config=ROOT/'configs/models.example.json')
        return dict(store=store,workflow=workflow,run=run,args=args,saved=saved,tests=tests,body=BODIES.get(role,{'source':SOURCE}))
    return make


def gateway_for(body,requests):
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'id':'unified-node-public-fixture','object':'chat.completion','created':1792700000,'model':'deepseek-flash',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(body)}}],
            'usage':{'prompt_tokens':25,'completion_tokens':10,'total_tokens':35}})
    return ModelGateway([ModelRoute('deepseek-flash')],transport=httpx.MockTransport(handler),environ={'DEEPSEEK_API_KEY':'unified-node-public-fixture-only'})


def execute(b,body=None,*,sandbox=None):
    args=b['args'];preview=asyncio.run(script.run(args));args.execute=True;args.preview_digest=preview['preview_digest']
    args.identities_file=Path('not-read-identities');args.secrets_file=Path('not-read-secrets');args.sandbox_image='sha256:'+'1'*64
    requests=[];gateway=gateway_for(body or b['body'],requests)
    with patch.object(script,'load_identities',return_value=(OP,WORKER)),patch.object(script,'load_project_secrets',return_value={'DEEPSEEK_API_KEY':'unified-node-public-fixture-only'}),\
         patch.object(script,'load_model_config',return_value=([ModelRoute('deepseek-flash')],{})),patch.object(script,'ModelGateway',return_value=gateway),\
         patch.object(script,'DockerSandbox',return_value=sandbox or FakeSandbox()):
        result=asyncio.run(script.run(args))
    return result,requests


@pytest.mark.parametrize('role',['analyst','architect','tester','developer'])
def test_unified_node_executes_one_existing_approved_task(node_case,role):
    b=node_case(role);plan=b['workflow'].get_plan('a',b['run'])
    result,requests=execute(b)
    assert result['passed'] and result['execution']['status']=='submitted' and result['role']==role
    assert len(requests)==1 and not result['creates_new_goal'] and not result['verified']
    assert b['workflow'].get_plan('a',b['run'])==plan
    with patch.object(script,'load_identities',side_effect=AssertionError('no private reread')),pytest.raises(Conflict,match='already started'):
        asyncio.run(script.run(b['args']))


@pytest.mark.parametrize('role',['analyst','architect','tester','developer'])
def test_preview_has_no_private_config_model_or_docker_access(node_case,role):
    b=node_case(role);before=b['store'].events('a',b['run'])
    with patch.object(script,'load_identities',side_effect=AssertionError('private')),patch.object(script,'ModelGateway',side_effect=AssertionError('network')),\
         patch.object(script,'DockerSandbox',side_effect=AssertionError('Docker')),patch.object(script,'Store',side_effect=AssertionError('write store')):
        p=asyncio.run(script.run(b['args']))
    assert p['role']==role and p['network_requests']==0
    assert b['store'].events('a',b['run'])==before


def test_validator_is_explicitly_pending_without_private_reads(node_case):
    b=node_case('validator')
    with patch.object(script,'load_identities',side_effect=AssertionError('private')),pytest.raises(Conflict,match='pending configuration'):
        asyncio.run(script.run(b['args']))


def test_structured_role_rejects_host_tests_and_stale_preview(node_case,tmp_path):
    b=node_case('analyst');b['args'].tests_file=tmp_path/'not-read'
    with pytest.raises(ValueError,match='NO_HOST_TESTS'):asyncio.run(script.run(b['args']))
    b['args'].tests_file=None;b['args'].execute=True;b['args'].preview_digest='0'*64
    with patch.object(script,'load_identities',side_effect=AssertionError('private')),pytest.raises(ValueError,match='CURRENT_PREVIEW'):
        asyncio.run(script.run(b['args']))


def test_auxiliary_data_is_reapproved_atomically_for_new_structured_epoch(node_case):
    b=node_case('architect',with_input=True);args=b['args'];args.node_id='supply'
    result,_=execute(b,BODIES['analyst']);artifact=ArtifactService(b['store']).get_artifact('a',b['run'],result['execution']['artifact_id'])
    args.node_id='work';args.execute=False
    with pytest.raises(Conflict,match='explicit operator'):asyncio.run(script.run(args))
    ArtifactService(b['store']).approve_input('a',b['run'],b['run']+':work',artifact['artifact_id'],'input-approve',actor='subject:operator',
        expected_plan_digest=b['saved']['digest'],expected_source_epoch=1,expected_target_epoch=0,expected_revision=1,expected_content_hash=artifact['content_hash'])
    result,requests=execute(b,BODIES['architect'])
    assert result['passed'] and BODIES['analyst']['summary'] in json.dumps(requests[0],ensure_ascii=False)
    with b['store'].connect() as db:
        assert [r[0] for r in db.execute('SELECT target_epoch FROM artifact_input_grants WHERE target_task_id=? ORDER BY rowid',(b['run']+':work',))]==[0,1]


@pytest.mark.parametrize('change',['pause','unknown','reservation','source-contract','hard-dependency','budget','legacy-approval'])
def test_new_role_admission_rejects_drift_without_claim(node_case,change):
    b=node_case('analyst',actor=None if change=='legacy-approval' else 'subject:operator')
    with b['store'].transaction() as db:
        if change=='pause':db.execute("UPDATE runs SET status='paused'")
        elif change=='source-contract':db.execute("UPDATE workflow_tasks SET contract='{}'")
        elif change=='hard-dependency':db.execute("UPDATE tasks SET depends='[\"missing\"]'")
        elif change=='budget':db.execute('UPDATE runs SET cost_limit=1')
        elif change=='unknown':
            db.execute("INSERT INTO effects(tenant,id,run_id,task_id,epoch,request_hash,state) VALUES ('a','unknown',?,?,0,?,'indeterminate')",(b['run'],b['run']+':work','0'*64))
        elif change=='reservation':
            db.execute("INSERT INTO reservations(tenant,id,run_id,task_id,epoch,cost,tokens,state) VALUES ('a','reserved',?,?,0,1,1,'reserved')",(b['run'],b['run']+':work'))
    before=b['store'].events('a',b['run'])
    with pytest.raises(Conflict):asyncio.run(script.run(b['args']))
    assert b['store'].events('a',b['run'])==before


def test_new_role_claim_race_has_one_winner(node_case):
    b=node_case('analyst');service=ApprovedExecutionService(b['store']);params=('a',b['run'],'work',b['saved']['digest'],None)
    preview=service.preview(*params,max_output_tokens=1024)
    def attempt(_):
        try:return service.claim(*params,preview['preview_digest'],operator=OP,worker=WORKER,max_output_tokens=1024)
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(r is not None for r in results)==1
