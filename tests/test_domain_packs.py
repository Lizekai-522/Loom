"""Actual registry transactions and frozen plans; no provider calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from loom.builtin_resources import fixture_manifest, software_workflow
from loom.demo import run_demo
from loom.domain_packs import DomainPackService, builtin_pack
from loom.store import Conflict, InvalidInput, NotFound, Store, canonical, digest
from loom.workflow import WorkflowDefinition, WorkflowService


@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path/'packs.db')
    workflow = WorkflowService(store)
    run = workflow.create_goal('a', 'audit', title='Assets', description='No execution',
        acceptance=['schema.valid'], cost_limit=1000000, token_limit=10000)['run_id']
    return store, workflow, DomainPackService(store), run


def bundle(*, version='1.0.0', name='documents'):
    source = builtin_pack().model_dump(mode='json')
    source.update(name=name, version=version, title='文档项目', description='只使用已登记的七种适配器')
    source['templates'] = [dict(id='requirements', title='梳理需求', nodes=[dict(
        id='spec', title='梳理需求', instruction='产出可核对的需求候选', role='analyst',
        role_output={'kind':'requirements','schema_version':'loom.requirements/v1'},
        limits={'cost_limit_microusd':10000,'token_limit':1000,'max_attempts':1})])]
    return source


def publish(case, data=None, *, key='publish'):
    return case[2].publish('a', key, case[3], bundle() if data is None else data, actor='subject:operator')


def test_builtin_resources_are_semantically_identical_and_detached_from_cwd(tmp_path, monkeypatch):
    original = Path(__file__).resolve().parents[1]/'packs/software-dev'
    expected = {name:json.loads((original/name).read_bytes()) for name in ('workflow-v1.json','manifest.json')}
    monkeypatch.chdir(tmp_path)
    workflow = software_workflow()
    assert workflow == expected['workflow-v1.json']
    assert fixture_manifest() == expected['manifest.json']
    assert fixture_manifest()['metadata']['executionMode'] == 'deterministic-fixture'
    workflow['roles'].clear()
    assert len(software_workflow()['roles']) == 7
    report = run_demo(Store(tmp_path/'demo.db'))
    assert report['mode'] == 'deterministic-fixture' and report['provider_cost_usd'] == 0
    assert not report['real_models_used'] and report['tasks'] == 7


def test_legacy_workflow_serialization_keeps_original_digest_and_contract(case):
    store, workflow, _, run = case
    old = software_workflow()
    parsed = WorkflowDefinition.model_validate(old).model_dump(mode='json')
    assert 'pack_ref' not in parsed
    saved = workflow.save_plan('a',run,'save',old,0)
    workflow.approve_plan('a',run,'approve',saved['revision'],saved['digest'])
    spec = workflow.get_task_spec('a',run,run+':spec')
    assert 'pack_ref' not in spec and spec['model_alias'] == 'deepseek-flash'
    assert saved['digest'] == digest(parsed)


def test_publish_is_idempotent_audited_and_never_approves_or_spends(case):
    store, workflow, packs, run = case
    before = store.snapshot('a',run); plan = workflow.get_plan('a',run)
    published = publish(case)
    assert publish(case) == published
    assert published['reference']['content_digest'] == digest(published['bundle'])
    assert not published['grants_authority'] and not published['plan_approved']
    assert packs.get('a','documents','1.0.0') == published
    assert workflow.get_plan('a',run) == plan and store.snapshot('a',run) == before
    assert store.verify_events(store.events('a',run))
    assert sum(e['type']=='domain_pack.published' for e in store.events('a',run)) == 1
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
    with pytest.raises(Conflict):publish(case,key='new-key')
    modified = bundle(); modified['title'] = 'changed'
    with pytest.raises(Conflict):publish(case,modified)


def test_custom_template_plan_and_task_pin_version_until_explicit_new_plan(case):
    store, workflow, packs, run = case
    first = publish(case)
    template = packs.template('a','documents','1.0.0','requirements')
    assert not template['plan_approved']
    definition = template['definition']
    saved = workflow.save_plan('a',run,'save',definition,0,actor='subject:operator')
    workflow.approve_plan('a',run,'approve',saved['revision'],saved['digest'],actor='subject:operator')
    spec = workflow.get_task_spec('a',run,run+':spec')
    assert spec['pack_ref'] == first['reference'] and spec['role'] == 'analyst'
    later = bundle(version='1.1.0'); later['roles'][0]['model_alias'] = 'different-route'
    publish(case,later,key='next-version')
    assert workflow.get_task_spec('a',run,run+':spec') == spec
    assert packs.template('a','documents','1.0.0','requirements') == template
    assert packs.template('a','documents','1.1.0','requirements')['definition']['roles'][0]['model_alias'] == 'different-route'
    # Choosing the older immutable version remains possible without mutating defaults.
    assert packs.get('a','documents','1.0.0') == first


@pytest.mark.parametrize('change', ['digest','name','roles','version'])
def test_plan_ref_mismatch_rejected_before_any_task_creation(case, change):
    store, workflow, packs, run = case; publish(case)
    definition = packs.template('a','documents','1.0.0','requirements')['definition']
    if change == 'digest':definition['pack_ref']['content_digest'] = '0'*64
    elif change == 'name':definition['pack_ref']['name'] = 'elsewhere'
    elif change == 'version':definition['pack_ref']['version'] = '1.0.1'
    else:definition['roles'][0]['model_alias'] = 'new-route'
    before = store.snapshot('a',run)
    with pytest.raises((Conflict,InvalidInput,NotFound)):
        workflow.save_plan('a',run,'bad',definition,0)
    assert store.snapshot('a',run) == before and workflow.get_plan('a',run)['revision'] == 0


@pytest.mark.parametrize('change', ['unknown_role','host_tool','scope','schema','duplicate_role','duplicate_template','cycle','foreign_suite'])
def test_packs_cannot_add_adapters_permissions_or_unsupported_schemas(case, change):
    data = bundle()
    if change == 'unknown_role':data['roles'][0]['id'] = 'shell-admin'
    elif change == 'host_tool':data['roles'][0]['tools'].append('shell.run')
    elif change == 'scope':data['scope'] = 'internet'
    elif change == 'schema':data['schema_version'] = 'loom.domain-pack/v2'
    elif change == 'duplicate_role':data['roles'][1] = deepcopy(data['roles'][0])
    elif change == 'duplicate_template':data['templates'].append(deepcopy(data['templates'][0]))
    elif change == 'cycle':data['templates'][0]['nodes'][0]['depends'] = ['spec']
    else:data['templates'][0]['nodes'][0]['evaluation_digest'] = 'a'*64
    with pytest.raises(InvalidInput):publish(case,data)
    with case[0].connect() as db:assert db.execute('SELECT count(*) FROM domain_packs').fetchone()[0] == 0


def test_builtin_version_read_only_and_missing_templates_and_tenants(case):
    _, _, packs, run = case; publish(case)
    with pytest.raises(Conflict):publish(case,builtin_pack())
    with pytest.raises(NotFound):packs.get('b','documents','1.0.0')
    with pytest.raises(NotFound):packs.template('a','documents','1.0.0','missing')
    assert len(packs.catalog('b')) == 1 and len(packs.catalog('a')) == 2
    with pytest.raises(NotFound):packs.publish('b','p',run,bundle(),actor='subject:operator')


@pytest.mark.parametrize('identity', [('../secret','1.0.0'),('documents','01.0.0'),('documents','1.0'),('documents','https://model.invalid')])
def test_pack_identifiers_never_resolve_paths_or_urls(case,identity):
    with pytest.raises(InvalidInput):case[2].get('a',*identity)


def test_registry_sqlite_immutability_and_rebuilt_event_binding(case):
    store, workflow, packs, run = case; publish(case)
    definition = packs.template('a','documents','1.0.0','requirements')['definition']
    saved = workflow.save_plan('a',run,'save',definition,0)
    with store.transaction() as db:
        for sql in ("UPDATE domain_packs SET actor='subject:other'", 'DELETE FROM domain_packs'):
            with pytest.raises(sqlite3.IntegrityError):db.execute(sql)
    with store.transaction() as db:
        db.execute('DROP TRIGGER domain_packs_no_update')
        data = json.loads(db.execute('SELECT bundle FROM domain_packs').fetchone()[0]); data['title'] = 'mutated'
        db.execute('UPDATE domain_packs SET bundle=?,content_digest=?',(canonical(data),digest(data)))
    with pytest.raises(Conflict):packs.get('a','documents','1.0.0')
    with pytest.raises(Conflict):workflow.approve_plan('a',run,'approve',saved['revision'],saved['digest'])
    assert not store.snapshot('a',run)['tasks']


def test_concurrent_same_version_publication_has_one_atomic_winner(case):
    def attempt(index):
        try:return publish(case,key='publish-'+str(index))
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=4) as pool:results = list(pool.map(attempt,range(4)))
    assert sum(result is not None for result in results) == 1
    assert len(case[2].catalog('a')) == 2
    assert case[0].verify_events(case[0].events('a',case[3]))
