"""Full-duplex interrupt, observe automations, worktrees, browser pick, mounted computer."""
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.model_gateway import ModelGateway, ModelRoute
from loom.store import Store
from monsoon.runtime import AssistantRuntime
from monsoon.store import AssistantStore
from monsoon.web import extract_elements


USER = LocalIdentity('a', 'alice', frozenset({'user'}), 'harness-parity-human-user-token-000000')
OPERATOR = LocalIdentity('a', 'ms1-operator', frozenset({'operator'}), 'harness-parity-operator-token-000000')


def local(key=None):
    headers = {'X-Loom-Console': 'local'}
    if key:
        headers['Idempotency-Key'] = key or uuid4().hex
    return headers


def sse(content):
    frames = [
        {'id': 'stream-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'deepseek-flash',
         'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]},
        {'id': 'stream-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'deepseek-flash',
         'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
        {'id': 'stream-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'deepseek-flash', 'choices': [],
         'usage': {'prompt_tokens': 11, 'completion_tokens': 4, 'total_tokens': 15}},
    ]
    return ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames) + 'data: [DONE]\n\n'


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / 'parity.db')
    app = create_app(store, {}, identities=(USER, OPERATOR), local_console=True)
    client = TestClient(app, base_url='http://127.0.0.1:8796', client=('127.0.0.1', 50000))
    yield dict(store=store, app=app, client=client, data=AssistantStore(store))
    client.close()


def test_interrupt_stops_streaming_reply_and_queues_followup(env):
    client, data = env['client'], env['data']
    thread = client.get('/v1/assistant/thread', headers=local()).json()['thread']
    streaming = data.add_message('a', thread['id'], role='assistant', kind='text', body='生成到一半', status='streaming', author='assistant')
    result = client.post(f"/v1/assistant/threads/{thread['id']}/interrupt", headers=local('interrupt-1'),
                         json={'reason': 'user_interrupt', 'followup_text': '改成三句话'})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['interrupted'] == [streaming['id']]
    assert body['followup']['text'] == '改成三句话'
    assert body['followup']['meta']['trigger'] == 'followup'
    assert data.message('a', streaming['id'])['status'] == 'done'
    assert data.message('a', streaming['id'])['meta']['interrupted'] is True
    events = client.get(f"/v1/assistant/threads/{thread['id']}/events", headers=local()).json()['events']
    assert any(event['type'] == 'turn.interrupted' for event in events)


def test_barge_in_message_marks_the_open_reply(env):
    client, data = env['client'], env['data']
    thread = client.get('/v1/assistant/thread', headers=local()).json()['thread']
    streaming = data.add_message('a', thread['id'], role='assistant', kind='text', body='旧回复', status='streaming', author='assistant')
    sent = client.post(f"/v1/assistant/threads/{thread['id']}/messages", headers=local('barge-1'),
                       json={'text': '停，听这个', 'barge_in': True})
    assert sent.status_code == 201, sent.text
    assert streaming['id'] in sent.json()['meta']['barge_in_interrupted']
    assert data.message('a', streaming['id'])['meta']['interrupted'] is True


def test_runtime_discards_a_reply_interrupted_during_the_stream(env):
    store = env['store']
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen['stream'] = body['stream']
        data = AssistantStore(store)
        rows = store.connect()
        try:
            row = rows.execute("SELECT id, thread_id FROM ms_messages WHERE status='streaming'").fetchone()
            data.interrupt_streaming(rows, 'a', row['thread_id'], actor='subject:alice', reason='user_interrupt')
            rows.commit()
        finally:
            rows.close()
        return httpx.Response(200, text=sse('这句不该留下'), headers={'content-type': 'text/event-stream'})

    gateway = lambda: ModelGateway([ModelRoute('deepseek-flash')], transport=httpx.MockTransport(handler),
                                   environ={'DEEPSEEK_API_KEY': 'harness-parity-fixture-key'})
    runtime = AssistantRuntime(store, gateway_factory=gateway)
    thread = env['data'].thread_for('a', 'alice')
    env['data'].add_message('a', thread['id'], role='user', kind='text', body='你好', status='queued', author='subject:alice')
    import asyncio
    assert asyncio.run(runtime.chat_once()) == 'interrupted'
    reply = [item for item in env['data'].messages('a', thread['id']) if item['role'] == 'assistant'][-1]
    assert reply['meta']['interrupted'] is True
    assert '不该留下' not in reply['text']
    assert seen['stream'] is True


def test_enabled_skill_is_data_in_the_prompt_and_slash_command_expands(env):
    from loom.product_settings_api import UserSettings
    settings = UserSettings(env['store'])
    identity = type('I', (), {'tenant_id': 'a', 'subject_id': 'alice'})()
    settings.write(identity, 'skill-1', 'skills', dict(title='格式', content='用短句', enabled=True, config={}))
    settings.write(identity, 'command-1', 'commands', dict(title='总结', content='请用三点总结', enabled=True, config={}))
    captured = {}

    def handler(request):
        captured['body'] = json.loads(request.content)
        return httpx.Response(200, text=sse('好的。'), headers={'content-type': 'text/event-stream'})

    gateway = lambda: ModelGateway([ModelRoute('deepseek-flash')], transport=httpx.MockTransport(handler),
                                   environ={'DEEPSEEK_API_KEY': 'harness-parity-fixture-key'})
    runtime = AssistantRuntime(env['store'], gateway_factory=gateway)
    thread = env['data'].thread_for('a', 'alice')
    env['data'].add_message('a', thread['id'], role='user', kind='text', body='/总结 这段', status='queued', author='subject:alice')
    import asyncio
    assert asyncio.run(runtime.chat_once()) == 'replied'
    prompt = captured['body']['messages'][-1]['content']
    system = captured['body']['messages'][0]['content']
    assert prompt.startswith('请用三点总结')
    assert '用短句' in system


def test_observe_automation_does_not_queue_a_model_message(env):
    client = env['client']
    created = client.post('/v1/workspace/projects', headers=local('project-1'), json={'name': '演示'}).json()
    automation = client.post('/v1/workspace/automations', headers=local('auto-1'), json={
        'name': '观察', 'prompt': '不要调用模型', 'due_at': 1000.0, 'project_id': created['id']}).json()
    assert automation['mode'] == 'observe' and automation['enabled'] is False
    assert client.post('/v1/workspace/automations/' + automation['id'] + '/enable', headers=local()).status_code == 200
    receipts = env['app'].state.workspace.dispatch_due(now=1001.0)
    assert receipts[0]['state'] == 'would_dispatch' and receipts[0]['model_called'] is False
    assert env['data'].thread_of('a', 'alice') is None or env['data'].messages('a', env['data'].thread_of('a', 'alice')['id']) == []


def test_worktree_diff_and_apply_stay_out_of_the_project_until_requested(env, tmp_path):
    client = env['client']
    project = client.post('/v1/workspace/projects', headers=local('project-wt'), json={'name': '仓库'}).json()
    created = client.post(f"/v1/workspace/projects/{project['id']}/worktrees", headers=local(),
                          json={'run_id': 'run-1', 'title': '隔离'}).json()
    assert created['branch'] == 'wt/run-1'
    path = __import__('pathlib').Path(created['path'])
    (path / 'note.txt').write_text('hello worktree\n', encoding='utf-8')
    diff = client.get('/v1/workspace/worktrees/' + created['id'] + '/diff', headers=local())
    assert diff.status_code == 200 and 'hello worktree' in diff.text
    assert not (__import__('pathlib').Path(project['path']) / 'note.txt').exists()
    applied = client.post('/v1/workspace/worktrees/' + created['id'] + '/apply', headers=local())
    assert applied.status_code == 200 and applied.json()['applied'] is True
    assert (__import__('pathlib').Path(project['path']) / 'note.txt').read_text(encoding='utf-8') == 'hello worktree\n'
    again = client.post(f"/v1/workspace/projects/{project['id']}/worktrees", headers=local('again'),
                        json={'run_id': 'run-1', 'title': '隔离'})
    assert again.status_code == 201 and again.json()['id'] == created['id']


def test_worktree_slot_keeps_one_extra_human_seat(env):
    workspace = env['app'].state.workspace
    workspace.worker_slots = 1
    client = env['client']
    project = client.post('/v1/workspace/projects', headers=local('project-slot'), json={'name': '座位'}).json()
    identity = type('I', (), {'tenant_id': 'a', 'subject_id': 'alice'})()
    first = workspace.ensure_worktree(identity, project['id'], 'worker-1', title='w', is_human_request=False)
    with pytest.raises(Exception):
        workspace.ensure_worktree(identity, project['id'], 'worker-2', title='w', is_human_request=False)
    human = workspace.ensure_worktree(identity, project['id'], 'person-1', title='h', is_human_request=True)
    assert first['id'] != human['id'] and human['by_person'] is True


def test_browser_pick_uses_the_guarded_outline_and_computer_route_is_mounted(env):
    html = '<html><body><button id="go">开始</button><p>说明</p></body></html>'
    assert extract_elements(html)[0]['selector'] == '#go'
    env['app'].state.browser_inspector.fetch = lambda url: dict(
        url=url, title='示例', text='说明', html=html, renderer='html-extract')
    client = env['client']
    preview = client.post('/v1/assistant/browser/preview', headers=local(), json={'url': 'https://example.com/page'})
    assert preview.status_code == 200, preview.text
    chosen = client.post('/v1/assistant/browser/pick', headers=local(),
                         json={'preview_id': preview.json()['id'], 'selector': '#go'})
    assert chosen.status_code == 200 and chosen.json()['tag'] == 'button' and '开始' in chosen.json()['text']
    status = client.get('/v1/computer/status', headers=local())
    assert status.status_code == 200
    assert status.json()['scope'] == 'local_manual_only'
    assert 'available' in status.json()
    assert client.get('/v1/computer/status').status_code == 401
