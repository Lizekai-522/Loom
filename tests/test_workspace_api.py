"""Workspace persistence/isolation and scheduler queueing; no model/network calls."""
import json
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from loom.auth import AuthRegistry, LocalIdentity, Principal
from loom.store import Conflict, InvalidInput, LoomError, NotFound, Store
from loom.workspace import Workspace
from loom.workspace_api import install_workspace_api


ALICE = LocalIdentity('shared', 'alice', frozenset({'user'}), 'workspace-fixture-alice-token-000000000')
BOB = LocalIdentity('shared', 'bob', frozenset({'user'}), 'workspace-fixture-bob-token-00000000000')
OTHER = LocalIdentity('other', 'alice', frozenset({'user'}), 'workspace-fixture-other-token-000000000')
WORKER = LocalIdentity('shared', 'worker', frozenset({'worker'}), 'workspace-fixture-worker-token-00000000')
OPERATOR = LocalIdentity('shared', 'operator', frozenset({'operator'}), 'workspace-fixture-operator-token-000000')


def headers(identity=ALICE, key='workspace-test'):
    return {'Authorization': 'Bearer ' + identity.token, 'Idempotency-Key': key}


def app_for(tmp_path, *, scheduler=False):
    store = Store(tmp_path / 'workspace.db')
    models = tmp_path / 'models.json'
    models.write_text(json.dumps(dict(routes=[
        dict(alias='deepseek-flash', provider='deepseek', model='deepseek-flash', key_env='PRIVATE_ENV'),
        dict(alias='unknown', provider='unknown', model='not-priced', api_key='never-return-this'),
        dict(alias='deepseek-v4-pro', provider='wrong-provider', model='deepseek-v4-pro')])), encoding='utf-8')
    config = SimpleNamespace(model_config=models, source_path=None)
    app = FastAPI()
    registry = AuthRegistry({}, [ALICE, BOB, OTHER, WORKER, OPERATOR])

    def reader(request: Request):
        auth = request.headers.get('authorization', '')
        identity = registry.authenticate(auth[7:]) if auth.startswith('Bearer ') else None
        if identity is None:
            raise HTTPException(401)
        if not registry.authorize(identity, 'read'):
            raise HTTPException(403)
        return identity

    def writer(identity: Annotated[Principal, Depends(reader)]):
        if not registry.authorize(identity, 'goal.write'):
            raise HTTPException(403)
        return identity

    @app.exception_handler(LoomError)
    async def error(request, exc):
        status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 422
        return JSONResponse({'code': exc.code}, status_code=status)

    workspace = install_workspace_api(app, store,
        Reader=Annotated[Principal, Depends(reader)], Writer=Annotated[Principal, Depends(writer)],
        executor_config=config, scheduler=scheduler)
    return app, workspace, store


@pytest.fixture
def env(tmp_path):
    app, workspace, store = app_for(tmp_path)
    with TestClient(app) as client:
        yield client, workspace, store


def test_project_crud_persists_and_isolates_users_and_tenants(env):
    client, workspace, store = env
    result = client.post('/v1/workspace/projects', headers=headers(), json={'name': '测试项目'})
    assert result.status_code == 201
    project = result.json()
    assert Path(project['path']).is_dir()
    assert Path(project['path']).is_relative_to(workspace.root)
    assert client.get('/v1/workspace/projects', headers=headers(BOB)).json()['items'] == []
    assert client.get('/v1/workspace/projects/' + project['id'], headers=headers(BOB)).status_code == 404
    assert client.get('/v1/workspace/projects/' + project['id'], headers=headers(OTHER)).status_code == 404
    assert client.patch('/v1/workspace/projects/' + project['id'], headers=headers(), json={'name': '改名'}).json()['name'] == '改名'
    assert client.patch('/v1/workspace/preferences', headers=headers(), json={'selected_project_id': project['id']}).status_code == 200
    marker = Path(project['path']) / 'keep.txt'
    marker.write_text('keep')
    assert Workspace(store).project(ALICE, project['id'])['name'] == '改名'
    assert client.delete('/v1/workspace/projects/' + project['id'], headers=headers()).json()['files_retained']
    assert marker.is_file()
    assert client.get('/v1/workspace/preferences', headers=headers()).json()['selected_project_id'] is None
    assert client.get('/v1/workspace/projects/' + project['id'], headers=headers()).status_code == 404


def test_project_path_is_server_managed_and_writes_are_human_only(env):
    client, _, _ = env
    assert client.post('/v1/workspace/projects', headers=headers(), json={'name': 'x', 'path': 'C:/'}).status_code == 422
    assert client.post('/v1/workspace/projects', headers=headers(WORKER), json={'name': 'x'}).status_code == 403
    assert client.post('/v1/workspace/projects', headers=headers(OPERATOR), json={'name': 'x'}).status_code == 403
    assert client.get('/v1/workspace/projects').status_code == 401


def test_preferences_persist_validate_and_do_not_grant_permissions(env):
    client, workspace, store = env
    changed = dict(appearance='dark', font='mono', font_size=18, shortcuts='vim', confirmation_mode='read_only', preferred_model='deepseek-flash')
    assert client.patch('/v1/workspace/preferences', headers=headers(), json=changed).status_code == 200
    assert Workspace(store).preferences(ALICE)['confirmation_mode'] == 'read_only'
    assert client.get('/v1/workspace/preferences', headers=headers(BOB)).json()['appearance'] == 'light'
    for value in ({'appearance': 'invalid'}, {'font_size': 99}, {'confirmation_mode': 'auto'}, {'api_key': 'private'}, {'preferred_model': 'unknown'}, {'selected_project_id': '../escape'}):
        assert client.patch('/v1/workspace/preferences', headers=headers(), json=value).status_code in (404, 422)
    assert workspace.assistant.ledger.verify(ALICE.tenant_id, 'workspace')


def test_models_expose_only_configured_priced_aliases_without_provider_secrets(env):
    client, _, _ = env
    response = client.get('/v1/workspace/models', headers=headers())
    assert response.json() == {'items': [{'alias': 'deepseek-flash', 'server_managed': True}], 'server_managed': True}
    assert 'PRIVATE_ENV' not in response.text and 'never-return-this' not in response.text


def test_automation_crud_and_due_dispatch_are_atomic_and_restart_idempotent(env):
    client, workspace, store = env
    automation = client.post('/v1/workspace/automations', headers=headers(), json={
        'name': '定时问候', 'prompt': '问候我', 'interval_minutes': 5,
        'due_at': 1000.0, 'model': 'deepseek-flash'}).json()
    identifier = automation['id']
    assert automation['enabled'] is False
    assert workspace.dispatch_due(now=1001.0) == []
    assert client.patch('/v1/workspace/automations/' + identifier, headers=headers(), json={'name': '更新'}).status_code == 200
    assert client.get('/v1/workspace/automations', headers=headers(BOB)).json()['items'] == []
    assert client.post('/v1/workspace/automations/' + identifier + '/enable', headers=headers()).status_code == 200
    receipts = workspace.dispatch_due(now=1001.0)
    assert len(receipts) == 1 and receipts[0]['state'] == 'queued'
    assert receipts[0]['model_called'] is False
    restarted = Workspace(store, executor_config=workspace.executor_config)
    assert restarted.dispatch_due(now=1001.0) == []
    messages = workspace.assistant.messages(ALICE.tenant_id, workspace.assistant.thread_of(ALICE.tenant_id, ALICE.subject_id)['id'])
    assert len(messages) == 1 and messages[0]['text'] == '问候我'
    assert messages[0]['meta']['model_alias'] == 'deepseek-flash'
    assert messages[0]['meta']['confirmation_mode'] == 'ask'
    assert workspace.assistant.thread_of(BOB.tenant_id, BOB.subject_id) is None
    assert workspace.automation(ALICE, identifier)['next_run_at'] == 1300.0
    assert client.post('/v1/workspace/automations/' + identifier + '/disable', headers=headers()).json()['enabled'] is False
    assert workspace.dispatch_due(now=2000.0) == []
    assert client.delete('/v1/workspace/automations/' + identifier, headers=headers()).status_code == 200
    assert client.get('/v1/workspace/automations/' + identifier, headers=headers()).status_code == 404


def test_run_now_requires_key_deduplicates_and_preserves_read_only_preference(env):
    client, workspace, _ = env
    client.patch('/v1/workspace/preferences', headers=headers(), json={'confirmation_mode': 'read_only'})
    automation = client.post('/v1/workspace/automations', headers=headers(), json={'name': '手动', 'prompt': '列出待办'}).json()
    path = '/v1/workspace/automations/' + automation['id'] + '/run-now'
    assert client.post(path, headers={'Authorization': 'Bearer ' + ALICE.token}).status_code == 422
    one = client.post(path, headers=headers()).json()
    two = client.post(path, headers=headers()).json()
    assert one['id'] == two['id']
    assert client.post(path, headers=headers(BOB)).status_code == 404
    messages = workspace.assistant.messages(ALICE.tenant_id, workspace.assistant.thread_of(ALICE.tenant_id, ALICE.subject_id)['id'])
    assert len(messages) == 1
    assert messages[0]['meta']['confirmation_mode'] == 'read_only'
    assert client.get('/v1/workspace/automations/' + automation['id'] + '/runs', headers=headers()).json()['items'][0]['message_id'] == one['message_id']
    workspace.assistant.update_message(ALICE.tenant_id, one['message_id'], status='answered')
    receipt = client.get('/v1/workspace/automations/' + automation['id'] + '/runs', headers=headers()).json()['items'][0]
    assert receipt['state'] == 'answered' and receipt['dispatch_state'] == 'queued'
    assert receipt['model_called'] is None


def test_dispatch_transaction_rolls_back_on_message_failure(env, monkeypatch):
    client, workspace, store = env
    automation = client.post('/v1/workspace/automations', headers=headers(), json={'name': '失败', 'prompt': 'hello', 'due_at': 1000.0}).json()
    client.post('/v1/workspace/automations/' + automation['id'] + '/enable', headers=headers())
    def fail(*args, **kwargs):
        raise RuntimeError('test failure')
    monkeypatch.setattr(workspace.assistant, 'add_message', fail)
    with pytest.raises(RuntimeError):
        workspace.dispatch_due(now=1001.0)
    assert workspace.automation_runs(ALICE, automation['id']) == []
    assert workspace.assistant.thread_of(ALICE.tenant_id, ALICE.subject_id) is None
    assert workspace.automation(ALICE, automation['id'])['enabled'] is True


def test_real_lifespan_timer_queues_once_without_any_model_process(tmp_path):
    app, workspace, _ = app_for(tmp_path, scheduler=True)
    with TestClient(app) as client:
        item = client.post('/v1/workspace/automations', headers=headers(), json={
            'name': '真实计时', 'prompt': '你好', 'due_at': time.time() + 0.15}).json()
        client.post('/v1/workspace/automations/' + item['id'] + '/enable', headers=headers())
        deadline = time.monotonic() + 4
        receipts = []
        while time.monotonic() < deadline:
            receipts = client.get('/v1/workspace/automations/' + item['id'] + '/runs', headers=headers()).json()['items']
            if receipts:
                break
            time.sleep(0.05)
        assert len(receipts) == 1 and receipts[0]['state'] == 'queued'
        assert app.state.workspace_scheduler_error is None
        assert workspace.automation(ALICE, item['id'])['enabled'] is False
