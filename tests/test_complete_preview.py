import importlib.util
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
from loom.api import create_app
from loom.auth import LocalIdentity
from loom.public_access import PublicPreview,PreviewStore
from loom.workflow import WorkflowService
from tests.test_public_access import CONFIG,HEADERS,login

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
try:
    spec=importlib.util.spec_from_file_location('complete_preview_template',ROOT/'scripts/prepare_public_preview.py')
    script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
finally:sys.path.pop(0)
OP=LocalIdentity('public-preview','preview-operator',frozenset({'operator'}),CONFIG.bearer)

def test_complete_template_keeps_original_multirole_example_and_frozen_approval(tmp_path):
    store=PreviewStore(tmp_path/'preview.db');workflow=WorkflowService(store);evaluation='a'*64
    original=script.create_example(store,ROOT,evaluation,OP,'original');old=workflow.get_plan('public-preview',original['run_id'])
    goal=script.create_verified_example(store,ROOT,evaluation,OP,'complete')
    assert goal==script.create_verified_example(store,ROOT,evaluation,OP,'complete')
    plan=workflow.get_plan('public-preview',goal['run_id']);nodes=plan['definition']['nodes']
    assert len(nodes)==1 and nodes[0]['role']=='developer' and nodes[0]['evaluation_digest']==evaluation
    assert len(plan['definition']['roles'])==7 and len(old['definition']['nodes'])==3
    assert workflow.get_plan('public-preview',original['run_id'])==old
    assert store.snapshot('public-preview',goal['run_id'])['status']=='draft'
    workflow.approve_plan('public-preview',goal['run_id'],'approve-complete',plan['revision'],plan['digest'],actor='subject:'+OP.subject_id)
    snapshot=store.snapshot('public-preview',goal['run_id'])
    assert snapshot['status']=='open' and len(snapshot['tasks'])==1 and snapshot['tasks'][0]['status']=='pending'
    assert snapshot['spent']==snapshot['reserved']==0 and not store.events('public-preview',goal['run_id'])[-1]['type'].startswith('verification.')

def test_complete_example_public_route_requires_invite_exact_body_and_cap(tmp_path):
    store=PreviewStore(tmp_path/'public.db',max_goals=1);app=create_app(store,{},identities=(OP,))
    guard=PublicPreview(app,lambda:CONFIG,verified_example=lambda key:script.create_verified_example(store,ROOT,'a'*64,OP,key))
    client=TestClient(guard,base_url=CONFIG.origin,follow_redirects=False);url='/preview/examples/verified'
    assert client.post(url,json={},headers=HEADERS).status_code==401
    login(client)
    assert client.post(url,json={'cost_limit':0},headers=HEADERS).status_code==422
    first=client.post(url,json={},headers=HEADERS);assert first.status_code==201
    assert client.post(url,json={},headers=HEADERS).json()==first.json()
    assert client.post(url,json={},headers={**HEADERS,'Idempotency-Key':'new-goal'}).status_code==409
    assert client.post(url,json={},headers={**HEADERS,'Origin':'https://attacker.example'}).status_code==403

def test_no_complete_example_callback_means_no_public_creation(tmp_path):
    client=TestClient(PublicPreview(create_app(PreviewStore(tmp_path/'none.db'),{},identities=(OP,)),lambda:CONFIG),base_url=CONFIG.origin,follow_redirects=False)
    login(client)
    assert client.post('/preview/examples/verified',json={},headers=HEADERS).status_code==403
