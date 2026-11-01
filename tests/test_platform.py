"""FR-002/003/004/005/019/027/031/032/036/037/038/049/054 through the HTTP API.

Model HTTP and the sandbox are test doubles (see tests/test_autopilot.py)."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from loom.adaptation import agreement, case_conflicts, choose_topology, deviation_bounds, repair_plan, sampled
from loom.api import create_app
from loom.auth import LocalIdentity
from loom.autopilot import SessionService
from loom.builtin_resources import software_workflow
from loom.platform_ledger import Ledger
from loom.store import InvalidInput
from tests.test_approved_execution import OP, WORKER
from tests.test_autopilot import CHECKS, GOOD, PRINCIPAL, REQUIREMENTS, case, run_until_idle

OP2 = LocalIdentity('a', 'reviewer-op', frozenset({'operator'}), 'platform-fixture-second-operator-0000000')
VIEW = LocalIdentity('a', 'viewer', frozenset({'viewer'}), 'platform-fixture-viewer-identity-00000000')
OTHER = LocalIdentity('b', 'operator-b', frozenset({'operator'}), 'platform-fixture-other-tenant-op-0000000')
_counter = iter(range(10 ** 6))


def h(identity=OP, key=True):
    headers = {'Authorization': 'Bearer ' + identity.token}
    if key:
        headers['Idempotency-Key'] = f'k-{next(_counter)}'
    return headers


@pytest.fixture
def api(case):
    app = create_app(case['store'], {}, identities=(OP, WORKER, OP2, VIEW, OTHER), executor_config=case['config'])
    return dict(case, client=TestClient(app), app=app)


def agent(**extra):
    return {'agent_id': 'py-dev', 'base_role': 'developer', 'version': '1.0.0', 'skills': ['code.implement'],
            'tools': ['workspace.read', 'workspace.write', 'tests.run'], 'description': '只写标准库程序', **extra}


# FR-002 ----------------------------------------------------------------------
def test_agents_register_version_discover_and_never_widen(api):
    c = api['client']
    first = c.post('/v1/agents', json=agent(), headers=h())
    assert first.status_code == 201 and first.json()['authority_widened'] is False
    assert c.post('/v1/agents', json=agent(), headers=h()).status_code == 409  # a version is registered once
    assert c.post('/v1/agents', json=agent(version='0.9.0'), headers=h()).status_code == 409
    assert c.post('/v1/agents', json=agent(version='1.1.0', description='v2'), headers=h()).status_code == 201
    widen = c.post('/v1/agents', json={**agent(), 'agent_id': 'bad', 'base_role': 'analyst', 'tools': ['workspace.write']}, headers=h())
    assert widen.status_code == 422
    assert c.post('/v1/agents', json=agent(agent_id='w'), headers=h(WORKER)).status_code == 403
    listed = c.get('/v1/agents?skill=code.implement', headers=h(key=False)).json()['items']
    assert [a['agent_id'] for a in listed] == ['py-dev'] and listed[0]['version'] == '1.1.0'
    assert c.get('/v1/agents/py-dev', headers=h(key=False)).json()['versions'] == ['1.0.0', '1.1.0']
    assert c.get('/v1/agents', headers=h(OTHER, key=False)).json()['items'] == []
    assert Ledger(api['store']).verify('a', 'agent')


# FR-003 / FR-031 / FR-005 / FR-054 ---------------------------------------------
def test_profiles_routing_tools_memory_and_sampling_come_from_real_runs(api):
    c = api['client']
    service = SessionService(api['store'], executor_config=api['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'profile-run', prompt='写一个加一程序')
    run_until_idle(api, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
    profiles = {p['role']: p for p in c.get('/v1/capability-profiles', headers=h(key=False)).json()['items']}
    assert set(profiles) == {'analyst', 'tester', 'developer'}
    assert profiles['developer']['success_rate'] == 1.0 and profiles['developer']['p50_seconds'] is not None
    assert c.post('/v1/capability-profiles/snapshots', headers=h()).status_code == 201
    c.post('/v1/agents', json=agent(), headers=h())
    routed = c.get('/v1/agent-routing?role=developer', headers=h(key=False)).json()
    assert routed['selected'] == 'py-dev' and routed['candidates'][0]['score'] > 0
    assert c.get('/v1/agent-routing?role=analyst', headers=h(key=False)).json()['builtin_fallback'] == 'analyst'
    tools = c.get('/v1/tool-selection?role=developer&need=tests.run', headers=h(key=False)).json()
    assert tools['selected'] == 'tests.run' and tools['execution_authorized'] is False
    assert c.get('/v1/tool-selection?role=analyst&need=tests.run', headers=h(key=False)).json()['selected'] is None
    # Delivery wrote session and task memory; recall is scoped to that thread.
    found = c.get('/v1/memory-search', params={'q': '加一程序', 'session_id': created['session_id']}, headers=h(key=False)).json()['items']
    assert found and found[0]['source'] == 'autopilot'
    assert c.get('/v1/memory-search', params={'q': '加一程序', 'session_id': 'other-thread'}, headers=h(key=False)).json()['items'] == []
    report = c.get('/v1/evaluation/sampling', headers=h(key=False)).json()
    assert report['reviewed'] == 0 and report['upper_bound'] is None and report['samples_needed_for_2pct'] == 149


# FR-004 ----------------------------------------------------------------------
def test_knowledge_needs_independent_review_before_recall_and_is_tenant_isolated(api):
    c = api['client']
    entry = c.post('/v1/knowledge', json={'title': '金额计算', 'body': '金额统一用整数分计算，避免浮点误差', 'tags': ['money']}, headers=h()).json()
    assert entry['status'] == 'proposed' and entry['recallable'] is False
    assert c.get('/v1/knowledge-search?q=金额', headers=h(key=False)).json()['items'] == []
    self_review = c.post(f"/v1/knowledge/{entry['entry_id']}/review", json={'decision': 'approved', 'note': '自审'}, headers=h())
    assert self_review.status_code == 409
    approved = c.post(f"/v1/knowledge/{entry['entry_id']}/review", json={'decision': 'approved', 'note': '核对过'}, headers=h(OP2))
    assert approved.status_code == 200 and approved.json()['reviewer'] == 'subject:reviewer-op'
    hits = c.get('/v1/knowledge-search?q=金额计算', headers=h(key=False)).json()['items']
    assert hits[0]['entry_id'] == entry['entry_id']
    assert c.get('/v1/knowledge-search?q=金额计算', headers=h(OTHER, key=False)).json()['items'] == []
    rejected = c.post('/v1/knowledge', json={'title': '错误做法', 'body': '金额用浮点', 'tags': []}, headers=h()).json()
    c.post(f"/v1/knowledge/{rejected['entry_id']}/review", json={'decision': 'rejected', 'note': '不正确'}, headers=h(OP2))
    assert all(i['entry_id'] != rejected['entry_id'] for i in c.get('/v1/knowledge-search?q=金额', headers=h(key=False)).json()['items'])
    assert c.post('/v1/knowledge', json={'title': 'x', 'body': 'y'}, headers=h(VIEW)).status_code == 403


def test_memory_scopes(api):
    c = api['client']
    c.post('/v1/memory', json={'scope': 'tenant', 'content': '团队偏好：输出 JSON 时键按字母排序'}, headers=h())
    c.post('/v1/memory', json={'scope': 'session', 'scope_id': 't1', 'content': '本线程：错误码用 snake_case'}, headers=h())
    assert c.post('/v1/memory', json={'scope': 'session', 'content': 'x'}, headers=h()).status_code == 422
    both = c.get('/v1/memory-search', params={'q': '输出 JSON 错误码 snake_case', 'session_id': 't1'}, headers=h(key=False)).json()['items']
    assert [m['scope'] for m in both] == ['session', 'tenant']


# FR-019 / FR-027 -------------------------------------------------------------
def test_direct_topology_uses_user_examples_with_one_model_call(api):
    c = api['client']
    examples = [{'input': {'value': v}, 'expected': {'answer': v + 1}} for v in (1, 2, 3)]
    created = c.post('/v1/sessions', json={'prompt': '加一', 'examples': examples}, headers=h()).json()
    assert created['topology'] == 'direct' and [s['key'] for s in created['steps']] == ['implement', 'verify']
    requests, sandbox, _ = run_until_idle(api, [{'source': GOOD}], [True])
    view = c.get('/v1/sessions/' + created['session_id'], headers=h(key=False)).json()
    assert view['state'] == 'delivered' and len(requests) == 1 and view['acceptance_origin'] == 'user-examples'
    assert len(sandbox.inputs[0][1]) == 3  # every user example is acceptance
    instruction = json.loads(requests[0]['messages'][1]['content'])['instruction']
    assert '"value": 1' in instruction and '"value": 3' not in instruction  # one third withheld from the developer
    c.post('/v1/sessions', json={'prompt': '加一'}, headers=h())
    report = {r['topology']: r for r in c.get('/v1/topology/report', headers=h(key=False)).json()['items']}
    assert report['direct']['delivered'] == 1 and report['standard']['sessions'] == 1
    assert report['direct']['avg_model_calls'] == 1.0


def test_tester_contradicting_user_examples_stops_with_low_confidence(api):
    service = SessionService(api['store'], executor_config=api['config'], identities=(OP, WORKER))
    created = service.create(PRINCIPAL, 'low', prompt='加一', examples=[{'input': {'value': 1}, 'expected': {'answer': 99}}])
    requests, _, _ = run_until_idle(api, [REQUIREMENTS, CHECKS], [])
    view = service.get('a', created['session_id'])
    assert view['attention']['code'] == 'LOW_CONFIDENCE_TESTS' and 'one' in view['attention']['task_id']
    assert len(requests) == 2 and 'build' not in view['runs']


def test_consistency_and_topology_rules():
    assert agreement([{'a': 1}, {'a': 1}, {'a': 2}]) == {'samples': 3, 'distinct': 2, 'agreement': 0.6667}
    assert case_conflicts([{'name': 'x', 'input': 1, 'expected': 2}], [{'input': 1, 'expected': 3}]) == ['x']
    assert choose_topology('p', [], 'auto')[0] == 'standard'
    with pytest.raises(InvalidInput):
        choose_topology('p', [{}], 'direct')


def test_consistency_endpoint(api):
    out = api['client'].post('/v1/consistency', json={'samples': [1, 1, 2]}, headers=h(key=False)).json()
    assert out['agreement'] == 0.6667 and out['low_confidence'] is True


# FR-032 ----------------------------------------------------------------------
def test_plan_repair_primitives_on_draft_plans_only(api):
    c = api['client']
    goal = c.post('/v1/goals', json={'title': '改图', 'description': '测试', 'acceptance': ['tests.pass']}, headers=h()).json()
    definition = software_workflow()
    saved = c.put(f"/v1/goals/{goal['run_id']}/plan", json={'definition': definition, 'expected_revision': 0}, headers=h()).json()
    bypass = c.post(f"/v1/goals/{goal['run_id']}/plan/repairs",
                    json={'expected_revision': saved['revision'], 'operation': {'op': 'bypass', 'node_id': 'fix'}}, headers=h())
    assert bypass.status_code == 200
    nodes = {n['id']: n for n in bypass.json()['definition']['nodes']}
    assert 'fix' not in nodes and nodes['verify']['depends'] == ['review']
    new = dict(id='lint', title='静态检查', instruction='检查', role='reviewer', limits={'cost_limit_microusd': 1, 'token_limit': 1})
    insert = c.post(f"/v1/goals/{goal['run_id']}/plan/repairs",
                    json={'expected_revision': bypass.json()['revision'], 'operation': {'op': 'insert_prereq', 'node_id': 'review', 'node': new}}, headers=h())
    assert insert.status_code == 200 and 'lint' in {n['id'] for n in insert.json()['definition']['nodes']}
    cycle = c.post(f"/v1/goals/{goal['run_id']}/plan/repairs",
                   json={'expected_revision': insert.json()['revision'], 'operation': {'op': 'rewire', 'node_id': 'spec', 'depends': ['verify']}}, headers=h())
    assert cycle.status_code == 422
    with pytest.raises(Exception):
        repair_plan(definition, {'op': 'bypass', 'node_id': 'spec', 'x': 1} | {'node_id': 'nope'})
    approve = c.post(f"/v1/goals/{goal['run_id']}/plan/approve", json={'expected_revision': insert.json()['revision'],
                     'expected_digest': insert.json()['digest']}, headers=h())
    assert approve.status_code in (200, 409, 422)
    if approve.status_code == 200:
        after = c.post(f"/v1/goals/{goal['run_id']}/plan/repairs",
                       json={'expected_revision': insert.json()['revision'], 'operation': {'op': 'bypass', 'node_id': 'lint'}}, headers=h())
        assert after.status_code == 409


# FR-036 / FR-037 -------------------------------------------------------------
def rpc(c, method, params=None, identity=OP, request_id=1):
    return c.post('/mcp', json={'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}}, headers=h(identity, key=False)).json()


def test_loom_mcp_server_lists_validates_and_authorizes_tools(api):
    c = api['client']
    init = rpc(c, 'initialize')
    assert init['result']['serverInfo']['name'] == 'loom'
    names = {t['name'] for t in rpc(c, 'tools/list')['result']['tools']}
    assert {'loom_create_session', 'loom_get_session', 'loom_list_sessions', 'loom_search_knowledge', 'loom_list_agents'} <= names
    bad = rpc(c, 'tools/call', {'name': 'loom_create_session', 'arguments': {'prompt': ''}})
    assert bad['error']['code'] == -32602
    created = rpc(c, 'tools/call', {'name': 'loom_create_session', 'arguments': {'prompt': '加一', 'idempotency_key': 'mcp-1'}})
    session_id = created['result']['structuredContent']['session_id']
    got = rpc(c, 'tools/call', {'name': 'loom_get_session', 'arguments': {'session_id': session_id}})
    assert got['result']['structuredContent']['state'] == 'running'
    denied = rpc(c, 'tools/call', {'name': 'loom_create_session', 'arguments': {'prompt': 'x', 'idempotency_key': 'v'}}, identity=VIEW)
    assert denied['result']['isError'] is True
    assert rpc(c, 'nope')['error']['code'] == -32601
    note = c.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers=h(key=False))
    assert note.status_code == 202
    assert c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).status_code == 401
    check = c.post('/v1/function-calls/validate', json={'schema': {'type': 'object', 'required': ['a']}, 'arguments': {}}, headers=h(key=False)).json()
    assert check == {'valid': False, 'errors': ['$.a: required']}


def test_mcp_client_discovers_remote_tools_without_granting_execution(api):
    c = api['client']
    def handler(request):
        body = json.loads(request.content)
        assert body['method'] == 'tools/list' and request.url.host == 'tools.example.com'
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {'tools': [{'name': 'search', 'description': 'x', 'inputSchema': {'type': 'object'}}]}})
    api['app'].state.platform['mcp_servers'].transport = httpx.MockTransport(handler)
    assert c.post('/v1/mcp-servers', json={'name': 'remote', 'url': 'http://tools.example.com/mcp'}, headers=h()).status_code == 422
    assert c.post('/v1/mcp-servers', json={'name': 'remote', 'url': 'https://tools.example.com/mcp'}, headers=h()).status_code == 201
    found = c.get('/v1/mcp-servers/remote/tools', headers=h(key=False)).json()
    assert found['tools'][0]['name'] == 'search' and found['executable_by_roles'] is False


# FR-038 ----------------------------------------------------------------------
def plugin(**extra):
    return {'plugin_id': 'weather', 'base_url': 'https://api.example.com', 'roles': ['analyst'], 'enabled': False,
            'endpoints': [{'name': 'city', 'method': 'GET', 'path': '/v1/city/{name}',
                           'params': {'type': 'object', 'required': ['name'], 'additionalProperties': False,
                                      'properties': {'name': {'type': 'string', 'maxLength': 40}, 'days': {'type': 'integer', 'minimum': 1, 'maximum': 7}}}}],
            **extra}


def test_http_plugins_are_declarative_get_only_host_pinned_and_opt_in(api):
    c = api['client']
    assert c.post('/v1/plugins', json=plugin(base_url='http://api.example.com'), headers=h()).status_code == 422
    for internal in ('https://127.0.0.1', 'https://localhost', 'https://169.254.169.254', 'https://db.internal', 'https://[::1]'):
        assert c.post('/v1/plugins', json=plugin(base_url=internal), headers=h()).status_code == 422
    post = plugin(); post['endpoints'][0]['method'] = 'POST'
    assert c.post('/v1/plugins', json=post, headers=h()).status_code == 422
    assert c.post('/v1/plugins', json=plugin(), headers=h()).status_code == 201
    dry = c.post('/v1/plugins/weather/invoke', json={'endpoint': 'city', 'arguments': {'name': '上海/x', 'days': 3}}, headers=h()).json()
    assert dry['url'] == 'https://api.example.com/v1/city/%E4%B8%8A%E6%B5%B7%2Fx?days=3' and dry['called'] is False
    invalid = c.post('/v1/plugins/weather/invoke', json={'endpoint': 'city', 'arguments': {'name': 'a', 'days': 30}}, headers=h())
    assert invalid.status_code == 422
    disabled = c.post('/v1/plugins/weather/invoke', json={'endpoint': 'city', 'arguments': {'name': 'a'}, 'dry_run': False}, headers=h())
    assert disabled.status_code == 409
    c.post('/v1/plugins', json=plugin(enabled=True, version='1.1.0'), headers=h())
    api['app'].state.platform['plugins'].transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'temp': 21}))
    live = c.post('/v1/plugins/weather/invoke', json={'endpoint': 'city', 'arguments': {'name': 'a'}, 'dry_run': False}, headers=h()).json()
    assert live['called'] and live['json'] == {'temp': 21} and live['status'] == 200
    selection = c.get('/v1/tool-selection?role=analyst', headers=h(key=False)).json()
    assert 'plugin:weather' in [o['tool_id'] for o in selection['options'] if o['allowed']]


# FR-049 ----------------------------------------------------------------------
def test_pack_canary_promote_and_rollback(api):
    from loom.domain_packs import DomainPackService
    c = api['client']
    goal = c.post('/v1/goals', json={'title': '发布', 'description': '审计', 'acceptance': ['tests.pass']}, headers=h()).json()
    base = software_workflow()
    nodes = [dict(n) for n in base['nodes']]
    for n in nodes:
        n.pop('evaluation_digest', None)
    bundle = dict(schema_version='loom.domain-pack/v1', name='software-dev', version='0.2.0', title='软件 0.2', description='新模板',
                  scope='local-workspace', roles=base['roles'], templates=[dict(id='full', title='全流程', nodes=nodes)])
    DomainPackService(api['store']).publish('a', 'pack-02', goal['run_id'], bundle, actor='subject:operator')
    assert c.get('/v1/pack-rollouts/software-dev', headers=h(key=False)).json()['stable'] == '0.1.0'
    assert c.post('/v1/pack-rollouts/software-dev', json={'action': 'canary', 'version': '9.9.9', 'percent': 10}, headers=h()).status_code == 422
    canary = c.post('/v1/pack-rollouts/software-dev', json={'action': 'canary', 'version': '0.2.0', 'percent': 50}, headers=h()).json()
    assert canary['canary'] == '0.2.0'
    picks = {c.get('/v1/pack-rollouts/software-dev/resolve', params={'subject': f's{i}'}, headers=h(key=False)).json()['version'] for i in range(40)}
    assert picks == {'0.1.0', '0.2.0'}
    promoted = c.post('/v1/pack-rollouts/software-dev', json={'action': 'promote'}, headers=h()).json()
    assert promoted['stable'] == '0.2.0' and promoted['previous_stable'] == '0.1.0'
    rolled = c.post('/v1/pack-rollouts/software-dev', json={'action': 'rollback'}, headers=h()).json()
    assert rolled['stable'] == '0.1.0'
    assert c.post('/v1/pack-rollouts/software-dev', json={'action': 'promote'}, headers=h()).status_code == 409


# FR-054 ----------------------------------------------------------------------
def test_sampling_math_and_policy(case):
    assert deviation_bounds(30, 0)['upper_bound'] == 0.095
    assert deviation_bounds(149, 0)['meets_2pct_bound'] is True
    assert 0.02 < deviation_bounds(100, 1)['upper_bound'] < 0.06
    assert sampled('x', 0, risk='high') and not sampled('x', 0) and sampled('x', 1)
    assert sampled('abc', 0.5) == sampled('abc', 0.5)
    from tests.test_autopilot import FakeVerification, run_with_verification
    verification = FakeVerification('queued')
    service = SessionService(case['store'], executor_config=case['config'], identities=(OP, WORKER), verification=verification)
    created = service.create(PRINCIPAL, 'unsampled', prompt='加一')
    from unittest.mock import patch
    factory = lambda store, **kw: SessionService(store, **{**kw, 'verification': verification, 'sampling_rate': 0.0})
    with patch('loom.local_executor.SessionService', side_effect=factory):
        run_until_idle(case, [REQUIREMENTS, CHECKS, {'source': GOOD}], [True, True])
        run_until_idle(case, [], [])
    assert verification.requests == [] and service.get('a', created['session_id'])['state'] == 'delivered'
