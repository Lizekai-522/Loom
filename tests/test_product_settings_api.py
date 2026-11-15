"""Personal product settings: isolation, durable templates and redacted exports."""
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.product_settings_api import UserSettings
from loom.store import Store


ALICE = LocalIdentity('a', 'alice', frozenset({'user'}), 'product-settings-alice-test-only-00000')
BOB = LocalIdentity('a', 'bob', frozenset({'user'}), 'product-settings-bob-test-only-0000000')
OTHER = LocalIdentity('b', 'carol', frozenset({'user'}), 'product-settings-carol-test-only-00000')
WORKER = LocalIdentity('a', 'worker', frozenset({'worker'}), 'product-settings-worker-test-only-0000')


def headers(identity=ALICE, key=None):
    return {'Authorization': 'Bearer ' + identity.token, 'Idempotency-Key': key or uuid4().hex}


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / 'product.db')
    app = create_app(store, {}, identities=(ALICE, BOB, OTHER, WORKER))
    client = TestClient(app, base_url='http://127.0.0.1:8796', client=('127.0.0.1', 50000))
    yield store, app, client
    client.close()


def add(client, kind='memory', identity=ALICE, key=None, **changes):
    body = dict(title='偏好', content='请用中文回答', enabled=True, config={})
    body.update(changes)
    r = client.post('/v1/product/entries/' + kind, headers=headers(identity, key), json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_memory_is_subject_scoped_and_durable(env):
    store, app, client = env
    item = add(client)
    assert UserSettings(store).memory('a', 'alice') == ['请用中文回答']
    assert UserSettings(store).memory('a', 'bob') == []
    for who in [BOB, OTHER]:
        assert client.get('/v1/product/entries/memory', headers=headers(who)).json()['items'] == []
        r = client.patch('/v1/product/entries/memory/' + item['id'], headers=headers(who),
                         json=dict(expected_revision=1, enabled=False))
        assert r.status_code == 404
    r = client.patch('/v1/product/entries/memory/' + item['id'], headers=headers(),
                     json=dict(expected_revision=1, enabled=False))
    assert r.status_code == 200
    assert UserSettings(store).memory('a', 'alice') == []


def test_entry_idempotency_revision_and_delete(env):
    store, app, client = env
    item = add(client, key='one-entry')
    assert add(client, key='one-entry')['id'] == item['id']
    url = '/v1/product/entries/memory/' + item['id']
    assert client.patch(url, headers=headers(), json=dict(expected_revision=2, content='新偏好')).status_code == 409
    assert client.delete(url + '?expected_revision=2', headers=headers()).status_code == 409
    assert client.delete(url + '?expected_revision=1', headers=headers()).status_code == 200
    assert client.get('/v1/product/entries/memory', headers=headers()).json()['items'] == []


def test_personal_fields_are_redacted_before_store_and_export(env):
    store, app, client = env
    secret = 'sk-' + 'x' * 32
    item = add(client, content='凭据 ' + secret)
    assert secret not in json.dumps(item)
    with store.connect() as db:
        saved = db.execute('SELECT content FROM product_entries').fetchone()[0]
        receipts = [row['result'] for row in db.execute('SELECT result FROM receipts')]
    assert secret not in saved
    assert all(secret not in result for result in receipts)


def test_only_human_users_can_manage_settings(env):
    store, app, client = env
    for path in ['/v1/product/capabilities', '/v1/product/usage', '/v1/product/archives', '/v1/product/entries/memory']:
        assert client.get(path, headers=headers(WORKER)).status_code == 403
        assert client.get(path).status_code == 401


def test_template_is_explicit_idempotent_user_message_not_shell(env):
    store, app, client = env
    app.state.workspace.update_preferences(ALICE, dict(confirmation_mode='read_only'))
    item = add(client, kind='commands', content='总结以下文字')
    url = '/v1/product/entries/commands/' + item['id'] + '/run'
    r = client.post(url, headers=headers(key='send-once'), json={'input': '会议记录'})
    assert r.status_code == 201, r.text
    again = client.post(url, headers=headers(key='send-once'), json={'input': '会议记录'})
    assert again.json()['id'] == r.json()['id']
    assert r.json()['text'] == '总结以下文字\n\n会议记录'
    assert r.json()['status'] == 'queued'
    assert r.json()['author'] == 'subject:alice'
    assert r.json()['meta']['confirmation_mode'] == 'read_only'
    assert client.post(url, headers=headers(BOB), json={'input': ''}).status_code == 404
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM ms_messages').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0


def test_hooks_reject_shell_actions_and_report_unconnected_capabilities(env):
    store, app, client = env
    r = client.post('/v1/product/entries/hooks', headers=headers(),
                    json=dict(title='任意命令', content='', config=dict(event='reply.completed', action='shell', command='whoami')))
    assert r.status_code == 422
    item = add(client, kind='hooks', content='', config=dict(event='reply.completed', action='browser.notification'))
    assert item['config']['action'] == 'browser.notification'
    cap = client.get('/v1/product/capabilities', headers=headers()).json()
    assert cap['browser']['available'] is False
    assert cap['remote_devices']['available'] is False
    assert cap['subagents']['spawning_available'] is False


def test_archive_exports_only_own_complete_conversation_and_survives_original_edit(env):
    store, app, client = env
    assistant = app.state.assistant
    # Assistant install returns a service; obtain the underlying store directly.
    from monsoon.store import AssistantStore
    data = AssistantStore(store)
    for who, text in [(ALICE, 'Alice private'), (BOB, 'Bob private')]:
        thread = data.thread_for(who.tenant_id, who.subject_id)
        data.add_message(who.tenant_id, thread['id'], role='user', kind='text', body=text,
                         status='done', author='subject:' + who.subject_id)
    r = client.post('/v1/product/archives', headers=headers(key='archive-once'), json={'title': '测试快照'})
    assert r.status_code == 201, r.text
    url = '/v1/product/archives/' + r.json()['id']
    export = client.get(url, headers=headers())
    assert export.status_code == 200
    assert [m['text'] for m in export.json()['messages']] == ['Alice private']
    assert client.get(url, headers=headers(BOB)).status_code == 404
    assert client.delete(url, headers=headers(BOB)).status_code == 404
    assert client.delete(url, headers=headers()).status_code == 200
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM ms_messages').fetchone()[0] == 2


def test_usage_sums_only_this_persons_persisted_jobs(env):
    store, app, client = env
    from monsoon.store import AssistantStore
    data = AssistantStore(store)
    alice_thread = data.thread_for('a', 'alice')
    bob_thread = data.thread_for('a', 'bob')
    for who, thread, spent in [('alice', alice_thread, 120), ('bob', bob_thread, 999)]:
        run = store.create_run('a', who + '-run', acceptance=['schema.valid'])
        run_id = run['run_id']
        with store.transaction() as db:
            db.execute('UPDATE runs SET spent=?,tokens_spent=? WHERE tenant=? AND id=?', (spent, spent * 2, 'a', run_id))
        data.record_job('a', run_id, purpose='assistant-reply', owner=thread['id'], proactive=False)
    result = client.get('/v1/product/usage', headers=headers()).json()
    assert result['spent_microusd'] == 120
    assert result['tokens'] == 240
    assert result['scope'] == 'current_user'
