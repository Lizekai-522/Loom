import asyncio
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from loom.context_budget import check_context_budget
from loom.failures import classify_failure
from loom.model_gateway import GatewayError,ModelGateway,ModelRoute
from loom.sandbox import DockerSandbox,SandboxConfig
from loom.store import Conflict,digest
from loom.workflow import TaskLimits,WorkflowDefinition
from tests.test_approved_node import node_case,execute
from tests.test_execution import FakeSandbox,PACK


@pytest.mark.parametrize('value',[True,255,262145,1.2,'4096',-1])
def test_context_limit_is_a_strict_bounded_optional_approval_field(value):
    with pytest.raises(ValidationError):TaskLimits(cost_limit_microusd=1,token_limit=1,context_limit_bytes=value)


def test_old_contracts_keep_the_same_canonical_limits_and_digest():
    old=json.loads(PACK.read_bytes())
    parsed=WorkflowDefinition.model_validate(old).model_dump(mode='json')
    assert all('context_limit_bytes' not in node['limits'] for node in parsed['nodes'])
    old_again=deepcopy(parsed)
    for node in old_again['nodes']:node['limits']['context_limit_bytes']=None
    assert digest(WorkflowDefinition.model_validate(old_again).model_dump(mode='json'))==digest(parsed)


def test_exact_wire_bytes_include_multibyte_text_and_json_envelope():
    gateway=ModelGateway([ModelRoute('deepseek-flash')],environ={})
    try:
        messages=[dict(role='user',content='中文内容'*60)]
        raw=gateway.prepare_request('deepseek-flash',messages,128,json_mode=True)
        assert len(raw)>len(raw.decode('utf-8'))>256
        check_context_budget({'limits':{'context_limit_bytes':len(raw)}},raw)
        with pytest.raises(GatewayError,match='TASK_CONTEXT_LIMIT_EXCEEDED'):
            check_context_budget({'limits':{'context_limit_bytes':len(raw)-1}},raw)
        assert json.loads(raw)['messages']==messages
    finally:asyncio.run(gateway.aclose())


@pytest.mark.parametrize('role',['developer','analyst','architect','tester'])
def test_oversized_request_stops_before_effect_or_reservation(node_case,role):
    b=node_case(role,context_limit=256);sandbox=FakeSandbox()
    result,requests=execute(b,sandbox=sandbox)
    assert not result['passed'] and result['execution']['reason']=='TASK_CONTEXT_LIMIT_EXCEEDED'
    assert result['execution']['status']=='awaiting_human' and not requests and sandbox.execution_calls==0
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM effects').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==0
    assert b['store'].snapshot('a',b['run'])['reserved']==0


@pytest.mark.parametrize('role',['developer','analyst'])
def test_allowed_request_keeps_original_submission_boundary(node_case,role):
    b=node_case(role,context_limit=262144)
    result,requests=execute(b)
    assert result['passed'] and len(requests)==1 and not result['counts_as_success']
    assert result['execution']['status']=='submitted'


def test_approved_context_limit_cannot_be_raised_without_new_plan(node_case):
    b=node_case('developer',context_limit=4096)
    definition=deepcopy(b['saved']['definition']);definition['nodes'][0]['limits']['context_limit_bytes']=8192
    with pytest.raises(Conflict,match='immutable'):
        b['workflow'].save_plan('a',b['run'],'raise-limit',definition,b['saved']['revision'])
    assert b['workflow'].get_task_spec('a',b['run'],b['run']+':work')['limits']['context_limit_bytes']==4096
    failure=classify_failure('TASK_CONTEXT_LIMIT_EXCEEDED',stage='context')
    assert failure.family=='quota' and not failure.model_capability_error and not failure.auto_repair_allowed
