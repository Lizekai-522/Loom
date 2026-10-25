"""No duplicate Goal, wrong-task claim, source drift, retry or preview write."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import sqlite3
import pytest
from loom.approved_execution import ApprovedExecutionService, ReadOnlyStore
from loom.auth import LocalIdentity
from loom.sandbox import FrozenTest
from loom.store import Conflict, InvalidInput
from tests.test_suite_binding import ACCEPTANCE, binding, approve_binding

OP=LocalIdentity('a','operator',frozenset({'operator'}),'approved-execution-fixture-operator-00000000')
WORKER=LocalIdentity('a','worker',frozenset({'worker'}),'approved-execution-fixture-worker-00000000')

def prepared(b):
    saved,_=approve_binding(b)
    service=ApprovedExecutionService(b['store'])
    params=('a',b['target'],'work',saved['digest'],ACCEPTANCE)
    return service,params

def claim(service,params,preview):
    return service.claim(*params,preview['preview_digest'],operator=OP,worker=WORKER)

def test_read_only_preview_preserves_source_plan_host_secrets_and_database(binding):
    b=binding;service,params=prepared(b)
    before=b['store'].events('a',b['target']);source=b['store'].events('a',b['source'])
    preview=ApprovedExecutionService(ReadOnlyStore(b['store'].path)).preview(*params)
    assert preview['creates_new_goal'] is False and preview['network_requests']==0
    assert preview['development_suite']==b['ref'] and '674593' not in str(preview)
    assert b['store'].events('a',b['target'])==before and b['store'].events('a',b['source'])==source
    with ReadOnlyStore(b['store'].path).connect() as db:
        with pytest.raises(sqlite3.OperationalError):db.execute('CREATE TABLE forbidden(a)')

def test_exact_task_claim_creates_no_goal_or_plan_and_retry_is_denied(binding):
    b=binding;service,params=prepared(b);preview=service.preview(*params)
    plan=b['workflow'].get_plan('a',b['target'])
    with b['store'].connect() as db:count=db.execute('SELECT count(*) FROM runs').fetchone()[0]
    current=claim(service,params,preview)
    assert current.task_id==b['target']+':work' and current.epoch==1
    assert b['workflow'].get_plan('a',b['target'])==plan
    with b['store'].connect() as db:assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==count
    with pytest.raises(Conflict,match='already started'):claim(service,params,preview)

@pytest.mark.parametrize('bad', ['plan','host','source','paused','budget','unknown','dependency'])
def test_changes_stop_before_claim_and_without_partial_events(binding,bad):
    b=binding;service,params=prepared(b)
    before=b['store'].events('a',b['target'])
    if bad=='plan':params=(*params[:3],'0'*64,params[4])
    elif bad=='host':params=(*params[:4],(FrozenTest('wrong',1,2),))
    elif bad=='source':b['store'].invalidate('a',b['source'],b['source_claim'].task_id,'changed')
    else:
        with b['store'].transaction() as db:
            if bad=='paused':db.execute("UPDATE runs SET status='paused' WHERE id=?",(b['target'],))
            elif bad=='budget':db.execute('UPDATE runs SET cost_limit=1 WHERE id=?',(b['target'],))
            elif bad=='unknown':db.execute("INSERT INTO effects(tenant,id,run_id,task_id,epoch,request_hash,state,result) VALUES ('a','unknown',?,? ,0,'hash','intent',NULL)",(b['target'],b['target']+':work'))
            elif bad=='dependency':db.execute('UPDATE tasks SET depends=? WHERE id=?',('["unverified-parent"]',b['target']+':work'))
    with pytest.raises(Conflict):service.preview(*params)
    assert b['store'].events('a',b['target'])==before

def test_changed_preview_digest_and_wrong_identity_do_not_claim(binding):
    b=binding;service,params=prepared(b);before=b['store'].events('a',b['target'])
    with pytest.raises(Conflict):claim(service,params,{'preview_digest':'0'*64})
    with pytest.raises(InvalidInput):service.claim(*params,'0'*64,operator=WORKER,worker=OP)
    assert b['store'].events('a',b['target'])==before

def test_concurrent_callers_cannot_claim_or_execute_twice(binding):
    service,params=prepared(binding);preview=service.preview(*params)
    def attempt(_):
        try:return claim(service,params,preview)
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
    assert sum(r is not None for r in results)==1

def test_audit_failure_rolls_back_the_claim(binding):
    b=binding;service,params=prepared(b);preview=service.preview(*params)
    original=b['store']._event
    def failed(*args,**kwargs):
        if args[3]=='execution.existing_plan_admitted':raise RuntimeError('fixture audit failure')
        return original(*args,**kwargs)
    before=b['store'].events('a',b['target'])
    with patch.object(b['store'],'_event',side_effect=failed),pytest.raises(RuntimeError):claim(service,params,preview)
    assert b['store'].events('a',b['target'])==before
    assert service.preview(*params)['preview_digest']==preview['preview_digest']
