"""Actual message/runtime option semantics with HTTP model test doubles only."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from loom.model_gateway import ModelGateway, ModelRoute
from loom.model_pricing import FLASH, PRO
from monsoon.runtime import ACTION_MARK

from tests.test_assistant import env, local, sse, runtime, FakeWeb


@pytest.fixture
def configured(env):
    path = Path(env['store'].path).parent / 'workspace-options-models.json'
    path.write_text(json.dumps(dict(routes=[
        dict(alias=alias, provider='deepseek', model=alias, key_env='DEEPSEEK_API_KEY')
        for alias in ('deepseek-flash', 'deepseek-v4-pro')])), encoding='utf-8')
    workspace = env['app'].state.workspace
    workspace.executor_config = SimpleNamespace(model_config=path, source_path=None)
    workspace.config_path = None
    env['workspace'] = workspace
    env['identity'] = SimpleNamespace(tenant_id='a', subject_id='alice')
    return env


def send(configured, text, *, key='options-message', **options):
    thread = configured['client'].get('/v1/assistant/thread', headers=local()).json()['thread']
    result = configured['client'].post('/v1/assistant/threads/' + thread['id'] + '/messages',
        headers=local(key), json=dict(text=text, **options))
    return thread, result


class RoutedModel:
    """Echo the requested model identity in mock provider response envelopes."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        content = self.replies.pop(0)
        if body['stream']:
            return httpx.Response(200, text=sse(content, model=body['model']),
                headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json={'id': 'options-1', 'object': 'chat.completion', 'created': 1,
            'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 50, 'completion_tokens': 30, 'total_tokens': 80}})

    def factory(self):
        return lambda: ModelGateway([
            ModelRoute(alias, model=alias) for alias in ('deepseek-flash', 'deepseek-v4-pro')],
            transport=httpx.MockTransport(self.handler), environ={'DEEPSEEK_API_KEY': 'workspace-options-fixture-only'})


def test_message_options_are_validated_and_foreign_project_is_hidden(configured):
    workspace = configured['workspace']
    foreign = workspace.add_project(SimpleNamespace(tenant_id='a', subject_id='bob'), 'Bob项目')
    _, bad_model = send(configured, '你好', model_alias='not-configured')
    assert bad_model.status_code == 422
    _, foreign_project = send(configured, '你好', key='foreign', project_id=foreign['id'])
    assert foreign_project.status_code == 404
    _, invalid_mode = send(configured, '你好', key='invalid-mode', confirmation_mode='auto')
    assert invalid_mode.status_code == 422
    thread = configured['data'].thread_of('a', 'alice')
    assert configured['data'].messages('a', thread['id']) == []


def test_message_snapshot_and_identical_retry_are_idempotent(configured):
    workspace, identity = configured['workspace'], configured['identity']
    project = workspace.add_project(identity, 'Alice项目')
    workspace.update_preferences(identity, dict(preferred_model='deepseek-v4-pro',
        selected_project_id=project['id'], confirmation_mode='read_only'))
    thread, first = send(configured, '解释这段代码')
    _, again = send(configured, '解释这段代码')
    assert first.status_code == 201 and again.status_code == 201
    assert first.json() == again.json()
    assert first.json()['meta']['model_alias'] == 'deepseek-v4-pro'
    assert first.json()['meta']['project_id'] == project['id']
    assert first.json()['meta']['confirmation_mode'] == 'read_only'
    assert len(configured['data'].messages('a', thread['id'])) == 1
    _, conflict = send(configured, '解释这段代码', confirmation_mode='ask')
    assert conflict.status_code == 409


def test_read_only_enforces_no_coding_todo_or_research_side_effects(configured):
    actions = [dict(type='coding', title='写程序', prompt='读取JSON加一'),
        dict(type='research', title='调研', question='查一下Python'),
        dict(type='todo', text='记一下', research=True)]
    thread, response = send(configured, '请给出建议', confirmation_mode='read_only')
    assert response.status_code == 201
    model = RoutedModel(['下面是建议。\n' + ACTION_MARK + json.dumps(actions, ensure_ascii=False)])
    assert asyncio.run(runtime(configured, model).chat_once()) == 'replied'
    assert configured['data'].tasks('a', thread_id=thread['id']) == []
    assert configured['data'].todos('a', thread_id=thread['id']) == []
    assert configured['data'].approvals('a') == []
    reply = configured['data'].messages('a', thread['id'])[-1]
    assert reply['meta']['actions'] == [] and '仅对话模式' in reply['text']
    assert '不创建任务' in model.requests[0]['messages'][0]['content']


def test_retry_replays_frozen_options_after_preferences_change(configured):
    workspace, identity = configured['workspace'], configured['identity']
    workspace.update_preferences(identity, dict(preferred_model='deepseek-flash', confirmation_mode='ask'))
    thread, first = send(configured, '解释列表', key='lost-response')
    assert first.status_code == 201
    workspace.update_preferences(identity, dict(preferred_model='deepseek-v4-pro', confirmation_mode='read_only'))
    _, retried = send(configured, '解释列表', key='lost-response')
    assert retried.status_code == 201
    assert retried.json() == first.json()
    assert len(configured['data'].messages('a', thread['id'])) == 1


def test_receipt_replays_after_selected_project_and_model_are_removed(configured):
    workspace, identity = configured['workspace'], configured['identity']
    project = workspace.add_project(identity, '临时项目')
    options = dict(project_id=project['id'], model_alias='deepseek-v4-pro')
    thread, first = send(configured, '解释项目', key='removed-options', **options)
    assert first.status_code == 201
    workspace.delete_project(identity, project['id'])
    workspace.executor_config.model_config.write_text('{"routes": []}', encoding='utf-8')
    _, replay = send(configured, '解释项目', key='removed-options', **options)
    assert replay.status_code == 201 and replay.json() == first.json()
    _, new_request = send(configured, '解释项目', key='new-key-after-removal', **options)
    assert new_request.status_code in (404, 422)
    assert len(configured['data'].messages('a', thread['id'])) == 1


def test_selected_model_uses_its_tariff_and_does_not_change_research_default(configured):
    actions = [dict(type='research', title='调研', question='Python是什么')]
    thread, result = send(configured, '研究一下Python', model_alias='deepseek-v4-pro', confirmation_mode='ask')
    assert result.status_code == 201
    model = RoutedModel(['我来查。\n' + ACTION_MARK + json.dumps(actions, ensure_ascii=False),
                         json.dumps({'queries': ['Python']})])
    rt = runtime(configured, model, FakeWeb())
    assert asyncio.run(rt.chat_once()) == 'replied'
    reply = [m for m in configured['data'].messages('a', thread['id']) if m['role'] == 'assistant' and m['kind'] == 'text'][-1]
    assert model.requests[0]['model'] == 'deepseek-v4-pro'
    assert reply['meta']['cost_microusd'] == PRO.cost(80, 20)
    assert reply['meta']['cost_microusd'] != FLASH.cost(80, 20)
    ledger = configured['store'].snapshot('a', reply['meta']['run_id'])
    assert ledger['spent'] == PRO.cost(80, 20) and ledger['reserved'] == 0
    assert rt.route_alias == 'deepseek-flash'
    assert asyncio.run(rt.work_once()) is not None
    assert model.requests[1]['model'] == 'deepseek-flash'
    assert len(configured['data'].tasks('a', thread_id=thread['id'])) == 1


def test_automation_metadata_reaches_the_mock_gateway_and_honors_read_only(configured):
    workspace, identity = configured['workspace'], configured['identity']
    workspace.update_preferences(identity, dict(preferred_model='deepseek-v4-pro', confirmation_mode='read_only'))
    automation = workspace.add_automation(identity, dict(name='解释', prompt='解释Python列表'))
    receipt = workspace.run_automation(identity, automation['id'], 'options-automation')
    model = RoutedModel(['可以使用列表。\n' + ACTION_MARK + json.dumps([dict(type='todo', text='不应创建')])])
    assert asyncio.run(runtime(configured, model).chat_once()) == 'replied'
    assert model.requests[0]['model'] == 'deepseek-v4-pro'
    thread = configured['data'].thread_of('a', 'alice')
    messages = configured['data'].messages('a', thread['id'])
    assert messages[0]['id'] == receipt['message_id']
    assert messages[0]['meta']['automation_run_id'] == receipt['id']
    assert messages[0]['meta']['confirmation_mode'] == 'read_only'
    assert configured['data'].todos('a', thread_id=thread['id']) == []
