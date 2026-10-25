"""Real JetStream transport for atomic decision/grant audit. Provider is mocked."""
import asyncio
import os
from uuid import uuid4

import nats
import pytest

from loom.jetstream import JetStreamBridge
from loom.review_decisions import ReviewDecisionService
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff, generate, reviewer_claim


pytestmark = pytest.mark.skipif(not os.environ.get('LOOM_NATS_TEST_URL'), reason='live NATS URL required')


@pytest.mark.parametrize('no_findings', [True, False])
def test_decision_and_optional_grant_relay_once_without_completing_tasks(handoff, no_findings):
    b=handoff
    if no_findings:b['body'].update(verdict='no_findings', findings=[])
    result=asyncio.run(generate(b,reviewer_claim(b)))
    decisions=ReviewDecisionService(b['store']);params=('a',b['target'],result['artifact_id'])
    preview=decisions.preview(*params);before=b['store'].snapshot('a',b['target'])
    kwargs=dict(actor='subject:operator',expected_preview_digest=preview['preview_digest'],
        decision='await_independent_review' if no_findings else 'approve_fixer',reason='真实消息传输测试，非独立模型验收')
    if not no_findings:kwargs['target_task_id']=b['target']+':fix'
    record=decisions.decide(*params,'live-review-routing',**kwargs)
    assert b['store'].snapshot('a',b['target'])==before
    assert decisions.decide(*params,'live-review-routing',**kwargs)==record
    events=b['store'].events('a',b['target'])

    async def relay():
        stream='LOOM_DECISION_'+uuid4().hex[:16];prefix='loomdecision.'+uuid4().hex[:16]
        bridge=JetStreamBridge(b['store'],stream=stream,prefix=prefix)
        nc=await nats.connect(os.environ['LOOM_NATS_TEST_URL'],allow_reconnect=False,connect_timeout=3)
        js=nc.jetstream();seen=[]
        try:
            await bridge.provision(js)
            sub=await bridge.subscribe(js,tenant='a',run_id=b['target'],durable='review-routing',ack_wait=0.15)
            await bridge.publish_pending(js,limit=1000)
            def apply(db,event):seen.append(event['type'])
            for index, _ in enumerate(events):
                message=(await sub.fetch(1,timeout=3))[0]
                if index < len(events)-1:
                    assert (await bridge.consume_message(message,tenant='a',run_id=b['target'],durable='review-routing',apply=apply)).applied
                else:
                    class LostAck:
                        data, subject = message.data, message.subject
                        async def ack_sync(self):raise ConnectionError('simulated lost acknowledgement')
                    with pytest.raises(ConnectionError):
                        await bridge.consume_message(LostAck(),tenant='a',run_id=b['target'],durable='review-routing',apply=apply)
                    await asyncio.sleep(0.2)
                    redelivered=(await sub.fetch(1,timeout=3))[0]
                    assert (await bridge.consume_message(redelivered,tenant='a',run_id=b['target'],durable='review-routing',apply=apply)).duplicate
            assert seen.count('review.decision_recorded')==1
            assert seen.count('artifact.input_approved')==(0 if no_findings else 1)
            assert 'task.completed' not in seen and 'run.completed' not in seen
            info=await js.consumer_info(stream,'review-routing')
            assert info.num_pending==info.num_ack_pending==0
        finally:
            await js.delete_stream(stream);await nc.close()
    asyncio.run(relay())
    assert decisions.history(*params)['items'][0]['event_hash']==record['event_hash']
