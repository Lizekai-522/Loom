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


def test_worktree_apply_reports_untracked_conflicts_and_checkout_receives_program(env):
    from pathlib import Path
    client = env['client']
    project = client.post('/v1/workspace/projects', headers=local('project-conflict'), json={'name': '冲突'}).json()
    created = client.post(f"/v1/workspace/projects/{project['id']}/worktrees", headers=local('wt-conflict'),
                          json={'run_id': 'run-conflict', 'title': '冲突'}).json()
    root = Path(project['path'])
    checkout = Path(created['path'])
    (root / 'note.txt').write_text('local untracked\n', encoding='utf-8')
    (checkout / 'note.txt').write_text('from worktree\n', encoding='utf-8')
    applied = client.post('/v1/workspace/worktrees/' + created['id'] + '/apply', headers=local('apply-conflict'))
    assert applied.status_code == 409, applied.text
    assert applied.json()['code'] == 'WORKTREE_CONFLICT'
    assert 'note.txt' in applied.json()['detail']
    assert (root / 'note.txt').read_text(encoding='utf-8') == 'local untracked\n'
    written = env['app'].state.workspace.write_program('a', 'run-conflict', 'print(1)\n')
    assert written['written'] is True
    assert (checkout / 'main.py').read_text(encoding='utf-8') == 'print(1)\n'
    diff = client.get('/v1/workspace/worktrees/' + created['id'] + '/diff', headers=local())
    assert 'print(1)' in diff.text
    removed = client.delete('/v1/workspace/worktrees/' + created['id'], headers=local('remove-conflict'))
    assert removed.status_code == 200 and removed.json()['removed'] is True


def test_project_files_are_listed_and_secret_names_stay_out(env):
    from pathlib import Path
    client = env['client']
    project = client.post('/v1/workspace/projects', headers=local('project-files'), json={'name': '文件'}).json()
    root = Path(project['path'])
    (root / 'src').mkdir()
    (root / 'src' / 'app.py').write_text('print("ok")\n', encoding='utf-8')
    (root / '.env').write_text('SECRET=1\n', encoding='utf-8')
    listed = client.get(f"/v1/workspace/projects/{project['id']}/files?q=app", headers=local())
    assert listed.status_code == 200
    paths = [item['path'] for item in listed.json()['items']]
    assert paths == ['src/app.py']
    body = client.get(f"/v1/workspace/projects/{project['id']}/file", headers=local(), params={'rel': 'src/app.py'})
    assert body.status_code == 200 and 'print' in body.json()['text']
    hidden = client.get(f"/v1/workspace/projects/{project['id']}/file", headers=local(), params={'rel': '.env'})
    assert hidden.status_code == 422
    sent = client.post('/v1/assistant/threads/' + client.get('/v1/assistant/thread', headers=local()).json()['thread']['id'] + '/messages',
                       headers=local('mention-file'), json={'text': '看这个文件', 'project_id': project['id'], 'mentions': ['src/app.py']})
    assert sent.status_code == 201, sent.text
    assert 'print' in sent.json()['meta']['mentions'][0]['text']
    assert sent.json()['meta']['project_path'] == project['path']


def test_png_rgb_round_trips_pixels_without_pillow():
    import struct
    import zlib
    from loom.computer_api import _png_rgb_zlib
    bgra = bytes([255, 0, 0, 255, 0, 255, 0, 128])
    data = _png_rgb_zlib(2, 1, bgra)
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    assert data[12:16] == b'IHDR'
    width, height = struct.unpack('!II', data[16:24])
    assert (width, height) == (2, 1)
    pos, idat = 8, b''
    while pos < len(data):
        length = struct.unpack('!I', data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b'IDAT':
            idat += chunk
        elif kind == b'IEND':
            break
    raw = zlib.decompress(idat)
    assert raw[0] == 0 and raw[1:4] == bytes([0, 0, 255]) and raw[4:7] == bytes([0, 255, 0])


def test_console_keeps_green_tokens_and_the_audit_wiring():
    root = __import__('pathlib').Path('src/loom/web')
    theme = (root / 'verilo-theme.css').read_text(encoding='utf-8')
    assistant = (root / 'assistant.html').read_text(encoding='utf-8')
    workbench = (root / 'workbench.js').read_text(encoding='utf-8')
    ui = (root / 'workspace-ui.js').read_text(encoding='utf-8')
    script = (root / 'assistant.js').read_text(encoding='utf-8')
    chat = (root / 'chat.html').read_text(encoding='utf-8')
    settings = (root / 'settings.html').read_text(encoding='utf-8')
    assert '#245b35' in theme and 'verilo-theme.css' in assistant
    assert '/console/admin#section-model' in workbench and '/console/settings#models' in ui
    assert 'terminalWorktreeId' in ui and 'pre.wm-diff' in workbench
    assert 'encodeURIComponent(item.id)' in workbench
    assert 'waitingCode.length' in script and 'ok !== false' in script
    assert '今天要构建什么' not in chat and 'class="muted"' not in settings
    assert 'id="conversation"' in chat and 'id="prompt"' in chat and 'data-inspector="dag"' in assistant
    assert 'id="session-rail"' in assistant and 'id="rightbar"' in assistant
    assert 'id="account-trigger"' in assistant and 'id="account-name"' in assistant and 'id="account-avatar"' in assistant
    assert 'href="/console/settings"' in assistant and 'href="/console/settings#appearance"' in assistant
    assert 'href="/console/settings#general"' in assistant and '退出登录' in assistant
    assert 'href="/console/settings#usage"' in assistant and 'href="/console/settings#guide"' in assistant
    assert 'id="account-usage"' in assistant and 'id="account-signout"' in assistant and 'verilo-account-danger" hidden' in assistant
    assert 'href="/console/tasks"' in assistant
    tasks = (root / 'tasks.html').read_text(encoding='utf-8')
    assert 'data-filter="active"' in tasks and 'data-filter="paused"' in tasks and 'data-filter="ended"' in tasks and 'data-filter="all"' in tasks
    assert 'id="task-search"' in tasks and 'id="task-detail"' in tasks and '新建任务' in tasks
    assert '#1f6feb' not in (root / 'settings.css').read_text(encoding='utf-8')
    assert '#245b35' in theme
    assert 'id="inspector-resize"' in assistant and 'data-close-inspector="files"' in assistant
    assert 'id="rightbar" class="rightbar" hidden' in assistant and 'id="inspector" class="inspector" hidden' in assistant
    assert 'Premium' not in assistant and 'Super' not in assistant and 'Grok' not in assistant and '添加账户' not in assistant
    assert '新建工作树' not in workbench and '还没有项目。' in workbench
    assert 'verilo.inspector.width' in workbench and 'verilo.inspector.memory' in workbench
    assert '当前没有任务或会话在使用检查器。' in workbench and 'spent_today_microusd' not in workbench
    assert 'account-name' in script and 'state.me.subject_id' in script and 'display_name' in script
    assert 'spent_today_microusd' in script and 'account-signout-sep' in script
    assert 'authentication !== "public-session"' in script
    css = (root / 'assistant.css').read_text(encoding='utf-8')
    assert 'bottom:calc(100% + 4px)' in css and '#245b35' in theme
    assert '--inspector-width' in css and 'verilo-account-usage-bar' in css
    assert 'zekai' not in assistant.lower() and 'zinc' not in css and 'zinc' not in theme
    chrome = (root / 'harness-chrome.css').read_text(encoding='utf-8')
    assert 'Copyright (c) 2026 DeepSeek' in chrome and 'Permission is hereby granted' in chrome
    assert 'height:38px' in chrome and 'border-radius:28px' in chrome and 'width:32px' in chrome
    assert 'font-size:20px' in chrome and 'flex:0 0 47%' in chrome
    assert 'harness-chrome.css' in assistant and 'harness-chrome.css' in tasks and 'harness-chrome.css' in chat
    assert 'id="account-trigger"' in chat and 'id="account-signout"' in chat and 'verilo-account-danger" hidden' in chat
    assert '编程会话' in chat and 'DeepSeek' not in assistant and 'DeepSeek' not in chat and 'DeepSeek' not in tasks
    assert 'verilo.sidebar.collapsed' in (root / 'frame.js').read_text(encoding='utf-8')
    assert 'verilo.sidebar.collapsed' in (root / 'harness-ui.js').read_text(encoding='utf-8')
    preferences = (root / 'preferences.html').read_text(encoding='utf-8')
    admin = (root / 'settings.html').read_text(encoding='utf-8')
    assert 'harness-chrome.css' in preferences and 'class="admin-app"' in admin and 'id="section-model"' in admin


def test_settings_report_today_spend_against_the_daily_budget(env):
    client = env['client']
    fresh = client.get('/v1/assistant/settings', headers=local()).json()
    assert fresh['spent_today_microusd'] == 0
    assert fresh['daily_budget_microusd'] == 1_000_000
    data = env['data']
    thread = data.thread_for('a', 'alice')
    run = env['store'].create_run('a', 'spend-today', acceptance=['schema.valid'])
    with env['store'].transaction() as db:
        db.execute('UPDATE runs SET spent=? WHERE tenant=? AND id=?', (500, 'a', run['run_id']))
    data.record_job('a', run['run_id'], purpose='assistant-reply', owner=thread['id'], proactive=False)
    body = client.get('/v1/assistant/settings', headers=local()).json()
    assert body['spent_today_microusd'] == 500
    assert body['daily_budget_microusd'] == 1_000_000
