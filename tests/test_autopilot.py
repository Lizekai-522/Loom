"""Automatic sessions: delegated audited steps; model HTTP and sandbox are test doubles."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.autopilot import SessionService, split_cases
from loom.auth import LocalIdentity, Principal
from loom.executor_config import LocalExecutorConfig, load_executor_config
from loom.local_executor import LocalExecutor
from loom.model_gateway import ModelGateway, ModelRoute
from loom.store import InvalidInput, Store
from tests.test_approved_execution import OP, WORKER
from tests.test_repair import SequenceSandbox

ROOT = Path(__file__).resolve().parents[1]
GOOD = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'
REQUIREMENTS = dict(schema_version='loom.requirements/v1', summary='读取 value 输出 value+1',
                    items=[dict(id='add-one', text='answer 等于 value 加一')], uncertainties=[])
CHECKS = dict(schema_version='loom.public-checks/v1', visibility='development', cases=[
    dict(name='one', input={'value': 1}, expected={'answer': 2}),
    dict(name='zero', input={'value': 0}, expected={'answer': 1}),
    dict(name='negative', input={'value': -5}, expected={'answer': -4}),
    dict(name='large', input={'value': 99}, expected={'answer': 100})])
PRINCIPAL = Principal(OP.tenant_id, OP.subject_id, OP.roles)


def gateway(bodies, requests):
    """Factory: the executor opens and closes one gateway per request."""
    pending = list(bodies)
    return lambda *args, **kwargs: _gateway(pending, requests)


def _gateway(pending, requests):

    def handler(request):
        requests.append(json.loads(request.content))
        body = pending.pop(0)
        return httpx.Response(200, json={'id': 'autopilot-fixture', 'object': 'chat.completion', 'created': 1792700000,
            'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': json.dumps(body)}}],
            'usage': {'prompt_tokens': 25, 'completion_tokens': 10, 'total_tokens': 35}})
    return ModelGateway([ModelRoute('deepseek-flash')], transport=httpx.MockTransport(handler),
                        environ={'DEEPSEEK_API_KEY': 'autopilot-public-fixture-only'})


@pytest.fixture
def case(tmp_path):
    store = Store(tmp_path / 'auto.db')
    model = tmp_path / 'model.json'
    model.write_bytes((ROOT / 'configs/models.example.json').read_bytes())
    auto = tmp_path / 'private-auto-acceptance'
    config = LocalExecutorConfig('test-local', tmp_path / 'private-identities', tmp_path / 'private-model-key', model,
                                 'sha256:' + '1' * 64, {}, auto_acceptance_dir=auto)
    return dict(store=store, config=config, auto=auto, tmp=tmp_path)


def executor(c):
    with patch('loom.local_executor.load_identities', return_value=(OP, WORKER)):
        return LocalExecutor(c['store'], c['config'])


def human_approves_waiting_plans(c):
    """The person clicks "批准并开始编写" for every session waiting on them."""
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER), initialize=False)
    approved = 0
    for item in service.list('a'):
        for session_id in service.get('a', item['session_id'])['thread']:
            if service.get('a', session_id, include_source=False)['state'] == 'awaiting_approval':
                service.approve(PRINCIPAL, session_id, 'human-approve:' + session_id)
                approved += 1
    return approved


def run_until_idle(c, bodies, outcomes, *, limit=20, approve=True):
    requests, sandbox = [], SequenceSandbox(outcomes)
    model = gateway(bodies, requests)
    worker = executor(c)
    with patch('loom.local_executor.load_project_secrets', return_value={'DEEPSEEK_API_KEY': 'fixture-only'}), \
         patch('loom.local_executor.load_model_config', return_value=([ModelRoute('deepseek-flash')], {})), \
         patch('loom.local_executor.ModelGateway', side_effect=model), \
         patch('loom.local_executor.DockerSandbox', return_value=sandbox):
        idle = 0
        for _ in range(limit):
            result = asyncio.run(worker.drain_one())
            idle = idle + 1 if result is None else 0
            if idle >= 2 and approve and human_approves_waiting_plans(c):
                idle = 0
            if idle >= 3:
                break
    return requests, sandbox, worker


def test_split_is_deterministic_never_empty_and_never_all():
    assert split_cases(['a', 'b']) == (['a'], ['b'])
    assert split_cases(['a', 'b', 'c', 'd', 'e', 'f']) == (['a', 'b', 'd', 'e'], ['c', 'f'])
    with pytest.raises(InvalidInput):
        split_cases(['only'])


def test_one_sentence_runs_to_delivery_without_manual_steps(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'first', prompt='写一个程序：读取 {"value":整数}，输出 {"answer":value+1}')
    assert created['state'] == 'running' and created['steps'][0]['status'] == 'queued'
    # Plan approval happened under the opting-in operator, with an audit record.
    events = c['store'].events('a', created['current_run_id'])
    assert any(e['type'] == 'workflow.plan_approved' and e['actor'] == 'subject:operator' for e in events)
    assert any(e['type'] == 'autopilot.session_created' for e in events)

    requests, sandbox, worker = run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    view = service.get('a', created['session_id'])
    assert view['state'] == 'delivered', view['attention']
    assert [s['status'] for s in view['steps'][:3]] == ['submitted', 'submitted', 'submitted']
    assert view['steps'][3]['status'] == 'not_configured'
    assert view['steps'][1]['split'] == dict(development=3, heldout=1, origin='tester-model-heldout', heldout_names=['negative'], user_examples=0)
    assert view['steps'][2]['source'] == GOOD
    assert [a['phase'] for a in view['steps'][2]['attempts']] == ['development', 'acceptance']
    assert view['independently_verified'] is False and view['counts_as_success'] is False
    assert len(requests) == 3

    # Held-out case never reached the developer prompt; development cases did.
    developer_prompt = '\n'.join(m['content'] for m in requests[2]['messages'])
    assert 'negative' not in developer_prompt and 'zero' in developer_prompt and '-4' not in developer_prompt
    held_file = next(c['auto'].iterdir())
    assert json.loads(held_file.read_text(encoding='utf-8'))['tests'][0]['name'] == 'negative'
    # Acceptance ran on exactly the held-out case, development on the other three.
    assert [len(tests) for _, tests in sandbox.inputs] == [3, 1]
    # Idempotent: further ticks never repeat a paid step.
    again, _, _ = run_until_idle(c, [], [])
    assert again == []


def test_acceptance_failure_stops_and_asks_a_person(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'fail', prompt='写一个加一程序')
    run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, False])
    view = service.get('a', created['session_id'])
    assert view['state'] == 'needs_attention'
    assert view['attention']['code'] == 'ACCEPTANCE_FAILED'
    # Stopped sessions do not act again until a person resumes them.
    assert run_until_idle(c, [], [])[0] == []
    resumed = service.resume(PRINCIPAL, created['session_id'], 'resume-1', reason='已查看证据')
    assert resumed['state'] == 'running'


def test_too_few_cases_stops_before_any_developer_call(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'few', prompt='写一个加一程序')
    few = {**CHECKS, 'cases': CHECKS['cases'][:1]}
    requests, _, _ = run_until_idle(c, [REQUIREMENTS, few], [])
    view = service.get('a', created['session_id'])
    assert view['attention']['code'] == 'TOO_FEW_CASES' and len(requests) == 2 and 'build' not in view['runs']


def test_missing_auto_acceptance_directory_stops(case):
    c = case
    c['config'] = LocalExecutorConfig(*[getattr(c['config'], f) for f in ('executor_id', 'identities_file', 'secrets_file',
                                      'model_config', 'sandbox_image', 'host_tests')])
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'nodir', prompt='写一个加一程序')
    run_until_idle(c, [REQUIREMENTS, CHECKS], [])
    assert service.get('a', created['session_id'])['attention']['code'] == 'AUTO_ACCEPTANCE_NOT_CONFIGURED'


def test_create_is_idempotent_and_http_requires_operator(case):
    c = case
    client = TestClient(create_app(c['store'], {}, identities=(OP, WORKER), executor_config=c['config']))
    headers = {'Authorization': 'Bearer ' + OP.token, 'Idempotency-Key': 'http-1'}
    first = client.post('/v1/sessions', json={'prompt': '写一个加一程序'}, headers=headers)
    second = client.post('/v1/sessions', json={'prompt': '写一个加一程序'}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()['session_id'] == second.json()['session_id']
    changed = client.post('/v1/sessions', json={'prompt': '另一个需求'}, headers=headers)
    assert changed.status_code == 409
    denied = client.post('/v1/sessions', json={'prompt': 'x'}, headers={'Authorization': 'Bearer ' + WORKER.token, 'Idempotency-Key': 'w'})
    assert denied.status_code == 403
    listed = client.get('/v1/sessions', headers={'Authorization': 'Bearer ' + OP.token}).json()['items']
    assert [item['session_id'] for item in listed] == [first.json()['session_id']]


def test_executor_config_accepts_optional_private_auto_directory(tmp_path):
    path = tmp_path / 'executor.json'
    path.write_text(json.dumps(dict(schema_version='loom.local-executor/v1', executor_id='x', identities_file='i.json',
        secrets_file='s.json', model_config='m.json', sandbox_image=None, host_tests={}, auto_acceptance_dir='auto')), encoding='utf-8')
    assert load_executor_config(path).auto_acceptance_dir == (tmp_path / 'auto').resolve()


def test_paused_run_is_left_alone_until_resumed(case):
    from loom.operator import OperatorService
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'pause', prompt='写一个加一程序')
    OperatorService(c['store']).pause('a', created['current_run_id'], 'pause-key', 'subject:operator', '用户暂停')
    assert service.get('a', created['session_id'])['state'] == 'paused'
    requests, _, _ = run_until_idle(c, [REQUIREMENTS], [])
    assert requests == [] and service.get('a', created['session_id'])['state'] == 'paused'


class FakeVerification:
    """Mirrors VerificationJobs.preview: existing_request carries the job state."""
    def __init__(self, outcome='queued'):
        self.requests = []
        self.jobs = self
        self.outcome = outcome

    def check(self, tenant):
        return None

    def preview(self, tenant, run_id, task_id):
        job = dict(state=self.outcome) if self.requests else None
        return {'ready': not self.requests, 'preview_digest': 'f' * 64, 'existing_request': job}

    def request(self, identity, run_id, task_id, key, *, expected_preview_digest, reason):
        self.requests.append((identity.subject_id, run_id, task_id, key))
        return {'state': 'queued'}


def run_with_verification(c, verification, bodies, outcomes):
    factory = lambda store, **kw: SessionService(store, **{**kw, 'verification': verification})
    with patch('loom.local_executor.SessionService', side_effect=factory):
        run_until_idle(c, bodies, outcomes)
        run_until_idle(c, [], [])


def test_configured_verification_is_requested_once_and_session_waits(case):
    c = case
    verification = FakeVerification('queued')
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER), verification=verification)
    created = service.create(PRINCIPAL, 'verify', prompt='写一个加一程序')
    run_with_verification(c, verification, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    view = service.get('a', created['session_id'])
    assert len(verification.requests) == 1 and verification.requests[0][0] == 'operator'
    # Regression: a queued job must keep the session waiting, never "delivered (unverified)".
    assert view['state'] == 'running' and view['steps'][3]['status'] == 'queued'


@pytest.mark.parametrize('outcome', ['rejected', 'disputed', 'blocked'])
def test_failed_verification_stops_and_is_never_delivered(case, outcome):
    c = case
    verification = FakeVerification(outcome)
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER), verification=verification)
    created = service.create(PRINCIPAL, 'v-' + outcome, prompt='写一个加一程序')
    run_with_verification(c, verification, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    view = service.get('a', created['session_id'])
    assert view['state'] == 'needs_attention' and view['attention']['code'] == 'VERIFICATION_' + outcome.upper()
    assert view['steps'][3]['status'] == outcome


def test_verified_goal_is_delivered_as_verified(case):
    c = case
    verification = FakeVerification('verified')
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER), verification=verification)
    created = service.create(PRINCIPAL, 'v-ok', prompt='写一个加一程序')
    run_with_verification(c, verification, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    build = service.get('a', created['session_id'])['runs']['build']
    task = build + ':implement'
    # Simulate the private verifier's trusted completion: task done, goal succeeded.
    with c['store'].transaction() as db:
        db.execute("UPDATE tasks SET status='done' WHERE tenant='a' AND id=?", (task,))
        db.execute("UPDATE runs SET status='succeeded' WHERE tenant='a' AND id=?", (build,))
    run_with_verification(c, verification, [], [])
    view = service.get('a', created['session_id'])
    assert view['state'] == 'verified' and view['independently_verified'] is True


def test_unloadable_verification_stops_instead_of_unverified_delivery(case):
    from loom.autopilot import UNAVAILABLE
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER), verification=UNAVAILABLE)
    created = service.create(PRINCIPAL, 'v-broken', prompt='写一个加一程序')
    run_with_verification(c, UNAVAILABLE, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    view = service.get('a', created['session_id'])
    assert view['attention']['code'] == 'VERIFICATION_UNAVAILABLE' and view['steps'][3]['status'] == 'unavailable'


def test_recurring_stop_after_resume_is_recorded_again_and_can_be_resumed(case):
    from loom.operator import OperatorService
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'again', prompt='写一个加一程序')
    run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, False])
    assert service.get('a', created['session_id'])['attention']['code'] == 'ACCEPTANCE_FAILED'
    for attempt in range(2):
        service.resume(PRINCIPAL, created['session_id'], f'resume-{attempt}', reason='再看一次')
        run_until_idle(c, [], [])
        view = service.get('a', created['session_id'])
        assert view['state'] == 'needs_attention' and view['attention']['code'] == 'ACCEPTANCE_FAILED'


def test_unregistered_operator_stops_with_reason(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'no-op', prompt='写一个加一程序')
    with patch('loom.local_executor.load_identities', return_value=(WORKER,)):
        worker = LocalExecutor(c['store'], c['config'])
    worker.advance_sessions()
    assert service.get('a', created['session_id'])['attention']['code'] == 'OPERATOR_NOT_REGISTERED'


def test_preview_goal_cap_stops_instead_of_spinning(tmp_path, case):
    from loom.public_access import PreviewStore
    c = dict(case, store=PreviewStore(tmp_path / 'preview.db', max_goals=1))
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'cap', prompt='写一个加一程序')
    run_until_idle(c, [REQUIREMENTS, CHECKS], [])
    view = service.get('a', created['session_id'])
    assert view['attention']['code'] == 'PREVIEW_LIMIT_REACHED' and 'build' not in view['runs']


def test_human_cancellation_is_respected(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'cancel', prompt='写一个加一程序')
    service.tick()
    run_id, task = created['current_run_id'], created['current_run_id'] + ':requirements'
    request = service.dispatch.history('a', run_id, task)['items'][-1]
    service.dispatch.cancel('a', run_id, request['request_id'], 'cancel-1', actor='subject:operator', reason='人工取消')
    service.tick()
    history = service.dispatch.history('a', run_id, task)['items']
    assert [h['state'] for h in history] == ['cancelled']
    assert service.get('a', created['session_id'])['attention']['code'] == 'EXECUTION_CANCELLED'


def test_session_view_hides_heldout_case_contents(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'hide', prompt='写一个加一程序')
    run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    view = service.get('a', created['session_id'])
    cases = view['steps'][1]['content']['cases']
    assert {'name': 'negative', 'hidden': True} in cases
    # Neither the held-out input nor its answer appears anywhere in the view (UUIDs may contain "-5").
    assert '{"value": -5}' not in json.dumps(view) and '{"answer": -4}' not in json.dumps(view)


def test_follow_up_continues_the_thread_with_previous_source_but_not_heldout_cases(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    first = service.create(PRINCIPAL, 'thread-1', prompt='写一个加一程序')
    from loom.store import Conflict
    with pytest.raises(Conflict):
        service.create(PRINCIPAL, 'too-early', prompt='再支持字符串数字', parent_session_id=first['session_id'])
    run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    second = service.create(PRINCIPAL, 'thread-2', prompt='再支持字符串形式的数字', parent_session_id=first['session_id'])
    assert second['thread_id'] == first['session_id'] and second['thread'] == [first['session_id'], second['session_id']]
    assert second['request'] == '再支持字符串形式的数字' and second['title'] == first['title']
    assert '上一版程序' in second['prompt'] and 'x["value"]+1' in second['prompt'] and 'negative' not in second['prompt']
    listed = service.list('a')
    assert len(listed) == 1 and listed[0]['session_id'] == second['session_id'] and listed[0]['turns'] == 2
    requests, _, _ = run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    assert '再支持字符串形式的数字' in requests[0]['messages'][-1]['content']
    third = service.create(PRINCIPAL, 'thread-3', prompt='输出加上 ok 字段', parent_session_id=second['session_id'])
    assert '1. 再支持字符串形式的数字' in third['prompt'] and third['prompt'].count('原始需求') == 1
    assert service.get('a', third['session_id'])['activity']['text']


def test_session_view_reports_current_activity_while_running(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'activity', prompt='写一个加一程序')
    assert created['activity']['text'] == '计划已由你批准'


def test_long_follow_up_chains_stay_within_instruction_limits(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    current = service.create(PRINCIPAL, 'long-0', prompt='加一' + '。' * 3990)
    long_source = GOOD + '#' + 'x' * 6000 + '\n'
    run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': long_source}], [True, True])
    for turn in range(1, 4):
        current = service.create(PRINCIPAL, f'long-{turn}', prompt=f'第{turn}次补充' + '要' * 3900,
                                 parent_session_id=current['session_id'])
        assert len(current['prompt']) <= 15000 + 20 and current['thread_id'] == current['thread'][0]
        run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': long_source}], [True, True])
        assert service.get('a', current['session_id'])['state'] == 'delivered'
    listed = service.list('a')
    assert len(listed) == 1 and listed[0]['turns'] == 4 and listed[0]['state'] == 'delivered'


def test_user_examples_join_heldout_acceptance_and_never_reach_prompts(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'examples', prompt='写一个加一程序',
                             examples=[{'input': {'value': 41}, 'expected': {'answer': 42}}])
    assert created['acceptance_examples'] == [{'input': {'value': 41}, 'expected': {'answer': 42}}]
    requests, sandbox, _ = run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    view = service.get('a', created['session_id'])
    assert view['state'] == 'delivered' and view['steps'][1]['split']['user_examples'] == 1
    assert view['acceptance_origin'] == 'tester-model-heldout+user-examples'
    # Acceptance ran on tester held-out + the user's example; no model ever saw 41/42.
    assert [len(tests) for _, tests in sandbox.inputs] == [3, 2]
    assert [t.name for t in sandbox.inputs[1][1]] == ['negative', 'user-example-1']
    assert all('41' not in '\n'.join(m['content'] for m in r['messages']) for r in requests)


@pytest.mark.parametrize('examples', [[{'input': 1}], ['x'], [{'input': 1, 'expected': 2}] * 11,
                                      [{'input': 'x' * 20000, 'expected': 1}]])
def test_invalid_user_examples_are_rejected_before_any_goal(case, examples):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    with pytest.raises(InvalidInput):
        service.create(PRINCIPAL, 'bad-examples', prompt='写一个加一程序', examples=examples)
    assert service.list('a') == []


def test_autopilot_reserves_from_byte_limits_not_full_context(case):
    from loom.model_pricing import FLASH, REQUEST_TOKEN_OVERHEAD
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'cheap', prompt='写一个加一程序')
    run_until_idle(c, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    view = service.get('a', created['session_id'])
    reserved = [e['payload']['cost'] for run in view['runs'].values() for e in c['store'].events('a', run) if e['type'] == 'budget.reserved']
    assert reserved[:2] == [FLASH.cost(131072 + REQUEST_TOKEN_OVERHEAD, 1024)] * 2
    assert reserved[2] == FLASH.cost(196608 + REQUEST_TOKEN_OVERHEAD, 1024)
    assert max(reserved) < 70000 and view['spend']['cost_limit_microusd'] == 225000


def test_http_session_accepts_examples(case):
    c = case
    client = TestClient(create_app(c['store'], {}, identities=(OP, WORKER), executor_config=c['config']))
    response = client.post('/v1/sessions', json={'prompt': '加一', 'examples': [{'input': {'value': 1}, 'expected': {'answer': 2}}]},
                           headers={'Authorization': 'Bearer ' + OP.token, 'Idempotency-Key': 'ex-http'})
    assert response.status_code == 201 and response.json()['acceptance_examples'][0]['expected'] == {'answer': 2}
    bad = client.post('/v1/sessions', json={'prompt': '加一', 'examples': [{'input': 1}]},
                      headers={'Authorization': 'Bearer ' + OP.token, 'Idempotency-Key': 'ex-bad'})
    assert bad.status_code == 422


def test_implementation_plan_waits_for_the_person_and_only_a_human_can_approve(case):
    """Plan v2.2 5.3: no machine approval; the executor never approves the build plan."""
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'needs-human', prompt='写一个程序：读取 {"value":整数}，输出 {"answer":value+1}')
    requests, sandbox, _ = run_until_idle(c, [REQUIREMENTS, CHECKS], [], approve=False)
    view = service.get('a', created['session_id'])
    assert view['state'] == 'awaiting_approval' and view['steps'][2]['status'] == 'awaiting_approval'
    assert view['approval']['development_cases'] == 3 and view['approval']['heldout_cases'] == 1
    assert len(requests) == 2 and sandbox.execution_calls == 0  # no developer call, nothing executed
    build = view['runs']['build']
    assert service.workflow.get_plan('a', build)['status'] == 'draft'
    # An instance principal (operator role) cannot approve, neither through the service nor over HTTP.
    instance = LocalIdentity('a', 'instance-op', frozenset({'operator'}), 'autopilot-instance-operator-fixture-0000')
    with pytest.raises(PermissionError):
        service.approve(Principal('a', 'instance-op', frozenset({'operator'})), created['session_id'], 'machine')
    app = create_app(c['store'], {}, identities=(OP, WORKER, instance), executor_config=c['config'])
    with TestClient(app) as client:
        denied = client.post(f"/v1/sessions/{created['session_id']}/approve", json={},
                             headers={'Authorization': 'Bearer ' + instance.token, 'Idempotency-Key': 'machine-approve'})
        assert denied.status_code == 403
        assert client.post('/v1/sessions', json={'prompt': 'x'},
                           headers={'Authorization': 'Bearer ' + instance.token, 'Idempotency-Key': 'machine-create'}).status_code == 403
        approved = client.post(f"/v1/sessions/{created['session_id']}/approve", json={'expected_digest': view['approval']['digest']},
                               headers={'Authorization': 'Bearer ' + OP.token, 'Idempotency-Key': 'human-approve'})
        assert approved.status_code == 200, approved.text
    events = c['store'].events('a', build)
    approvals = [e for e in events if e['type'] == 'workflow.plan_approved']
    assert len(approvals) == 1 and approvals[0]['actor'] == 'subject:' + OP.subject_id
    refusals = app.state.platform['agents'].ledger.history('a', 'approval-refusal', f"/v1/sessions/{created['session_id']}/approve")
    assert refusals and refusals[0]['record']['subject'] == 'subject:instance-op'
    run_until_idle(c, [{'source': GOOD}], [True, True])
    assert service.get('a', created['session_id'])['state'] == 'delivered'


def test_direct_topology_is_approved_by_the_creating_person_and_needs_no_second_click(case):
    c = case
    service = SessionService(c['store'], executor_config=c['config'], identities=(OP, WORKER))
    examples = [{'input': {'value': v}, 'expected': {'answer': v + 1}} for v in (1, 2, 3)]
    created = service.create(PRINCIPAL, 'direct-human', prompt='加一', examples=examples)
    run_until_idle(c, [{'source': GOOD}], [True, True], approve=False)
    assert service.get('a', created['session_id'])['state'] == 'delivered'
