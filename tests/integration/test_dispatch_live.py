"""Actual Docker and JetStream for queued local execution. Model HTTP is mocked."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import nats
import pytest

from loom.dispatch import DispatchService
from loom.attempts import attempt_summaries
from loom.executor_config import LocalExecutorConfig
from loom.jetstream import JetStreamBridge
from loom.operator import OperatorService
from loom.store import Conflict, canonical
from loom.sandbox import DockerSandbox,SandboxConfig
from loom.review_decisions import ReviewDecisionService
from loom.quotas import QuotaService,QuotaPolicy,Limit
from tests.test_dispatch import dispatch_case,enqueue,drain,worker_for
from tests.test_approved_node import node_case,ROOT,gateway_for
from loom.model_gateway import ModelRoute
from tests.test_approved_execution import OP,WORKER
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff,generate,reviewer_claim


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
@pytest.mark.parametrize('max_calls',[0,1])
def test_durable_model_quota_in_actual_worker_and_docker(dispatch_case,max_calls):
    b=dispatch_case('developer')
    policy=QuotaPolicy(60,Limit(20,2),Limit(max_calls,1),Limit(20,1))
    QuotaService(b['store']).configure(OP,policy,key='live-quota',expected_revision=0,reason='真实容器配额正反例')
    enqueue(b);b['body']={'source':'import json\nprint(json.dumps({"answer":2}))\n'}
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    result,requests=drain(b,sandbox=sandbox)
    assert len(requests)==max_calls
    assert result['state']==('submitted' if max_calls else 'awaiting_human')
    assert QuotaService(b['store']).read('a')['tenant_usage']==dict(calls=max_calls*2,in_flight=0)
    assert result['result']['model_calls']==max_calls and result['result']['settled_calls']==max_calls
    assert b['store'].snapshot('a',b['run'])['reserved']==0


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
def test_executor_heartbeat_continues_during_real_docker_without_repeat(dispatch_case):
    b=dispatch_case('developer');enqueue(b)
    b['body']={'source':'import time,json\ntime.sleep(.2)\nprint(json.dumps({"answer":2}))\n'}
    executor=worker_for(b);requests=[]
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    gateway=gateway_for(b['body'],requests)
    async def work():
        async with executor.status.monitor(interval=.05):
            path=executor.status.directory/(executor.status._scope('a')+'.json')
            before=json.loads(path.read_bytes())['observed_at']
            result=await executor.drain_one()
            assert result['state']=='submitted' and result['result']['reserved_cost']==0
            assert executor.status.read('a')['state']=='recent'
            assert json.loads(path.read_bytes())['observed_at']>before
            assert await executor.drain_one() is None
    with patch('loom.local_executor.load_project_secrets',return_value={'DEEPSEEK_API_KEY':'heartbeat-model-mock-only'}),\
         patch('loom.local_executor.load_model_config',return_value=([ModelRoute('deepseek-flash')],{})),\
         patch('loom.local_executor.ModelGateway',return_value=gateway),patch('loom.local_executor.DockerSandbox',return_value=sandbox):
        asyncio.run(work())
    assert len(requests)==1


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
@pytest.mark.parametrize('passes',[True,False])
def test_queued_developer_real_docker_execution_or_human_stop(dispatch_case,passes):
    b=dispatch_case('developer');enqueue(b)
    b['body']={'source':'import json\nprint(json.dumps({"answer":'+('2' if passes else '0')+'}))\n'}
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    observed=[];run_python=sandbox.run_python
    def observe(*args,**kwargs):
        result=run_python(*args,**kwargs)
        observed.extend((case.status,case.duration_seconds) for case in result.cases)
        return result
    sandbox.run_python=observe
    result,requests=drain(b,sandbox=sandbox)
    assert result['state']==('submitted' if passes else 'awaiting_human') and len(requests)==1, observed
    evaluation=next(e['payload'] for e in b['store'].events('a',b['run']) if e['type']=='execution.evaluated')
    assert evaluation['host_cases_total']==len(observed)
    assert evaluation['host_cases_passed']==sum(status=='passed' for status,_ in observed)
    summary=next(e for e in attempt_summaries(b['store'],'a',b['run']) if e['event_type']=='execution.evaluated')
    assert summary['host_cases_total']==len(observed) and 'cases' not in summary
    assert result['result']['reserved_cost']==result['result']['reserved_tokens']==0
    assert not b['dispatch'].preview(*b['params'])['ready']


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
def test_real_docker_timeout_has_bounded_safe_diagnostic_summary(dispatch_case):
    b=dispatch_case('developer');enqueue(b)
    b['body']={'source':'while True:\n    pass\n'}
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE'],timeout_seconds=2))
    result,requests=drain(b,sandbox=sandbox)
    assert result['state']=='awaiting_human' and len(requests)==1
    summary=next(e for e in attempt_summaries(b['store'],'a',b['run']) if e['event_type']=='execution.evaluated')
    assert summary['host_cases_total']==1 and summary['host_cases_timeout']==1
    assert summary['host_cases_passed']==0 and summary['hard_criteria_passed'] is False
    assert 'private-host' not in json.dumps(summary) and 'stdout' not in summary and 'stderr' not in summary


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


@pytest.mark.skipif(not os.environ.get('LOOM_NATS_TEST_URL'),reason='real JetStream required')
def test_resume_preview_and_recovery_event_reach_real_jetstream(dispatch_case):
    b=dispatch_case();ops=OperatorService(b['store'])
    ops.pause('a',b['run'],'preview-pause','subject:operator','暂停后核对恢复条件')
    before=b['store'].events('a',b['run'])
    preview=ops.resume_preview('a',b['run'])
    assert preview['ready'] and preview['counts_as_success'] is False
    assert b['store'].events('a',b['run'])==before
    ops.resume('a',b['run'],'preview-resume','subject:operator','显式恢复')
    events=b['store'].events('a',b['run'])
    async def relay():
        stream='LOOM_RESUME_'+uuid4().hex[:16];prefix='resumetest.'+uuid4().hex[:16]
        bridge=JetStreamBridge(b['store'],stream=stream,prefix=prefix)
        nc=await nats.connect(os.environ['LOOM_NATS_TEST_URL'],allow_reconnect=False,connect_timeout=3)
        js=nc.jetstream();seen=[]
        try:
            await bridge.provision(js)
            sub=await bridge.subscribe(js,tenant='a',run_id=b['run'],durable='resume-worker',ack_wait=1)
            await bridge.publish_pending(js,limit=1000)
            for _ in events:
                msg=(await sub.fetch(1,timeout=3))[0]
                await bridge.consume_message(msg,tenant='a',run_id=b['run'],durable='resume-worker',apply=lambda db,event:seen.append(event['type']))
            assert seen.count('run.resumed')==1
            assert seen.count('run.paused')==1
        finally:
            await js.delete_stream(stream)
            await nc.close()
    asyncio.run(relay())


@pytest.mark.skipif(not os.environ.get('LOOM_NATS_TEST_URL'),reason='real JetStream required')
def test_reconciliation_preserves_partial_evidence_and_publishes_only_valid_decision(dispatch_case):
    b=dispatch_case();store=b['store'];ops=OperatorService(store)
    claim=store.claim_next('a',b['run'],'fixture-worker',lease_seconds=300)
    reservation=f'python:{claim.task_id}:{claim.epoch}'
    store.reserve(claim,reservation,max_cost=100,max_tokens=100)
    call=store.begin_effect(claim,f'model-generation:{claim.epoch}',{'fixture':'no live model request'})['call_id']
    fragment=canonical({'fragment':'synthetic'})
    with store.transaction() as db:
        db.execute("UPDATE effects SET state='partial',result=? WHERE tenant='a' AND id=?",(fragment,call))
        db.execute("UPDATE tasks SET status='awaiting_human',worker=NULL,lease_until=NULL WHERE tenant='a' AND id=?",(claim.task_id,))
        db.execute("UPDATE runs SET status='awaiting_human' WHERE tenant='a' AND id=?",(b['run'],))
        store._event(db,'a',b['run'],'tool.indeterminate',{'call_id':call,'state':'partial'},'fixture')
        store._event(db,'a',b['run'],'execution.blocked',{'task_id':claim.task_id,'run_status':'awaiting_human'},'fixture')
    evidence={'reference':'fixture:ledger','summary':'synthetic evidence'}
    before=store.events('a',b['run'])
    with pytest.raises(Conflict):
        ops.reconcile_effect('a',b['run'],call,'deny-partial','subject:operator','not_sent',evidence,0,0)
    assert store.events('a',b['run'])==before
    result=ops.reconcile_effect('a',b['run'],call,'verify-usage','subject:operator','completed',evidence,10,20)
    assert result['state']=='reconciled' and result['counts_as_success'] is False
    with store.connect() as db:
        assert db.execute("SELECT result FROM effects WHERE tenant='a' AND id=?",(call,)).fetchone()[0]==fragment
        assert db.execute("SELECT state FROM reservations WHERE tenant='a' AND id=?",(reservation,)).fetchone()[0]=='settled'
    events=store.events('a',b['run'])
    async def relay():
        stream='LOOM_RECON_'+uuid4().hex[:16];prefix='recontest.'+uuid4().hex[:16]
        bridge=JetStreamBridge(store,stream=stream,prefix=prefix)
        nc=await nats.connect(os.environ['LOOM_NATS_TEST_URL'],allow_reconnect=False,connect_timeout=3)
        js=nc.jetstream();seen=[]
        try:
            await bridge.provision(js)
            sub=await bridge.subscribe(js,tenant='a',run_id=b['run'],durable='reconcile-worker',ack_wait=1)
            await bridge.publish_pending(js,limit=1000)
            for _ in events:
                msg=(await sub.fetch(1,timeout=3))[0]
                await bridge.consume_message(msg,tenant='a',run_id=b['run'],durable='reconcile-worker',apply=lambda db,event:seen.append(event['type']))
            assert seen.count('effect.reconciled')==1
        finally:
            await js.delete_stream(stream)
            await nc.close()
    asyncio.run(relay())


@pytest.mark.skipif(not os.environ.get('LOOM_NATS_TEST_URL'),reason='real JetStream required')
def test_stranded_completed_call_accounting_keeps_exhausted_attempt_limit(dispatch_case):
    b=dispatch_case();store=b['store'];ops=OperatorService(store)
    claim=store.claim_next('a',b['run'],'fixture-worker',lease_seconds=300)
    reservation=f'python:{claim.task_id}:{claim.epoch}'
    store.reserve(claim,reservation,max_cost=100,max_tokens=100)
    call=store.begin_effect(claim,f'model-generation:{claim.epoch}',{'fixture':'no live model request'})['call_id']
    store.finish_effect(claim,call,{'fixture':'completed response'})
    with store.transaction() as db:
        db.execute("UPDATE tasks SET lease_until=-1 WHERE tenant='a' AND id=?",(claim.task_id,))
    assert ops.list_interventions('a')[0]['stranded_reservations']==1
    assert store.claim_next('a',b['run'],'must-not-automatically-retry') is None
    evidence={'reference':'fixture:ledger','summary':'synthetic evidence'}
    resolved=ops.reconcile_effect('a',b['run'],call,'manual-account','subject:operator','completed',evidence,10,20)
    assert resolved['state']=='done' and resolved['counts_as_success'] is False
    assert store.snapshot('a',b['run'])['status']=='awaiting_human'
    assert ops.resume_preview('a',b['run'])['reason']=='ATTEMPTS_EXHAUSTED'
    with pytest.raises(Conflict):
        ops.resume('a',b['run'],'explicit-resume','subject:operator','人工核对后仍不能绕过次数限制')
    events=store.events('a',b['run'])
    async def relay():
        stream='LOOM_STRAND_'+uuid4().hex[:16];prefix='strandedtest.'+uuid4().hex[:16]
        bridge=JetStreamBridge(store,stream=stream,prefix=prefix)
        nc=await nats.connect(os.environ['LOOM_NATS_TEST_URL'],allow_reconnect=False,connect_timeout=3)
        js=nc.jetstream();seen=[]
        try:
            await bridge.provision(js)
            sub=await bridge.subscribe(js,tenant='a',run_id=b['run'],durable='stranded-worker',ack_wait=1)
            await bridge.publish_pending(js,limit=1000)
            for _ in events:
                msg=(await sub.fetch(1,timeout=3))[0]
                await bridge.consume_message(msg,tenant='a',run_id=b['run'],durable='stranded-worker',apply=lambda db,event:seen.append(event['type']))
            assert seen.count('effect.reconciled')==1 and seen.count('run.resumed')==0
        finally:
            await js.delete_stream(stream)
            await nc.close()
    asyncio.run(relay())
