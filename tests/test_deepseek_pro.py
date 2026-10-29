"""Pro reviewer contract and prices; all provider HTTP is simulated."""
import asyncio
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from loom.api import create_app
from loom.artifacts import ArtifactService
from loom.model_gateway import ModelGateway,ModelRoute,GatewayError
from loom.model_pricing import FLASH,PRO,role_tariff
from loom.quotas import QuotaService
from loom.role_execution import RoleTaskRunner
from loom.store import Conflict,InvalidInput,canonical
from tests.test_approved_execution import OP,WORKER
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff,reviewer_claim
from tests.test_quotas import configure

@pytest.fixture
def pro_case(handoff):
    b=handoff;definition=deepcopy(b['definition'])
    next(role for role in definition['roles'] if role['id']=='reviewer')['model_alias']='deepseek-v4-pro'
    definition['nodes'][0]['limits']={**definition['nodes'][0]['limits'],'cost_limit_microusd':1500000}
    goal=b['workflow'].create_goal('a','pro-goal',title='Pro独立代码审查',description='仅审阅明确引用的代码，不执行工具',acceptance=['tests.pass'],cost_limit=3000000,token_limit=6000000)
    target=goal['run_id'];saved=b['workflow'].save_plan('a',target,'pro-save',definition,0,actor='subject:operator')
    b['workflow'].approve_plan('a',target,'pro-approve',saved['revision'],saved['digest'],actor='subject:operator')
    b.update(target=target,definition=definition,saved=saved)
    return b

async def generate_pro(b,claim,*,response_model='deepseek-v4-pro',timeout=False,pause=False,route_model='deepseek-v4-pro'):
    async def handler(request):
        b['requests'].append(json.loads(request.content))
        if pause:
            from loom.operator import OperatorService
            OperatorService(b['store']).pause('a',b['target'],'pause-pro','subject:operator','停止后不提交')
        if timeout:raise httpx.ReadTimeout('synthetic Pro timeout',request=request)
        return httpx.Response(200,json=dict(id='pro-test-response',object='chat.completion',created=1792700000,model=response_model,
            choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content=canonical(b['body'])))],
            usage=dict(prompt_tokens=25,completion_tokens=10,total_tokens=35)))
    async with ModelGateway([ModelRoute('deepseek-v4-pro',model=route_model)],transport=httpx.MockTransport(handler),environ={'DEEPSEEK_API_KEY':'pro-public-fixture-only'}) as gateway:
        return await RoleTaskRunner(b['store'],gateway).run_role(claim,max_output_tokens=1024)

def test_peak_tariffs_are_separate_and_round_up():
    assert FLASH.cost(1000000,1000000)==1500000
    assert PRO.cost(1000000,1000000)==5280000 and PRO.cost(25,10)==73
    assert PRO.cost(PRO.context_limit,1024)==1388176
    for value in (True,-1,1.5):
        with pytest.raises(InvalidInput):PRO.cost(value,1)

def test_preview_reserves_pro_price_and_execution_keeps_real_source_without_gate_v(pro_case):
    b=pro_case;configure(b['store'])
    preview=b['admission'].preview('a',b['target'],'review',b['saved']['digest'],None)
    assert preview['initial_cost_reservation_upper']==PRO.cost(PRO.context_limit,768)
    result=asyncio.run(generate_pro(b,reviewer_claim(b)))
    assert result['status']=='submitted' and result['usage_cost_upper_microusd']==73
    assert not result['verified'] and not result['counts_as_success'] and len(b['requests'])==1
    candidate=ArtifactService(b['store']).get_artifact('a',b['target'],result['artifact_id'])
    assert candidate['model_provenance']['model']=='deepseek-v4-pro' and not candidate['producer_verified']
    with TestClient(create_app(b['store'],{},identities=(OP,WORKER))) as client:
        review=client.get('/v1/runs/'+b['target']+'/review',headers={'Authorization':'Bearer '+OP.token}).json()
    source=review['tasks'][0]['artifact']['model_provenance']
    assert source['model']=='deepseek-v4-pro' and source['usage_cost_upper_microusd']==73
    quota=QuotaService(b['store']).read('a')
    assert quota['resources'][0]['resource']=='deepseek/deepseek-v4-pro' and quota['tenant_usage']['in_flight']==0
    assert b['store'].snapshot('a',b['target'])['tasks'][-1]['status']=='pending'

def test_old_flash_budget_does_not_admit_pro(pro_case):
    b=pro_case
    definition=deepcopy(b['definition']);definition['nodes'][0]['limits']['cost_limit_microusd']=1000000
    goal=b['workflow'].create_goal('a','low-pro-goal',title='Pro预算拒绝',description='不调用模型',acceptance=['tests.pass'],cost_limit=2500000,token_limit=6000000)
    saved=b['workflow'].save_plan('a',goal['run_id'],'low-pro-save',definition,0,actor='subject:operator')
    b['workflow'].approve_plan('a',goal['run_id'],'low-pro-approve',saved['revision'],saved['digest'],actor='subject:operator')
    with pytest.raises(Conflict,match='budget'):b['admission'].preview('a',goal['run_id'],'review',saved['digest'],None)
    # The old approved Flash reviewer remains untouched and cannot be rebound to Pro.
    tariff=role_tariff({'role':'reviewer','model_alias':'deepseek-flash'})
    assert tariff is FLASH and tariff.cost(tariff.context_limit,1024)<PRO.cost(PRO.context_limit,1024)

@pytest.mark.parametrize('response_model,timeout,reason',[
    ('deepseek-flash',False,'MODEL_IDENTITY_MISMATCH'),('deepseek-v4-pro',True,'MODEL_TIMEOUT')])
def test_wrong_response_or_timeout_keeps_unknown_reservation(pro_case,response_model,timeout,reason):
    b=pro_case;configure(b['store']);result=asyncio.run(generate_pro(b,reviewer_claim(b),response_model=response_model,timeout=timeout))
    assert result['reason']==reason and len(b['requests'])==1 and result['billing_uncertain']
    assert b['store'].snapshot('a',b['target'])['reserved']==PRO.cost(PRO.context_limit,1024)
    assert QuotaService(b['store']).read('a')['tenant_usage']['in_flight']==1

def test_pro_pause_saves_known_model_billing_but_no_candidate(pro_case):
    b=pro_case;result=asyncio.run(generate_pro(b,reviewer_claim(b),pause=True))
    assert result['reason']=='EXECUTION_RUN_PAUSED' and not result['billing_uncertain']
    snapshot=b['store'].snapshot('a',b['target'])
    assert snapshot['reserved']==0 and snapshot['spent']==73 and snapshot['status']=='paused'
    assert not ArtifactService(b['store']).list_artifacts('a',b['target'])

def test_route_alias_cannot_lower_pro_tariff_or_change_model(pro_case):
    b=pro_case
    with pytest.raises(InvalidInput):asyncio.run(generate_pro(b,reviewer_claim(b),route_model='deepseek-flash'))
    assert not b['requests']
    with pytest.raises(InvalidInput):role_tariff({'role':'analyst','model_alias':'deepseek-v4-pro'})
    with pytest.raises(InvalidInput):role_tariff({'role':'reviewer','model_alias':'cheap-alias'},ModelRoute('cheap-alias',model='deepseek-v4-pro'))
