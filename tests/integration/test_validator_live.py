"""Actual Docker/NATS; model responses mocked, Ed25519 and Gate-V real."""
import asyncio
import os
from uuid import uuid4

import nats
import pytest

from loom.jetstream import JetStreamBridge
from loom.quotas import QuotaService
from loom.review_inputs import ReviewInputService
from loom.sandbox import DockerSandbox,SandboxConfig
from tests.test_approved_node import node_case
from tests.test_validator_execution import validation_case,configure_dispatch,drain_validator,validator_review_case
from tests.test_quotas import configure
from tests.test_verification_jobs import run_job


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
@pytest.mark.parametrize('negative',[False,True])
def test_accepted_program_is_reexecuted_in_real_docker_then_gets_new_signatures(validation_case,tmp_path,negative):
    image=os.environ['LOOM_SANDBOX_TEST_IMAGE']
    b=validation_case(source_sandbox=DockerSandbox(SandboxConfig(image)))
    configure(b['store']);config,dispatch=configure_dispatch(b,tmp_path)
    from dataclasses import replace
    config=replace(config,sandbox_image=image)
    from loom.dispatch import DispatchService
    from tests.test_approved_execution import OP,WORKER
    dispatch=DispatchService(b['store'],config=config,identities=(OP,WORKER))
    preview=dispatch.preview('a',b['run'],b['validator_task'])
    dispatch.enqueue('a',b['run'],b['validator_task'],'real-validator',actor='subject:operator',
        expected_dispatch_digest=preview['dispatch_digest'],reason='真实容器复验，无生成模型；之后进行新双签')
    result=drain_validator(b,config,DockerSandbox(SandboxConfig(image)))
    assert result['state']=='submitted' and result['result']['model_calls']==0
    view=ReviewInputService(b['store']).preview('a',b['run'],b['validator_task'],include_source=True)
    source=ReviewInputService(b['store']).preview('a',b['run'],b['source_task'],include_source=True)
    assert view['files']==source['files'] and view['execution_summary']['cases_executed']==1
    assert view['execution_summary']['hard_criteria_passed']
    assert view['subject']['evaluation_event_hash']!=source['subject']['evaluation_event_hash']
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=1,in_flight=0)
    current=validator_review_case(b,tmp_path/'new-verification')
    verified,calls=asyncio.run(run_job(current,negative=negative))
    assert len(calls)==2 and verified['state']==('disputed' if negative else 'verified')
    assert b['store'].snapshot('a',b['run'])['status']==('awaiting_human' if negative else 'succeeded')
    assert b['store'].verify_events(b['store'].events('a',b['run']))


@pytest.mark.skipif(not os.environ.get('LOOM_NATS_TEST_URL'),reason='live NATS URL required')
def test_validator_completion_events_relay_once_through_actual_jetstream(validation_case,tmp_path):
    from tests.test_validator_execution import execute_validator
    b=validation_case();execute_validator(b)
    current=validator_review_case(b,tmp_path/'transport-verification');asyncio.run(run_job(current))
    events=b['store'].events('a',b['run'])
    async def relay():
        stream='LOOM_VALIDATOR_'+uuid4().hex[:16];prefix='loomvalidator.'+uuid4().hex[:16]
        bridge=JetStreamBridge(b['store'],stream=stream,prefix=prefix)
        nc=await nats.connect(os.environ['LOOM_NATS_TEST_URL'],allow_reconnect=False,connect_timeout=3)
        js=nc.jetstream();seen=[]
        try:
            await bridge.provision(js)
            sub=await bridge.subscribe(js,tenant='a',run_id=b['run'],durable='validator-events',ack_wait=0.15)
            await bridge.publish_pending(js,limit=1000)
            def apply(db,event):seen.append(event['type'])
            for index,_ in enumerate(events):
                message=(await sub.fetch(1,timeout=3))[0]
                if index<len(events)-1:
                    assert (await bridge.consume_message(message,tenant='a',run_id=b['run'],durable='validator-events',apply=apply)).applied
                else:
                    class LostAck:
                        data,subject=message.data,message.subject
                        async def ack_sync(self):raise ConnectionError('synthetic lost acknowledgement')
                    with pytest.raises(ConnectionError):await bridge.consume_message(LostAck(),tenant='a',run_id=b['run'],durable='validator-events',apply=apply)
                    await asyncio.sleep(.2)
                    duplicate=(await sub.fetch(1,timeout=3))[0]
                    assert (await bridge.consume_message(duplicate,tenant='a',run_id=b['run'],durable='validator-events',apply=apply)).duplicate
            assert seen.count('task.completed')==2 and seen.count('run.completed')==1
            assert seen.count('verification.finished')==2
            info=await js.consumer_info(stream,'validator-events');assert info.num_pending==info.num_ack_pending==0
        finally:
            await js.delete_stream(stream);await nc.close()
    asyncio.run(relay())
