"""Per-person identities for the invited preview: each member signs in with their own code."""
import json
from pathlib import Path
import stat
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.public_access import COOKIE, MEMBERS_SCHEMA, PreviewAccess, PreviewMember, PreviewStore, PublicPreview, code_hash, load_members

ORIGIN = 'https://loom-preview.example'
ADMIN_CODE = 'public_fixture_access_code_000000000000000'
ALICE = LocalIdentity('public-preview', 'alice', frozenset({'user'}), 'preview-test-alice-0000000000000000000000')
BOB = LocalIdentity('public-preview', 'bob', frozenset({'user'}), 'preview-test-bob-00000000000000000000000000')
OPERATOR = LocalIdentity('public-preview', 'operator', frozenset({'operator'}), 'preview-test-operator-000000000000000000')
CODES = {'alice': 'alice_personal_code_0000000000000000', 'bob': 'bob_personal_code_000000000000000000'}
HEADERS = {'Origin': ORIGIN}


def member(identity):
    return PreviewMember(identity.subject_id, code_hash(CODES[identity.subject_id]), identity.token)


class Config:
    """Mutable stand-in for the files the server re-reads on every request."""
    def __init__(self):
        self.members = (member(ALICE), member(BOB))

    def __call__(self):
        return PreviewAccess(ORIGIN, ADMIN_CODE, ALICE.token, members=self.members)


@pytest.fixture
def team(tmp_path):
    config = Config()
    store = PreviewStore(tmp_path / 'preview.db', max_goals=20)
    app = PublicPreview(create_app(store, {}, identities=(ALICE, BOB, OPERATOR), local_console=False), config)
    def client():
        return TestClient(app, base_url=ORIGIN)
    return client, config, app


def login(client, code):
    return client.post('/preview/login', data={'code': code}, headers=HEADERS, follow_redirects=False)


def test_each_member_signs_in_as_themselves(team):
    client, _, app = team
    alice, bob = client(), client()
    assert login(alice, CODES['alice']).status_code == 303 and login(bob, CODES['bob']).status_code == 303
    assert alice.get('/v1/me').json()['subject_id'] == 'alice'
    assert bob.get('/v1/me').json()['subject_id'] == 'bob'
    cookie = alice.cookies.get(COOKIE)
    assert cookie.startswith('v2.alice.') and ALICE.token not in cookie and CODES['alice'] not in cookie
    assert all(ALICE.token not in key and BOB.token not in key for key in app.sessions)


def test_shared_admin_code_is_not_a_login_when_members_exist(team):
    client, _, _ = team
    visitor = client()
    assert login(visitor, ADMIN_CODE).status_code == 401
    assert visitor.get('/v1/me').status_code == 401


def test_forged_or_swapped_member_token_is_rejected(team):
    client, _, _ = team
    alice = client(); login(alice, CODES['alice'])
    token = alice.cookies.get(COOKIE)
    parts = token.split('.')
    forged = client(); forged.cookies.set(COOKIE, '.'.join([parts[0], 'bob', *parts[2:]]), domain='loom-preview.example')
    assert forged.get('/v1/me').status_code == 401
    legacy = client(); legacy.cookies.set(COOKIE, '.'.join(parts[2:]), domain='loom-preview.example')
    assert legacy.get('/v1/me').status_code == 401


def test_rotating_a_code_or_removing_a_member_takes_effect_immediately(team):
    client, config, _ = team
    alice, bob = client(), client()
    login(alice, CODES['alice']); login(bob, CODES['bob'])
    config.members = (PreviewMember('alice', code_hash('alice_rotated_code_00000000000000000'), ALICE.token), member(BOB))
    assert alice.get('/v1/me').status_code == 401
    assert bob.get('/v1/me').json()['subject_id'] == 'bob'
    assert login(client(), CODES['alice']).status_code == 401
    assert login(client(), 'alice_rotated_code_00000000000000000').status_code == 303
    config.members = (PreviewMember('alice', code_hash('alice_rotated_code_00000000000000000'), ALICE.token),)
    assert bob.get('/v1/me').status_code == 401


def test_logout_revokes_only_that_browser(team):
    client, _, _ = team
    first, second = client(), client()
    login(first, CODES['alice']); login(second, CODES['alice'])
    assert first.post('/preview/logout', headers=HEADERS).status_code == 204
    assert first.get('/v1/me').status_code == 401
    assert second.get('/v1/me').json()['subject_id'] == 'alice'


def test_coding_sessions_are_private_to_their_owner(team):
    client, _, _ = team
    alice, bob = client(), client()
    login(alice, CODES['alice']); login(bob, CODES['bob'])
    created = alice.post('/v1/sessions', json={'prompt': '写一个加一程序'}, headers={**HEADERS, 'Idempotency-Key': 's1'})
    assert created.status_code == 201
    session_id = created.json()['session_id']
    assert [item['session_id'] for item in alice.get('/v1/sessions').json()['items']] == [session_id]
    assert bob.get('/v1/sessions').json() == {'items': []}
    assert bob.get('/v1/sessions/' + session_id).status_code == 404
    assert bob.post(f'/v1/sessions/{session_id}/resume', json={'reason': 'x'}, headers={**HEADERS, 'Idempotency-Key': 'r1'}).status_code == 404
    assert bob.post(f'/v1/sessions/{session_id}/approve', json={'expected_digest': '0' * 64}, headers={**HEADERS, 'Idempotency-Key': 'a1'}).status_code == 404
    child = bob.post('/v1/sessions', json={'prompt': '继续', 'parent_session_id': session_id}, headers={**HEADERS, 'Idempotency-Key': 's2'})
    assert child.status_code == 404
    assert alice.get('/v1/sessions/' + session_id).status_code == 200


def test_assistant_threads_and_todos_are_per_member(team):
    client, _, _ = team
    alice, bob = client(), client()
    login(alice, CODES['alice']); login(bob, CODES['bob'])
    a, b = alice.get('/v1/assistant/thread').json(), bob.get('/v1/assistant/thread').json()
    assert (a['thread']['subject'], b['thread']['subject']) == ('alice', 'bob') and a['thread']['id'] != b['thread']['id']
    todo = alice.post('/v1/assistant/todos', json={'text': '周五前交周报'}, headers={**HEADERS, 'Idempotency-Key': 't1'})
    assert todo.status_code == 201
    assert [t['id'] for t in alice.get('/v1/assistant/todos').json()['items']] == [todo.json()['id']]
    assert bob.get('/v1/assistant/todos').json()['items'] == []
    assert bob.post('/v1/assistant/todos/' + todo.json()['id'], json={'status': 'done'}, headers={**HEADERS, 'Idempotency-Key': 't2'}).status_code == 404


def test_member_file_must_match_human_identities(tmp_path):
    path = tmp_path / 'members.json'
    row = dict(subject='alice', display='爱丽丝', code_sha256=code_hash(CODES['alice']))
    path.write_text(json.dumps(dict(schema_version=MEMBERS_SCHEMA, members=[row])))
    loaded = load_members(path, (ALICE, BOB, OPERATOR))
    assert [(m.subject, m.display, m.bearer) for m in loaded] == [('alice', '爱丽丝', ALICE.token)]
    for bad in (dict(row, subject='operator'), dict(row, subject='carol'), dict(row, code='plain')):
        path.write_text(json.dumps(dict(schema_version=MEMBERS_SCHEMA, members=[bad])))
        with pytest.raises(ValueError):
            load_members(path, (ALICE, BOB, OPERATOR))
    with pytest.raises(ValueError):
        PreviewAccess(ORIGIN, ADMIN_CODE, ALICE.token, members=(member(ALICE), member(ALICE)))


def test_manage_script_adds_rotates_and_removes_without_printing_codes(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    sys.path.insert(0, str(scripts))
    try:
        from prepare_public_preview import prepare
        (tmp_path / 'loom-sandbox-image.id').write_text('sha256:' + '1' * 64)
        prepare(tmp_path)
    finally:
        sys.path.remove(str(scripts))
    private = tmp_path / 'loom-public-preview'
    migrated = json.loads((private / 'members.json').read_text())
    assert migrated['schema_version'] == MEMBERS_SCHEMA and len(migrated['members']) == 1
    def run(*args):
        result = subprocess.run([sys.executable, str(scripts / 'manage_preview_members.py'), *args, '--work-root', str(tmp_path)],
                                capture_output=True, text=True, check=False, env={'PYTHONPATH': str(scripts.parent / 'src') + ':' + str(scripts), 'PATH': '/usr/bin:/bin'})
        return json.loads(result.stdout), result.stdout + result.stderr
    added, out = run('add', '--subject', 'xiaoming', '--display', '小明')
    assert added['passed'] and added['restart_required']
    invite = Path(added['invite_file'])
    code = invite.read_text(encoding='utf-8').split('访问码：')[1].split('\n')[0]
    assert code not in out and stat.S_IMODE(invite.stat().st_mode) == 0o600
    from loom.auth import load_identities
    identities = load_identities(private / 'identities.json')
    people = {m.subject: m for m in load_members(private / 'members.json', identities)}
    assert people['xiaoming'].code_sha256 == code_hash(code) and people['xiaoming'].display == '小明'
    assert not run('add', '--subject', 'xiaoming')[0]['passed']
    rotated, out = run('rotate', '--subject', 'xiaoming')
    new_code = invite.read_text(encoding='utf-8').split('访问码：')[1].split('\n')[0]
    assert rotated['passed'] and new_code != code and new_code not in out
    assert [m['subject'] for m in run('list')[0]['members']][-1] == 'xiaoming'
    sys.path.insert(0, str(scripts))
    try:
        from update_preview_origin import update
        update(tmp_path, 'https://new-name.trycloudflare.com')
    finally:
        sys.path.remove(str(scripts))
    lines = invite.read_text(encoding='utf-8').split('\n')
    assert lines[0] == 'https://new-name.trycloudflare.com/console' and lines[1] == '访问码：' + new_code
    assert new_code not in (private / '访问信息.txt').read_text(encoding='utf-8')
    assert run('remove', '--subject', 'xiaoming')[0]['passed'] and not invite.exists()
    assert 'xiaoming' not in [m.subject for m in load_members(private / 'members.json', load_identities(private / 'identities.json'))]
