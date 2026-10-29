from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest

from loom.execution import LocalTaskRunner
from loom.operator import OperatorService
from loom.quotas import QuotaService,QuotaExceeded,Limit
from loom.store import Store
from tests.test_quotas import POLICY,configure,claim_for,admit,sizes
from tests.test_approved_node import node_case,execute
from tests.test_dispatch import dispatch_case,enqueue,drain
from tests.test_execution import FakeSandbox


def tool(store,claim,phase='acceptance'):
    with store.transaction() as db:
        return QuotaService(store).admit_tool_in(db,claim,'tests:'+phase,dict(tool_id='tests.run',phase=phase))


def test_tool_restart_replay_and_completion_do_not_reset_admissions(tmp_path):
    store=Store(tmp_path/'tools.db');configure(store);claim=claim_for(store);first=tool(store,claim)
    with pytest.raises(QuotaExceeded,match='QUOTA_TOOL_REPLAY_REQUIRES_REVIEW'):tool(Store(store.path),claim)
    assert QuotaService(store).read('a')['tenant_usage']==dict(calls=1,in_flight=1)
    store.finish_effect(claim,first['call_id'],{'known_tool_receipt':True})
    with pytest.raises(QuotaExceeded,match='QUOTA_TOOL_REPLAY_REQUIRES_REVIEW'):tool(store,claim)
    assert QuotaService(store).read('a')['tenant_usage']==dict(calls=1,in_flight=0)
    assert sizes(store)==(1,0,1)


def test_model_and_tool_have_separate_resource_limits_and_shared_tenant_usage(tmp_path):
    store=Store(tmp_path/'tools.db');configure(store);first=claim_for(store)
    model=admit(store,first);store.finish_effect(first,model['call_id'],{'known_model_response':True})
    tool(store,first)
    second=claim_for(store);assert admit(store,second)['dispatch']
    with pytest.raises(QuotaExceeded,match='QUOTA_TOOL_IN_FLIGHT_LIMIT'):tool(store,first,'development')
    result=QuotaService(store).read('a')
    assert result['tenant_usage']==dict(calls=3,in_flight=2)
    assert {item['kind']:item['in_flight'] for item in result['resources']}==dict(model=1,tool=1)


def test_tool_quota_concurrency_has_no_partial_intent(tmp_path):
    store=Store(tmp_path/'tools.db');configure(store);claims=[claim_for(store),claim_for(store)]
    def call(claim):
        try:return tool(Store(store.path),claim)
        except QuotaExceeded as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(call,claims))
    assert sum(isinstance(value,dict) for value in results)==1
    assert 'QUOTA_TOOL_IN_FLIGHT_LIMIT' in results and sizes(store)==(1,0,1)


def test_tool_rejection_preserves_known_model_usage_without_running_candidate(node_case):
    b=node_case('developer');configure(b['store'],replace(POLICY,tool=Limit(0,1)));sandbox=FakeSandbox()
    result,requests=execute(b,sandbox=sandbox)
    assert result['execution']['reason']=='QUOTA_TOOL_RATE_LIMIT' and len(requests)==1
    assert sandbox.execution_calls==0 and sizes(b['store'])==(1,1,1)
    assert b['store'].snapshot('a',b['run'])['reserved']==0
    assert not result['execution']['billing_uncertain']


def test_unknown_tool_is_retained_without_claiming_unknown_model_billing(dispatch_case):
    b=dispatch_case('developer');configure(b['store']);enqueue(b)
    result,requests=drain(b,sandbox=FakeSandbox(execute_error=True))
    assert result['state']=='indeterminate' and len(requests)==1
    assert result['result']['model_calls']==1 and result['result']['settled_calls']==1
    assert result['result']['reserved_cost']==0 and sizes(b['store'])==(2,1,2)
    usage=QuotaService(b['store']).read('a')
    assert next(item for item in usage['resources'] if item['kind']=='tool')['in_flight']==1
    blocked=next(event for event in b['store'].events('a',b['run']) if event['type']=='execution.blocked')
    assert not blocked['payload']['billing_uncertain']
    with b['store'].connect() as db:now=b['store'].now(db)
    with patch.object(b['store'],'now',return_value=now+61):
        assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=0,in_flight=1)


def test_successful_worker_counts_one_model_and_one_tool_separately(dispatch_case):
    b=dispatch_case('developer');configure(b['store']);enqueue(b)
    result,requests=drain(b)
    assert result['state']=='submitted' and len(requests)==1
    assert result['result']['model_calls']==result['result']['settled_calls']==1
    quota=QuotaService(b['store']).read('a')
    assert quota['tenant_usage']==dict(calls=2,in_flight=0) and quota['tool_enforcement_connected']
    assert {item['kind']:item['calls'] for item in quota['resources']}==dict(model=1,tool=1)


def test_operator_pause_after_tool_result_keeps_receipt_and_stops_submission(node_case):
    b=node_case('developer');configure(b['store']);sandbox=FakeSandbox();original=sandbox.run_python
    def pause(files,tests):
        result=original(files,tests)
        OperatorService(b['store']).pause('a',b['run'],'pause-tool','subject:operator','暂停后不得提交')
        return result
    with patch.object(sandbox,'run_python',side_effect=pause):result,requests=execute(b,sandbox=sandbox)
    assert result['execution']['reason']=='EXECUTION_RUN_PAUSED' and len(requests)==1
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=2,in_flight=0)
    assert b['store'].snapshot('a',b['run'])['status']=='paused'
