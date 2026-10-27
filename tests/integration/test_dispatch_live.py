"""Actual Docker and JetStream for queued local execution. Model HTTP is mocked."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import nats
import pytest

from loom.dispatch import DispatchService
from loom.executor_config import LocalExecutorConfig
from loom.jetstream import JetStreamBridge
from loom.sandbox import DockerSandbox,SandboxConfig
from loom.review_decisions import ReviewDecisionService
from tests.test_dispatch import dispatch_case,enqueue,drain
from tests.test_approved_node import node_case,ROOT
from tests.test_approved_execution import OP,WORKER
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff,generate,reviewer_claim


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
@pytest.mark.parametrize('passes',[True,False])
def test_queued_developer_real_docker_execution_or_human_stop(dispatch_case,passes):
    b=dispatch_case('developer');enqueue(b)
    b['body']={'source':'import json\nprint(json.dumps({"answer":'+('2' if passes else '0')+'}))\n'}
    result,requests=drain(b,sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE'])))
    assert result['state']==('submitted' if passes else 'awaiting_human') and len(requests)==1
    assert result['result']['reserved_cost']==result['result']['reserved_tokens']==0
    assert not b['dispatch'].preview(*b['params'])['ready']


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
@pytest.mark.parametrize('passes',[True,False])
def test_queued_exact_review_fixer_reapproves_epoch_and_uses_original_host(handoff,tmp_path,passes):
    b=handoff;result=asyncio.run(generate(b,reviewer_claim(b)));decisions=ReviewDecisionService(b['store'])
    p=decisions.preview('a',b['target'],result['artifact_id'])
    decisions.decide('a',b['target'],result['artifact_id'],'dispatch-fix-approve',actor='subject:operator',expected_preview_digest=p['preview_digest'],decision='approve_fixer',reason='核对意见后只修复一次',target_task_id=b['target']+':fix')
    host=tmp_path/'frozen-host.json';host.write_text(json.dumps({'tests':[dict(name=t.name,input=t.input,expected=t.expected) for t in b['suite']]}),encoding='utf-8')
    config=LocalExecutorConfig('test-local',tmp_path/'private-identities',tmp_path/'private-model-key',ROOT/'configs/models.example.json',os.environ['LOOM_SANDBOX_TEST_IMAGE'],{b['body']['subject']['evaluation_digest']:host})
    b.update(config=config,dispatch=DispatchService(b['store'],config=config,identities=(OP,WORKER)),params=('a',b['target'],b['target']+':fix'),body={'source':'import json\nprint(json.dumps({"answer":'+('2' if passes else '0')+'}))\n'})
    enqueue(b);result,requests=drain(b,sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE'])))
    assert result['state']==('submitted' if passes else 'awaiting_human') and len(requests)==1
    assert result['result']['reserved_cost']==0
    if not passes:assert b['store'].snapshot('a',b['target'])['status']=='awaiting_human'


@pytest.mark.skipif(not os.environ.get('LOOM_NATS_TEST_URL'),reason='real JetStream required')
def test_request_audit_survives_real_confirmation_loss_and_redelivery(dispatch_case):
    b=dispatch_case();request=enqueue(b);events=b['store'].events('a',b['run'])
    async def relay():
        stream='LOOM_DISPATCH_'+uuid4().hex[:16];prefix='dispatchtest.'+uuid4().hex[:16]
        bridge=JetStreamBridge(b['store'],stream=stream,prefix=prefix);nc=await nats.connect(os.environ['LOOM_NATS_TEST_URL'],allow_reconnect=False,connect_timeout=3);js=nc.jetstream();seen=[]
        try:
            await bridge.provision(js);sub=await bridge.subscribe(js,tenant='a',run_id=b['run'],durable='dispatch-worker',ack_wait=0.15)
            await bridge.publish_pending(js,limit=1000)
            def apply(db,event):seen.append(event['type'])
            for index,_ in enumerate(events):
                msg=(await sub.fetch(1,timeout=3))[0]
                if index<len(events)-1:await bridge.consume_message(msg,tenant='a',run_id=b['run'],durable='dispatch-worker',apply=apply)
                else:
                    class LostAck:
                        data,subject=msg.data,msg.subject
                        async def ack_sync(self):raise ConnectionError('simulated lost confirmation')
                    with pytest.raises(ConnectionError):await bridge.consume_message(LostAck(),tenant='a',run_id=b['run'],durable='dispatch-worker',apply=apply)
                    await asyncio.sleep(.2);again=(await sub.fetch(1,timeout=3))[0]
                    assert (await bridge.consume_message(again,tenant='a',run_id=b['run'],durable='dispatch-worker',apply=apply)).duplicate
            assert seen.count('execution.requested')==1
        finally:await js.delete_stream(stream);await nc.close()
    asyncio.run(relay());assert b['dispatch'].history(*b['params'])['items'][0]['request_id']==request['request_id']
