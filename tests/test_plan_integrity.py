"""Approved graph bindings must still govern the actual scheduler projection."""
import json
from pathlib import Path

import pytest

from loom.artifacts import ArtifactService
from loom.store import Conflict, Store
from loom.workflow import WorkflowService


@pytest.fixture
def approved_plan(tmp_path):
    store = Store(tmp_path / 'plan-integrity.db')
    workflow = WorkflowService(store)
    definition = json.loads((Path(__file__).resolve().parents[1] / 'packs/software-dev/workflow-v1.json').read_bytes())
    goal = workflow.create_goal('tenant-a', 'goal', title='Plan integrity', description='Check approved dependencies', acceptance=['tests.pass'])
    run = goal['run_id']
    saved = workflow.save_plan('tenant-a', run, 'plan', definition, 0)
    approved = workflow.approve_plan('tenant-a', run, 'approve', saved['revision'], saved['digest'])
    return store, workflow, run, approved['task_ids']


def test_tampered_runtime_dependency_cannot_unlock_scheduler_or_preview(approved_plan):
    store, workflow, run, ids = approved_plan
    with store.transaction() as db:
        # These predecessors are submitted and remain unverified, not done.
        for name in ('spec', 'design', 'case'):
            db.execute("UPDATE tasks SET status='submitted' WHERE tenant=? AND id=?", ('tenant-a', ids[name]))
        db.execute("UPDATE tasks SET depends='[]' WHERE tenant=? AND id=?", ('tenant-a', ids['code']))
    before = store.events('tenant-a', run)
    with pytest.raises(Conflict):
        store.claim_next('tenant-a', run, 'worker')
    with pytest.raises(Conflict):
        workflow.get_task_spec('tenant-a', run, ids['code'])
    with store.connect() as db, pytest.raises(Conflict):
        ArtifactService(store, initialize=False)._contract(db, 'tenant-a', run, ids['code'])
    assert store.events('tenant-a', run) == before
    with store.connect() as db:
        task = db.execute('SELECT status,epoch FROM tasks WHERE tenant=? AND id=?', ('tenant-a', ids['code'])).fetchone()
    assert (task['status'], task['epoch']) == ('pending', 0)


@pytest.mark.parametrize(('field', 'value'), [('priority', 999), ('channel', 'verification'), ('intent_id', 'other-intent')])
def test_frozen_task_projection_rejects_changed_scheduling_fields(approved_plan, field, value):
    store, workflow, run, ids = approved_plan
    with store.transaction() as db:
        db.execute('UPDATE tasks SET ' + field + '=? WHERE tenant=? AND id=?', (value, 'tenant-a', ids['spec']))
    with pytest.raises(Conflict):
        workflow.get_task_spec('tenant-a', run, ids['spec'])


def test_changed_projection_after_claim_cannot_reserve_or_create_effect(approved_plan):
    store, _, run, ids = approved_plan
    claim = store.claim_next('tenant-a', run, 'worker')
    assert claim.task_id == ids['spec']
    with store.transaction() as db:
        db.execute('UPDATE tasks SET priority=999 WHERE tenant=? AND id=?', ('tenant-a', ids['spec']))
    before = store.events('tenant-a', run)
    with pytest.raises(Conflict):
        store.reserve(claim, 'changed-projection', max_cost=1, max_tokens=1)
    assert store.events('tenant-a', run) == before
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['claim', 'legacy-add'])
def test_missing_managed_plan_cannot_fall_back_to_legacy_task_path(approved_plan, operation):
    store, _, run, _ = approved_plan
    with store.transaction() as db:
        db.execute('DELETE FROM workflow_tasks WHERE tenant=? AND run_id=?', ('tenant-a', run))
        db.execute('DELETE FROM workflow_goals WHERE tenant=? AND run_id=?', ('tenant-a', run))
    before = store.events('tenant-a', run)
    with pytest.raises(Conflict):
        if operation == 'claim':
            store.claim_next('tenant-a', run, 'worker')
        else:
            store.add_task('tenant-a', run, 'legacy-injection', task_id='unapproved-task', title='Unapproved')
    assert store.events('tenant-a', run) == before
