"""Local development convenience must not become remote/cross-origin access."""
import pytest
from fastapi.testclient import TestClient
from loom.api import create_app
from loom.auth import AuthConfigError, LocalIdentity
from loom.store import Store

TOKEN='local-console-public-fixture-operator-00000000'
IDENTITY=LocalIdentity('a','operator',frozenset({'operator'}),TOKEN)

def client(tmp_path, *, enabled=True, peer='127.0.0.1', base='http://127.0.0.1:8796'):
    return TestClient(create_app(Store(tmp_path/'console.db'),{},identities=(IDENTITY,),local_console=enabled),
                      base_url=base,client=(peer,50000))

def test_auto_operator_contains_no_token_and_never_gains_worker_permissions(tmp_path):
    with client(tmp_path) as c:
        response=c.get('/v1/me',headers={'X-Loom-Console':'local'})
        assert response.status_code==200 and response.json()['authentication']=='local-console'
        assert response.json()['roles']==['operator'] and TOKEN not in response.text
        headers={'X-Loom-Console':'local','Origin':'http://127.0.0.1:8796','Idempotency-Key':'goal'}
        goal=c.post('/v1/goals',headers=headers,json={'title':'中文目标','description':'本机测试','acceptance':['tests.pass']})
        assert goal.status_code==201
        run=goal.json()['run_id']
        assert c.post('/v1/runs/'+run+'/claim',headers=headers,json={'worker_id':'operator'}).status_code==403
        events=c.get('/v1/runs/'+run+'/events',headers=headers).json()['events']
        assert any(event['actor']=='subject:operator' for event in events)

@pytest.mark.parametrize('headers',[
    {},{'X-Loom-Console':'wrong'}, {'X-Loom-Console':'local','Origin':'https://evil.example'},
    {'X-Loom-Console':'local','Origin':'null'}, {'X-Loom-Console':'local','Origin':'http://127.0.0.1:8797'},
    {'X-Loom-Console':'local','Origin':'http://localhost:8796'},
    {'X-Loom-Console':'local','Origin':'http://127.0.0.1:8796/path'},
    {'X-Loom-Console':'local','Sec-Fetch-Site':'cross-site'},
    {'X-Loom-Console':'local','Sec-Fetch-Site':'same-site'},
    {'X-Loom-Console':'local','X-Forwarded-For':'127.0.0.1'},
    {'X-Loom-Console':'local','Authorization':'Bearer invalid'},
    {'X-Loom-Console':'local','Authorization':'Basic invalid'},
])
def test_bad_origin_marker_or_bearer_never_falls_back(tmp_path,headers):
    with client(tmp_path) as c:
        assert c.get('/v1/me',headers=headers).status_code==401

@pytest.mark.parametrize('peer,base',[
    ('192.0.2.12','http://127.0.0.1:8796'),('127.0.0.1','http://evil.example:8796'),
    ('127.0.0.1','http://127.0.0.1.evil.example:8796')])
def test_remote_peers_and_rebinding_host_rejected(tmp_path,peer,base):
    with client(tmp_path,peer=peer,base=base) as c:
        assert c.get('/v1/me',headers={'X-Loom-Console':'local'}).status_code==401

def test_opt_in_required_and_original_bearer_still_works(tmp_path):
    with client(tmp_path,enabled=False) as c:
        assert c.get('/v1/me',headers={'X-Loom-Console':'local'}).status_code==401
        assert c.get('/v1/me',headers={'Authorization':'Bearer '+TOKEN}).json()['authentication']=='local-bearer'

@pytest.mark.parametrize('identities',[(),(IDENTITY,LocalIdentity('b','op',frozenset({'operator'}),'local-console-public-fixture-other-00000000'))])
def test_ambiguous_or_absent_operator_refuses_startup(tmp_path,identities):
    with pytest.raises(AuthConfigError):create_app(Store(tmp_path/'c.db'),{},identities=identities,local_console=True)

def test_connection_page_has_no_credential_input(tmp_path):
    with client(tmp_path) as c:
        text=c.get('/console').text
        assert 'id="access-token"' not in text and 'type="password"' not in text
        assert 'console-zh.js' in text
        js=c.get('/console/assets/console.js').text
        assert 'state.token' not in js and 'connect();' in js
