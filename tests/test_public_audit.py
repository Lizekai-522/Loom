"""A program may echo hidden inputs; public readers cannot fetch that trace."""
import json

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.store import digest
from tests.test_public_access import public,login,OP


def seed_sensitive_execution(store):
    run=store.create_run('preview','sensitive-run',acceptance=['tests.pass'])['run_id']
    store.add_task('preview',run,'add',task_id=run+':work',title='隐私测试')
    claim=store.claim_next('preview',run,'test-worker')
    output=dict(kind='python-json-program',source='print(1)',tests=dict(cases=[dict(name='hidden-case-name',passed=True,
        status='passed',stdout='hidden-stdout-input-marker',stderr='hidden-stderr-input-marker')]))
    with store.transaction() as db:
        store._event(db,'preview',run,'execution.evaluated',dict(task_id=claim.task_id,epoch=claim.epoch,
            content_hash=digest(output),hard_criteria_passed=True,host_cases_total=1,host_cases_passed=1),'local-executor')
        store._event(db,'preview',run,'tool.done',dict(call_id='sensitive-tool',state='done',
            result=dict(provider_response='private-model-transcript-marker',report=output['tests'])),'gateway')
    store.submit_output(claim,'sensitive-submit',output)
    return run


def test_public_event_and_replay_do_not_expose_hidden_host_io_or_model_transcripts(public):
    client,_,store=public;run=seed_sensitive_execution(store);login(client)
    event_response=client.get('/v1/runs/'+run+'/events');replay=client.get('/v1/runs/'+run+'/replay')
    review=client.get('/v1/runs/'+run+'/review')
    assert event_response.status_code==replay.status_code==review.status_code==200
    text=event_response.text+replay.text+review.text
    for marker in ('hidden-case-name','hidden-stdout-input-marker','hidden-stderr-input-marker','private-model-transcript-marker'):
        assert marker not in text
    data=event_response.json();assert data['redacted'] and data['source_chain_verified'] and not data['original_envelopes_included']
    assert all(event['payload_redacted'] and 'original_event_hash' in event and 'hash' not in event for event in data['events'])
    assert any(event['type']=='execution.evaluated' and event['payload']['host_cases_passed']==1 for event in data['events'])
    assert replay.json()['outputs_redacted'] and 'output' not in replay.json()['tasks'][run+':work']
    assert review.json()['tasks'][0]['artifact']['cases'][0]['name']=='case-1'
    # The immutable original evidence and its chain remain intact for local audit.
    original=store.events('preview',run);assert store.verify_events(original)
    assert 'hidden-stderr-input-marker' in json.dumps(original)


def test_named_local_operator_can_still_read_original_diagnostics(public):
    _,_,store=public;run=seed_sensitive_execution(store)
    with TestClient(create_app(store,{},identities=(OP,))) as client:
        response=client.get('/v1/runs/'+run+'/events',headers={'Authorization':'Bearer '+OP.token,'X-Public-Preview':'false'})
        assert response.status_code==200 and 'hidden-stderr-input-marker' in response.text
        assert 'redacted' not in response.json()


@pytest.mark.parametrize('header',['false','local','0'])
def test_client_headers_cannot_disable_public_projection(public,header):
    client,_,store=public;run=seed_sensitive_execution(store);login(client)
    response=client.get('/v1/runs/'+run+'/events',headers={'X-Public-Preview':header,'X-Loom-Console':'local','Authorization':'Bearer '+OP.token})
    assert response.status_code==200 and response.json()['redacted'] and 'hidden-stderr-input-marker' not in response.text
