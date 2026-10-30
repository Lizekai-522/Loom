from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.domain_packs import DomainPackService
from loom.store import Store
from loom.workflow import WorkflowService
from tests.test_domain_packs import bundle
from tests.test_public_access import public, login, HEADERS


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path/'api.db'); workflow = WorkflowService(store)
    run = workflow.create_goal('a','goal',title='Assets',description='No model calls',acceptance=['schema.valid'])['run_id']
    roles = ('operator','worker','viewer','other')
    tokens = {role:'domain-pack-fixture-token-only-'+role+'-00000000' for role in (*roles,'legacy')}
    identities = tuple(LocalIdentity('b' if role=='other' else 'a',role,
        frozenset({'viewer' if role=='other' else role}),tokens[role]) for role in roles)
    with TestClient(create_app(store,{'a':tokens['legacy']},identities=identities)) as client:
        def request(method,path,body=None,*,role='operator',key='publish'):
            headers = {} if role is None else {'Authorization':'Bearer '+tokens[role]}
            if key is not None:headers['Idempotency-Key'] = key
            return client.request(method,path,json=body,headers=headers)
        yield store,workflow,run,request


@pytest.mark.parametrize('role,code',[('operator',201),('legacy',403),('worker',403),('viewer',403),(None,401)])
def test_named_operator_alone_publishes(api,role,code):
    store,_,run,request = api
    before = store.snapshot('a',run)
    result = request('POST','/v1/domain-packs',{'audit_run_id':run,'bundle':bundle()},role=role)
    assert result.status_code == code and store.snapshot('a',run) == before
    if code == 201:
        assert result.json()['actor'] == 'subject:operator'
        assert result.json()['grants_authority'] is False
        assert request('POST','/v1/domain-packs',{'audit_run_id':run,'bundle':bundle()}).json() == result.json()


@pytest.mark.parametrize('role',['operator','worker','viewer','legacy'])
def test_authenticated_catalog_template_and_export_are_read_only(api,role):
    store,workflow,run,request = api
    request('POST','/v1/domain-packs',{'audit_run_id':run,'bundle':bundle()})
    before = store.events('a',run)
    catalog = request('GET','/v1/domain-packs',role=role)
    assert catalog.status_code == 200 and len(catalog.json()['items']) == 2
    result = request('GET','/v1/domain-packs/documents/1.0.0',role=role)
    assert result.status_code == 200
    template = request('GET','/v1/domain-packs/documents/1.0.0/templates/requirements',role=role).json()
    assert template['definition']['pack_ref'] == result.json()['reference']
    assert not template['plan_approved'] and workflow.get_plan('a',run)['revision'] == 0
    assert store.events('a',run) == before


def test_other_tenant_unknown_version_and_missing_idempotency_key(api):
    _,_,run,request = api
    assert request('POST','/v1/domain-packs',{'audit_run_id':run,'bundle':bundle()},key=None).status_code == 422
    request('POST','/v1/domain-packs',{'audit_run_id':run,'bundle':bundle()})
    assert len(request('GET','/v1/domain-packs',role='other').json()['items']) == 1
    assert request('GET','/v1/domain-packs/documents/1.0.0',role='other').status_code == 404
    assert request('GET','/v1/domain-packs/documents/2.0.0').status_code == 404
    assert request('GET','/v1/domain-packs').json()['items'][0]['grants_authority'] is False


@pytest.mark.parametrize('extra',[{'actor':'subject:other'},{'tenant':'b'},{'execute':True},{'approved':True}])
def test_publish_body_cannot_supply_authority(api,extra):
    _,_,run,request = api
    assert request('POST','/v1/domain-packs',{'audit_run_id':run,'bundle':bundle(),**extra}).status_code == 422


def test_public_guard_allows_invited_read_and_denies_all_pack_publications(public):
    client,_,store = public
    assert client.get('/v1/domain-packs').status_code == 401
    login(client)
    assert client.get('/v1/domain-packs').status_code == 200
    template = client.get('/v1/domain-packs/software-dev/0.1.0/templates/seven-roles')
    assert template.status_code == 200 and not template.json()['plan_approved']
    assert client.post('/v1/domain-packs',json={'bundle':bundle()},headers=HEADERS).status_code == 403
    with store.connect() as db:assert db.execute('SELECT count(*) FROM domain_packs').fetchone()[0] == 0
