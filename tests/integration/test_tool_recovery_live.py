"""Actual Docker lost reply and manual tool resolution; model HTTP is simulated."""
import os

import pytest

from loom.quotas import QuotaService
from loom.sandbox import DockerSandbox,SandboxConfig,SandboxUnavailable
from loom.tool_recovery import ToolRecoveryService
from tests.test_approved_execution import OP
from tests.test_approved_node import node_case
from tests.test_dispatch import dispatch_case,enqueue,drain
from tests.test_quotas import configure

pytestmark=pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')

def test_known_process_exit_resolution_after_real_container_reply_loss(dispatch_case):
    b=dispatch_case('developer');configure(b['store']);enqueue(b)
    b['body']={'source':'import json\nprint(json.dumps({"answer":2}))\n'}
    class LostReply(DockerSandbox):
        calls=0
        def run_python(self,*args,**kwargs):
            result=super().run_python(*args,**kwargs);assert result.passed
            self.calls+=1
            raise SandboxUnavailable('synthetic response loss after real Docker process exit')
    sandbox=LostReply(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    result,requests=drain(b,sandbox=sandbox)
    assert result['state']=='indeterminate' and len(requests)==sandbox.calls==1
    with b['store'].connect() as db:
        identifier=next(iter(b['store']._tool_call_ids(db,'a')))
        effect=dict(db.execute('SELECT * FROM effects WHERE id=?',(identifier,)).fetchone())
    assert QuotaService(b['store']).read('a')['tenant_usage']['in_flight']==1
    resolved=ToolRecoveryService(b['store']).reconcile(OP,b['run'],identifier,'actual-docker-resolved',
        expected_epoch=effect['epoch'],expected_request_hash=effect['request_hash'],decision='completed',
        evidence=dict(reference='private-integration://docker-process-exited',summary='Real Docker exited before injected reply loss; no execution receipt promoted.'))
    assert resolved['state']=='reconciled' and not resolved['verified'] and not resolved['billing_changed']
    again,new_requests=drain(b,sandbox=sandbox)
    assert again is None and not new_requests and sandbox.calls==1
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=2,in_flight=0)
    task=b['store'].snapshot('a',b['run'])['tasks'][0]
    assert task['status']!='done' and not task.get('output')
