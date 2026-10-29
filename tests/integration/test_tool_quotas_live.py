"""Real Docker tool quota and reply-loss recovery; model HTTP is simulated."""
import os
from dataclasses import replace
from unittest.mock import patch

import pytest

from loom.quotas import QuotaService,Limit
from loom.sandbox import DockerSandbox,SandboxConfig,SandboxUnavailable
from tests.test_approved_node import node_case
from tests.test_dispatch import dispatch_case,enqueue,drain
from tests.test_quotas import POLICY,configure

pytestmark=pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')


@pytest.mark.parametrize('max_calls',[0,1])
def test_tool_quota_allows_or_rejects_real_container_candidate(dispatch_case,max_calls):
    b=dispatch_case('developer');configure(b['store'],replace(POLICY,tool=Limit(max_calls,1)));enqueue(b)
    b['body']={'source':'import json\nprint(json.dumps({"answer":2}))\n'}
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    with patch.object(sandbox,'run_python',wraps=sandbox.run_python) as observed:
        result,requests=drain(b,sandbox=sandbox)
    assert observed.call_count==max_calls and len(requests)==1
    assert result['state']==('submitted' if max_calls else 'awaiting_human')
    assert result['result']['model_calls']==result['result']['settled_calls']==1
    assert result['result']['reserved_cost']==0
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=1+max_calls,in_flight=0)


def test_lost_reply_after_real_docker_retains_tool_slot_and_restart_does_not_repeat(dispatch_case):
    b=dispatch_case('developer');configure(b['store']);enqueue(b)
    b['body']={'source':'import json\nprint(json.dumps({"answer":2}))\n'}
    class LostReply(DockerSandbox):
        executions=0
        def run_python(self,*args,**kwargs):
            result=super().run_python(*args,**kwargs);assert result.passed
            self.executions+=1
            raise SandboxUnavailable('synthetic reply loss after actual container execution')
    sandbox=LostReply(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    result,requests=drain(b,sandbox=sandbox)
    assert result['state']=='indeterminate' and len(requests)==sandbox.executions==1
    again,new_requests=drain(b,sandbox=sandbox)
    assert again is None and not new_requests and sandbox.executions==1
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=2,in_flight=1)
