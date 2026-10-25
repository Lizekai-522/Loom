"""Routing is a bound operator record; never independent acceptance."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import sqlite3
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.review_decisions import ReviewDecisionService
from loom.store import Conflict, InvalidInput, NotFound
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff, generate, reviewer_claim
from tests.test_approved_execution import OP, WORKER


@pytest.fixture
def decision_case(handoff):
    b = handoff
    def produce(*, no_findings=False):
        if no_findings: b['body'].update(verdict='no_findings', findings=[])
        result = asyncio.run(generate(b, reviewer_claim(b)))
        assert result['status']=='submitted'
        b['artifact_id']=result['artifact_id']
        b['decisions']=ReviewDecisionService(b['store'])
        b['params']=('a',b['target'],b['artifact_id'])
        return b
    return produce


def decide(b, decision, key='decision', **kwargs):
    preview=b['decisions'].preview(*b['params'])
    return b['decisions'].decide(*b['params'],key,actor='subject:operator',
        expected_preview_digest=preview['preview_digest'],decision=decision,reason='已核对精确版本，记录下一步处理',**kwargs)


def test_no_findings_creates_an_independent_review_wait_without_claims_or_success(decision_case):
    b=decision_case(no_findings=True)
    before=b['store'].snapshot('a',b['target'])
    record=decide(b,'await_independent_review')
    after=b['store'].snapshot('a',b['target'])
    assert record['state']=='awaiting_independent_models' and not record['counts_as_success']
    assert not record['task_completed'] and not record['model_dispatched'] and not record['independently_verified']
    assert after==before
    assert b['decisions'].preview(*b['params'])['allowed_decisions']==[]
    assert b['store'].claim_next('a',b['target'],'validator',channel='verification') is None
    assert len(b['decisions'].history(*b['params'])['items'])==1


def test_defer_then_approve_fixer_preserves_history_and_creates_exact_grant(decision_case):
    b=decision_case();first=decide(b,'defer','hold')
    assert first['revision']==1 and first['grant_id'] is None
    approved=decide(b,'approve_fixer','review-route-approve',target_task_id=b['target']+':fix')
    assert approved['revision']==2 and approved['state']=='fixer_input_approved'
    assert [r['decision'] for r in b['decisions'].history(*b['params'])['items']]==['defer','approve_fixer']
    with b['store'].connect() as db:
        grant=db.execute('SELECT * FROM artifact_input_grants WHERE id=?',(approved['grant_id'],)).fetchone()
        assert grant['artifact_id']==b['artifact_id'] and grant['target_epoch']==0 and grant['actor']=='subject:operator'
    assert b['decisions'].preview(*b['params'])['allowed_decisions']==[]


def test_same_key_replays_receipt_and_binds_reason_actor_and_target(decision_case):
    b=decision_case(no_findings=True);p=b['decisions'].preview(*b['params'])
    kwargs=dict(actor='subject:operator',expected_preview_digest=p['preview_digest'],decision='await_independent_review',reason='等待独立复核')
    first=b['decisions'].decide(*b['params'],'one',**kwargs)
    events=b['store'].events('a',b['target'])
    assert b['decisions'].decide(*b['params'],'one',**kwargs)==first
    for key,value in [('reason','另一个理由'),('actor','subject:another'),('decision','defer')]:
        with pytest.raises(Conflict):b['decisions'].decide(*b['params'],'one',**{**kwargs,key:value})
    assert b['store'].events('a',b['target'])==events


@pytest.mark.parametrize('case',['no-findings-fixer','findings-no-fix','wrong-target','no-target','unexpected-target','unknown-decision','empty-reason','invalid-utf8','nul-reason','wrong-preview'])
def test_invalid_route_or_fields_have_no_write(decision_case,case):
    b=decision_case(no_findings=case=='no-findings-fixer')
    kwargs=dict(actor='subject:operator',expected_preview_digest=b['decisions'].preview(*b['params'])['preview_digest'],decision='defer',reason='明确理由')
    if case=='no-findings-fixer':kwargs.update(decision='approve_fixer',target_task_id=b['target']+':fix')
    elif case=='findings-no-fix':kwargs['decision']='await_independent_review'
    elif case=='wrong-target':kwargs.update(decision='approve_fixer',target_task_id=b['target']+':verify')
    elif case=='no-target':kwargs['decision']='approve_fixer'
    elif case=='unexpected-target':kwargs['target_task_id']=b['target']+':fix'
    elif case=='unknown-decision':kwargs['decision']='accept_success'
    elif case=='empty-reason':kwargs['reason']='   '
    elif case=='invalid-utf8':kwargs['reason']='\ud800'
    elif case=='nul-reason':kwargs['reason']='bad\x00reason'
    elif case=='wrong-preview':kwargs['expected_preview_digest']='0'*64
    before=b['store'].events('a',b['target'])
    with pytest.raises((Conflict,InvalidInput)):b['decisions'].decide(*b['params'],'bad',**kwargs)
    assert b['store'].events('a',b['target'])==before
    assert b['decisions'].history(*b['params'])['items']==[]


@pytest.mark.parametrize('change',['source','pause','cancel','unknown','reservation','target-epoch','hard-dependency'])
def test_changes_after_preview_reject_without_partial_records(decision_case,change):
    b=decision_case();p=b['decisions'].preview(*b['params'])
    with b['store'].transaction() as db:
        if change=='pause':db.execute("UPDATE runs SET status='paused' WHERE id=?",(b['target'],))
        elif change=='cancel':db.execute("UPDATE runs SET status='cancelled' WHERE id=?",(b['target'],))
        elif change=='unknown':db.execute("UPDATE effects SET state='indeterminate' WHERE run_id=?",(b['target'],))
        elif change=='reservation':db.execute("UPDATE reservations SET state='reserved' WHERE run_id=?",(b['target'],))
        elif change=='target-epoch':db.execute('UPDATE tasks SET epoch=1 WHERE id=?',(b['target']+':fix',))
        elif change=='hard-dependency':db.execute('UPDATE tasks SET depends=? WHERE id=?',(json.dumps([b['target']+':review']),b['target']+':fix'))
    if change=='source':b['store'].invalidate('a',b['run'],b['claim'].task_id,'changed source')
    before=b['store'].events('a',b['target'])
    with pytest.raises(Conflict):b['decisions'].decide(*b['params'],'changed',actor='subject:operator',expected_preview_digest=p['preview_digest'],decision='approve_fixer',reason='审阅后批准',target_task_id=b['target']+':fix')
    assert b['store'].events('a',b['target'])==before
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM review_decisions').fetchone()[0]==0
    if change in {'unknown','reservation'}:
        with pytest.raises(Conflict):b['decisions'].history(*b['params'])
    else:
        assert b['decisions'].history(*b['params'])['items']==[]


def test_concurrent_operators_create_one_record_grant_and_receipt(decision_case):
    b=decision_case();p=b['decisions'].preview(*b['params'])
    def attempt(index):
        try:return b['decisions'].decide(*b['params'],'op-'+str(index),actor='subject:operator',expected_preview_digest=p['preview_digest'],decision='approve_fixer',reason='审阅后批准',target_task_id=b['target']+':fix')
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(r is not None for r in results)==1
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM review_decisions WHERE run_id=?',(b['target'],)).fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM artifact_input_grants WHERE run_id=?',(b['target'],)).fetchone()[0]==1


def test_event_failure_rolls_back_grant_record_and_receipt(decision_case):
    b=decision_case();before=b['store'].events('a',b['target']);original=b['store']._event
    def fail(*args,**kwargs):
        if args[3]=='review.decision_recorded':raise RuntimeError('synthetic audit failure')
        return original(*args,**kwargs)
    with patch.object(b['store'],'_event',side_effect=fail),pytest.raises(RuntimeError):decide(b,'approve_fixer',target_task_id=b['target']+':fix')
    assert b['store'].events('a',b['target'])==before
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM review_decisions').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM artifact_input_grants WHERE run_id=?',(b['target'],)).fetchone()[0]==0


def test_reopen_and_append_only_history_remains_readable_after_source_change(decision_case):
    b=decision_case(no_findings=True);record=decide(b,'await_independent_review')
    b['decisions']=ReviewDecisionService(b['store'])
    assert b['decisions'].history(*b['params'])['items'][0]['decision_id']==record['decision_id']
    with b['store'].connect() as db:
        for command in ['UPDATE review_decisions SET revision=2','DELETE FROM review_decisions']:
            with pytest.raises(sqlite3.IntegrityError):db.execute(command)
    b['store'].invalidate('a',b['run'],b['claim'].task_id,'changed')
    p=b['decisions'].preview(*b['params']);assert p['stale'] and p['allowed_decisions']==[]
    assert b['decisions'].history(*b['params'])['items'][0]['subject']==b['body']['subject']


def test_missing_audit_or_missing_projection_is_detected(decision_case):
    b=decision_case(no_findings=True);decide(b,'await_independent_review')
    with b['store'].transaction() as db:
        db.execute('DROP TRIGGER review_decisions_no_delete');db.execute('DELETE FROM review_decisions')
    with pytest.raises(Conflict,match='history differs'):b['decisions'].history(*b['params'])


def test_history_is_bounded_and_last_slot_reserved_for_routing(decision_case):
    b=decision_case(no_findings=True)
    for revision in range(31):decide(b,'defer','defer-'+str(revision))
    p=b['decisions'].preview(*b['params'])
    assert p['allowed_decisions']==['await_independent_review']
    with pytest.raises(Conflict):decide(b,'defer','overflow')
    final=decide(b,'await_independent_review','last-route')
    assert final['revision']==32 and b['decisions'].preview(*b['params'])['allowed_decisions']==[]


def test_http_authentication_operator_only_and_exact_idempotency(decision_case):
    b=decision_case(no_findings=True)
    viewer=LocalIdentity('a','viewer',frozenset({'viewer'}),'review-decision-viewer-fixture-00000000')
    with TestClient(create_app(b['store'],{'a':'review-decision-legacy-fixture-00000000','b':'review-decision-other-fixture-00000000'},identities=(OP,WORKER,viewer))) as client:
        base=f'/v1/runs/{b["target"]}/artifacts/{b["artifact_id"]}'
        header=lambda token:{'Authorization':'Bearer '+token,'Idempotency-Key':'api-decision'}
        assert client.get(base+'/review-decision-preview').status_code==401
        assert client.get(base+'/review-decisions',headers=header('review-decision-other-fixture-00000000')).status_code==404
        p=client.get(base+'/review-decision-preview',headers=header(viewer.token));assert p.status_code==200
        body=dict(expected_preview_digest=p.json()['preview_digest'],decision='await_independent_review',reason='等待独立复核')
        for token in (WORKER.token,viewer.token,'review-decision-legacy-fixture-00000000'):
            assert client.post(base+'/review-decisions',headers=header(token),json=body).status_code==403
        assert client.post(base+'/review-decisions',headers=header(OP.token),json={**body,'verified':True}).status_code==422
        assert client.post(base+'/review-decisions',headers=header(OP.token),json=body).status_code==201
        assert client.post(base+'/review-decisions',headers=header(OP.token),json=body).status_code==201
        assert len(client.get(base+'/review-decisions',headers=header(viewer.token)).json()['items'])==1
