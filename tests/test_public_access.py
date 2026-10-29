from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.public_access import COOKIE, PreviewAccess, PreviewStore, PublicPreview
from loom.store import Conflict
from loom.workflow import WorkflowService

ORIGIN='https://loom-preview.example'
OP=LocalIdentity('preview','operator',frozenset({'operator'}),'preview-test-operator-00000000000000000')
CONFIG=PreviewAccess(ORIGIN,'public_fixture_access_code_000000000000000',OP.token)
HEADERS={'Origin':ORIGIN,'Idempotency-Key':'one'}
GOAL=dict(title='公网测试',description='仅测试，无真实模型调用',acceptance=['tests.pass'],cost_limit_microusd=100000,token_limit=10000)


@pytest.fixture
def public(tmp_path):
    store=PreviewStore(tmp_path/'preview.db',max_goals=2)
    app=PublicPreview(create_app(store,{},identities=(OP,),local_console=False),lambda:CONFIG)
    return TestClient(app,base_url=ORIGIN),app,store


def login(client):
    response=client.post('/preview/login',data={'code':CONFIG.access_code},headers={'Origin':ORIGIN},follow_redirects=False)
    assert response.status_code==303
    return response


def test_login_uses_secure_host_cookie_and_hides_server_credentials(public):
    client,app,store=public
    assert client.get('/preview/login').headers['referrer-policy']=='same-origin'
    assert client.get('/console',follow_redirects=False).headers['location']=='/preview/login'
    assert client.get('/v1/me',headers={'X-Loom-Console':'local','Authorization':'Bearer '+OP.token}).status_code==401
    response=login(client);cookie=response.headers['set-cookie']
    assert all(part in cookie for part in ('__Host-loom-preview=','Secure','HttpOnly','SameSite=strict','Path=/'))
    me=client.get('/v1/me');assert me.json()['authentication']=='public-session' and not me.json()['production_identity']
    console=client.get('/console');assert console.status_code==200 and console.headers['referrer-policy']=='no-referrer'
    assert OP.token not in me.text+cookie and CONFIG.access_code not in me.text+cookie
    assert all(OP.token not in key for key in app.sessions)
    assert client.get('/v1/goals').json()=={'goals':[]}


@pytest.mark.parametrize('path',['/v1/me','/v1/goals','/v1/runs/abc/events','/console/assets/console.js','/openapi.json','/docs'])
def test_unauthenticated_never_reads_private_api_or_assets(public,path):
    assert public[0].get(path,follow_redirects=False).status_code==401


@pytest.mark.parametrize('origin',[None,'https://attacker.example','null','http://loom-preview.example'])
def test_login_and_mutations_require_exact_https_origin(public,origin):
    client,_,_=public;headers={} if origin is None else {'Origin':origin}
    assert client.post('/preview/login',data={'code':CONFIG.access_code},headers=headers).status_code==403
    login(client)
    assert client.post('/v1/goals',json=GOAL,headers={**headers,'Idempotency-Key':'x'}).status_code==403


def test_host_cannot_be_bypassed_with_forwarding_headers_or_bearer(public):
    client,_,_=public
    assert client.get('/console',headers={'Host':'127.0.0.1','X-Forwarded-Host':'loom-preview.example'}).status_code==421
    login(client)
    me=client.get('/v1/me',headers={'Authorization':'Bearer invalid','X-Loom-Console':'local','X-Forwarded-For':'127.0.0.1'})
    assert me.json()['subject_id']=='operator'


@pytest.mark.parametrize('method,path',[('post','/v1/runs'),('post','/v1/runs/x/claim'),('post','/v1/runs/x/tasks/a/submit'),('post','/v1/runs/x/tasks/a/artifacts'),('post','/v1/runs/x/effects/a/reconcile'),('get','/openapi.json'),('get','/docs'),('get','/work/model-secrets.json')])
def test_public_routes_cannot_mint_worker_results_or_reconcile_bills(public,method,path):
    client,_,_=public;login(client)
    assert getattr(client,method)(path,headers=HEADERS).status_code==403


def test_invited_public_operator_can_pause_resume_and_terminate_own_run(public):
    client,_,store=public
    workflow=WorkflowService(store)
    goal=workflow.create_goal('preview','control-goal',title='受邀运行',description='仅测试，无模型请求',
                              acceptance=['tests.pass'],cost_limit=100000,token_limit=10000)
    definition=json.loads((Path(__file__).resolve().parents[1]/'packs/software-dev/workflow-v1.json').read_text(encoding='utf-8'))
    definition['nodes']=[dict(id='work',title='测试节点',instruction='隔离数据',role='developer',
        limits=dict(cost_limit_microusd=10000,token_limit=1000,max_attempts=2))]
    run=goal['run_id'];saved=workflow.save_plan('preview',run,'control-plan',definition,0)
    workflow.approve_plan('preview',run,'control-approve',saved['revision'],saved['digest'])
    path=f'/v1/runs/{run}/control/'
    body={'reason':'隔离环境测试'}
    assert client.post(path+'pause',json=body,headers={**HEADERS,'Idempotency-Key':'pause'}).status_code==401
    login(client)
    assert client.post(path+'pause',json=body,headers={'Origin':'https://attacker.example','Idempotency-Key':'pause'}).status_code==403
    paused=client.post(path+'pause',json=body,headers={**HEADERS,'Idempotency-Key':'pause'})
    assert paused.status_code==200 and paused.json()['status']=='paused'
    assert client.post(path+'pause',json=body,headers={**HEADERS,'Idempotency-Key':'pause'}).json()==paused.json()
    resumed=client.post(path+'resume',json=body,headers={**HEADERS,'Idempotency-Key':'resume'})
    assert resumed.status_code==200 and resumed.json()['status']=='open'
    terminated=client.post(path+'terminate',json=body,headers={**HEADERS,'Idempotency-Key':'terminate'})
    assert terminated.status_code==200 and terminated.json()['status']=='cancelled'
    assert all(e['actor']=='subject:operator' for e in store.events('preview',run) if e['type'] in {'run.paused','run.resumed','run.cancelled'})
    assert client.post(path+'unrecognized',json=body,headers={**HEADERS,'Idempotency-Key':'bad'}).status_code==403
    assert client.post(f'/v1/runs/{run}/effects/{"0"*64}/reconcile',json={},headers=HEADERS).status_code==403


def test_durable_caps_preserve_idempotent_receipts(public):
    client,_,store=public;login(client)
    first=client.post('/v1/goals',json=GOAL,headers=HEADERS);assert first.status_code==201
    assert client.post('/v1/goals',json={**GOAL,'cost_limit_microusd':1000001},headers={**HEADERS,'Idempotency-Key':'cost'}).status_code==409
    assert client.post('/v1/goals',json={**GOAL,'token_limit':3200001},headers={**HEADERS,'Idempotency-Key':'tokens'}).status_code==409
    assert client.post('/v1/goals',json=GOAL,headers={**HEADERS,'Idempotency-Key':'two'}).status_code==201
    assert client.post('/v1/goals',json=GOAL,headers={**HEADERS,'Idempotency-Key':'three'}).status_code==409
    assert client.post('/v1/goals',json=GOAL,headers=HEADERS).json()==first.json()
    restarted=PreviewStore(store.path,max_goals=2);workflow=WorkflowService(restarted)
    with pytest.raises(Conflict):workflow.create_goal('preview','restart',title='cap',description='cap',acceptance=['tests.pass'],cost_limit=100000,token_limit=10000)


def test_cap_serializes_concurrent_admission(tmp_path):
    store=PreviewStore(tmp_path/'race.db',max_goals=2);workflow=WorkflowService(store)
    def create(n):
        try:return workflow.create_goal('preview',str(n),title='race',description='race',acceptance=['tests.pass'],cost_limit=100000,token_limit=10000)
        except Conflict:return None
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(create,range(8)))
    assert sum(r is not None for r in results)==2


def test_logout_expiry_restart_and_policy_rotation_invalidate_sessions(public):
    client,app,_=public;login(client)
    assert client.post('/preview/logout',headers={'Origin':ORIGIN}).status_code==204
    assert client.get('/v1/me').status_code==401
    login(client);app.sessions={k:time.monotonic()-1 for k in app.sessions}
    assert client.get('/v1/me').status_code==401
    login(client);app.config=lambda:replace(CONFIG,access_code='new_private_access_code_000000000000000')
    assert client.get('/v1/me').status_code==401


def test_bounded_login_rate_body_and_headers(public):
    client,_,_=public
    for _ in range(12):assert client.post('/preview/login',data={'code':'invalid'},headers={'Origin':ORIGIN}).status_code==401
    assert client.post('/preview/login',data={'code':CONFIG.access_code},headers={'Origin':ORIGIN}).status_code==429
    response=client.get('/preview/login');assert response.headers['cache-control']=='no-store' and 'frame-ancestors' in response.headers['content-security-policy']
    assert client.post('/preview/login',content=b'x'*262145,headers={'Origin':ORIGIN}).status_code==413
    assert client.options('/v1/goals').status_code==405


def test_isolated_bootstrap_is_repeatable_without_loading_model_secret(tmp_path):
    scripts=Path(__file__).resolve().parents[1]/'scripts';sys.path.insert(0,str(scripts))
    try:
        from prepare_public_preview import prepare,create_example
        (tmp_path/'loom-sandbox-image.id').write_text('sha256:'+'1'*64)
        first=prepare(tmp_path);second=prepare(tmp_path)
        assert first==second and first['model_calls']==0
        private=tmp_path/'loom-public-preview';access=json.loads((private/'access.json').read_bytes())
        assert len(access['access_code'])>=32 and not (tmp_path/'loom-private/model-secrets.json').exists()
        workflow=WorkflowService(PreviewStore(private/'preview.db'))
        assert workflow.get_plan('public-preview',first['run_id'])['status']=='draft'
        assert not workflow.store.snapshot('public-preview',first['run_id'])['tasks']
        from loom.auth import load_identities
        from loom.dispatch import DispatchService
        from loom.executor_config import load_executor_config
        identities=load_identities(private/'identities.json');operator=next(i for i in identities if 'operator' in i.roles)
        plan=workflow.get_plan('public-preview',first['run_id'])
        workflow.approve_plan('public-preview',first['run_id'],'approve',plan['revision'],plan['digest'],actor='subject:'+operator.subject_id)
        dispatch=DispatchService(workflow.store,config=load_executor_config(private/'executor.json'),identities=identities)
        assert dispatch.preview('public-preview',first['run_id'],first['run_id']+':requirements')['ready']
    finally:sys.path.remove(str(scripts))


def test_example_endpoint_is_explicit_idempotent_and_bounded(public):
    client,app,store=public
    def example(key):return WorkflowService(store).create_goal('preview',key,title='样例',description='样例',acceptance=['tests.pass'],cost_limit=100000,token_limit=10000)
    app.example=example
    assert client.post('/preview/examples',content='{}',headers=HEADERS).status_code==401
    login(client)
    first=client.post('/preview/examples',content='{}',headers=HEADERS);assert first.status_code==201
    assert client.post('/preview/examples',content='{}',headers=HEADERS).json()==first.json()
    assert client.post('/preview/examples',content='{"code":"ignored"}',headers=HEADERS).status_code==422
    assert client.post('/preview/examples',content='{}',headers={'Origin':ORIGIN}).status_code==422
    assert not store.snapshot('preview',first.json()['run_id'])['tasks']


def test_shared_preview_limits_login_sessions_and_write_flood(public):
    client,app,_=public;client.get('/preview/login');app.sessions={str(i):time.monotonic()+100 for i in range(16)}
    assert client.post('/preview/login',data={'code':CONFIG.access_code},headers={'Origin':ORIGIN}).status_code==429
    app.sessions.clear();login(client)
    app.writes.extend([time.monotonic()]*60)
    assert client.post('/v1/goals',json=GOAL,headers=HEADERS).status_code==429
    assert client.get('/v1/goals').status_code==200


@pytest.mark.parametrize('origin',['http://loom-preview.example','https://user@loom-preview.example','https://loom-preview.example/path','https://loom-preview.example:443'])
def test_invalid_origin_configuration_is_rejected(origin):
    with pytest.raises(ValueError):replace(CONFIG,origin=origin)


def test_windows_chinese_launchers_have_power_shell_compatible_encoding():
    root=Path(__file__).resolve().parents[1]
    for name in ('start_local_dev.ps1','start_public_preview.ps1','stop_public_preview.ps1'):
        assert (root/'scripts'/name).read_bytes().startswith(b'\xef\xbb\xbf'),name
