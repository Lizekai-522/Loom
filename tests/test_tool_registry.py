from dataclasses import FrozenInstanceError
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.sandbox import FrozenTest, SandboxRejected
from loom.store import Store
from loom.tool_registry import TOOLS, catalog, run_python_tool
from tests.test_execution import FakeSandbox
from tests.test_public_access import public, login, ORIGIN


SPEC=dict(scope='local-workspace',role='developer',tools=['workspace.write','tests.run'])
CASES=(FrozenTest('one',1,1),)


@pytest.mark.parametrize('role',['developer','fixer'])
def test_registered_adapter_dispatches_exact_frozen_input(role):
    sandbox=FakeSandbox()
    result=run_python_tool(sandbox,{**SPEC,'role':role},{'main.py':'print(1)'},CASES)
    assert sandbox.execution_calls==1 and sandbox.inputs==[({'main.py':'print(1)'},CASES)]
    assert result.passed


@pytest.mark.parametrize('change',[{'role':'analyst'},{'role':'tester'},{'role':'validator'},
    {'scope':'host'},{'tools':['tests.run']},{'tools':['workspace.write']}])
def test_denied_capability_never_reaches_runtime(change):
    sandbox=FakeSandbox()
    with pytest.raises(SandboxRejected,match='TOOL_CAPABILITY_DENIED'):
        run_python_tool(sandbox,{**SPEC,**change},{'main.py':'print(1)'},CASES)
    assert sandbox.execution_calls==0


def test_registration_cannot_be_replaced_or_redirected():
    with pytest.raises(TypeError):TOOLS['shell.run']=TOOLS['tests.run']
    with pytest.raises(FrozenInstanceError):TOOLS['tests.run'].scope='host'
    with pytest.raises(SandboxRejected,match='TOOL_NOT_REGISTERED'):
        run_python_tool(FakeSandbox(),SPEC,{},CASES,tool_id='shell.run')
    with pytest.raises(SandboxRejected,match='TOOL_RUNTIME_NOT_CONFIGURED'):
        run_python_tool(None,SPEC,{},CASES)


def test_catalog_is_detached_and_has_no_health_or_authorization_claim():
    first=catalog('a',SimpleNamespace(sandbox_image='private-image-id'))
    assert first['runtime_configured'] and not first['runtime_health_checked']
    assert not first['model_dispatched'] and not first['counts_as_success']
    assert 'private-image-id' not in str(first)
    first['tools'][0]['label']='changed'
    assert catalog('b')['tools'][0]['label']=='Python 隔离测试'
    assert not catalog('b')['runtime_configured']


def test_catalog_requires_read_identity_and_is_read_only(tmp_path):
    store=Store(tmp_path/'tools.db')
    viewer=LocalIdentity('a','viewer',frozenset({'viewer'}),'tool-viewer-test-only-000000000000000')
    with TestClient(create_app(store,{},identities=(viewer,))) as client:
        assert client.get('/v1/tools').status_code==401
        auth={'Authorization':'Bearer '+viewer.token}
        with store.connect() as db:before=db.execute('SELECT count(*) FROM events').fetchone()[0]
        response=client.get('/v1/tools',headers=auth)
        assert response.status_code==200 and response.json()['tenant_id']=='a'
        assert viewer.token not in response.text
        assert client.post('/v1/tools',json={},headers=auth).status_code==405
        with store.connect() as db:assert db.execute('SELECT count(*) FROM events').fetchone()[0]==before


def test_invited_catalog_read_does_not_open_registration(public):
    client,_,_=public
    assert client.get('/v1/tools').status_code==401
    login(client)
    assert client.get('/v1/tools').json()['tenant_id']=='preview'
    assert client.post('/v1/tools',json={},headers={'Origin':ORIGIN}).status_code==403
