"""Same-Goal review binding with real Docker/NATS and simulated providers."""
import asyncio
import json
import os
from uuid import uuid4

import nats
import pytest

from loom.jetstream import JetStreamBridge
from loom.review_inputs import ReviewInputService
from loom.sandbox import DockerSandbox,SandboxConfig
from tests.test_approved_node import node_case
from tests.test_validator_execution import validation_case,execute_validator,validator_review_case
from tests.test_review_sources import generate_review,request_review_quality
from tests.test_verification_jobs import run_job


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
@pytest.mark.parametrize('disputed',[False,True])
def test_review_of_real_program_controls_same_goal_validator(validation_case,tmp_path,disputed):
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    b=validation_case(source_sandbox=sandbox,with_reviewer=True)
    result,calls=generate_review(b);assert result['passed'] and len(calls)==1
    current=request_review_quality(b,tmp_path/'real-program-quality');quality,requests=asyncio.run(run_job(current,negative=disputed))
    assert len(requests)==2 and quality['task_verified'] is not disputed
    if disputed:
        assert b['store'].snapshot('a',b['run'])['status']=='awaiting_human'
        assert next(t for t in b['store'].snapshot('a',b['run'])['tasks'] if t['id']==b['validator_task'])['epoch']==0
    else:
        assert execute_validator(b,sandbox)[0]['passed']
        final=validator_review_case(b,tmp_path/'final-quality');verified,_=asyncio.run(run_job(final))
        assert verified['goal_accepted'] and b['store'].snapshot('a',b['run'])['status']=='succeeded'
    view=ReviewInputService(b['store']).preview('a',b['run'],b['run']+':review',include_source=True)
    context=json.loads(view['files']['review-context.json'])
    assert context['execution_summary']['cases_executed']==1 and context['subject']['task_id']==b['source_task']
    assert view['execution_summary']['cases_executed']==0


@pytest.mark.skipif(not os.environ.get('LOOM_NATS_TEST_URL'),reason='live NATS URL required')
def test_frozen_review_subject_and_signed_source_proof_relay_in_jetstream(validation_case):
    b=validation_case(with_reviewer=True);generate_review(b)
    events=b['store'].events('a',b['run'])
    async def relay():
        stream='LOOM_SOURCE_'+uuid4().hex[:16];prefix='loomsource.'+uuid4().hex[:16]
        bridge=JetStreamBridge(b['store'],stream=stream,prefix=prefix)
        nc=await nats.connect(os.environ['LOOM_NATS_TEST_URL'],allow_reconnect=False,connect_timeout=3)
        js=nc.jetstream();seen=[]
        try:
            await bridge.provision(js)
            sub=await bridge.subscribe(js,tenant='a',run_id=b['run'],durable='review-source')
            await bridge.publish_pending(js,limit=1000)
            def apply(db,event):seen.append(event)
            for _ in events:
                message=(await sub.fetch(1,timeout=3))[0]
                assert (await bridge.consume_message(message,tenant='a',run_id=b['run'],durable='review-source',apply=apply)).applied
            bound=[e['payload'] for e in seen if e['type']=='execution.context_bound' and e['payload']['task_id']==b['run']+':review']
            assert len(bound)==1 and bound[0]['review_source_binding']['source']['source_node']=='work'
            assert bound[0]['review_source_binding']['subject']==bound[0]['review_subject']
            assert bound[0]['review_source_binding']['original_requirements']['description']=='加一'
            assert bound[0]['review_source_binding']['verification_job_id']==b['job']
            info=await js.consumer_info(stream,'review-source');assert info.num_pending==info.num_ack_pending==0
        finally:
            await js.delete_stream(stream);await nc.close()
    asyncio.run(relay())
