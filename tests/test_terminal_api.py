"""Terminal boundary tests and actual native PTY echo/resize/cleanup checks."""
import os
from pathlib import Path
import re
import time
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from loom.auth import Principal
from loom.terminal_api import TerminalSession, install_terminal_api, shell_environment


ALICE = Principal('a', 'alice', frozenset({'user'}))
BOB = Principal('a', 'bob', frozenset({'user'}))
OTHER = Principal('b', 'alice', frozenset({'user'}))
OPERATOR = Principal('a', 'operator', frozenset({'operator'}))
HEADERS = {'X-Loom-Console': 'local', 'Origin': 'http://127.0.0.1:8796'}


class Workspace:
    def __init__(self, root):
        self.path = root / 'terminal-project'
        self.path.mkdir()

    def project(self, identity, project_id):
        if identity != ALICE or project_id != 'project-a':
            raise HTTPException(404, 'PROJECT_NOT_FOUND')
        return dict(id=project_id, name='Terminal test', path=str(self.path))


def app_for(tmp_path, *, enabled=True):
    app = FastAPI()
    app.state.workspace = Workspace(tmp_path)

    def identity(request: Request):
        # Test dependency stands in for the application's authenticated Reader.
        return {'alice': ALICE, 'bob': BOB, 'other': OTHER, 'operator': OPERATOR}.get(
            request.headers.get('x-test-identity', 'alice'), OPERATOR)

    role = Annotated[Principal, Depends(identity)]
    install_terminal_api(app, None, Reader=role, Writer=role, local_console=enabled)
    return app


@pytest.fixture
def env(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8796', client=('127.0.0.1', 50000)) as client:
        yield app, client


def create(client):
    response = client.post('/v1/terminals', headers=HEADERS,
                           json={'project_id': 'project-a', 'cols': 100, 'rows': 28})
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_output(client, terminal_id, expected, *, after=0):
    deadline = time.monotonic() + 12
    result = ''
    while time.monotonic() < deadline:
        response = client.get('/v1/terminals/' + terminal_id + '/output', headers=HEADERS,
                              params={'after': after})
        assert response.status_code == 200
        page = response.json()
        after = page['cursor']
        result += ''.join(e['text'] for e in page['events'])
        clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', result)
        if expected in clean:
            return clean, after
        time.sleep(.05)
    raise AssertionError('PTY did not produce expected output')


def test_real_pty_persists_state_resizes_and_stops(env):
    app, client = env
    terminal = create(client)
    assert terminal['backend'] == ('conpty' if os.name == 'nt' else 'posix-pty')
    terminal_id = terminal['id']
    url = '/v1/terminals/' + terminal_id
    command = "$veriloTestValue = 41; Write-Output ($veriloTestValue + 1)\r" if os.name == 'nt' else "veriloTestValue=41; printf '%s\\n' $((veriloTestValue+1))\n"
    assert client.post(url + '/input', headers=HEADERS, json={'data': command}).status_code == 200
    _, cursor = wait_for_output(client, terminal_id, '42')
    # Concatenation ensures an echoed command cannot satisfy the assertion.
    command = "Write-Output ('PTY_' + 'OK:' + $veriloTestValue)\r" if os.name == 'nt' else "printf '%s%s:%s\\n' PTY_ OK $veriloTestValue\n"
    client.post(url + '/input', headers=HEADERS, json={'data': command})
    text, _ = wait_for_output(client, terminal_id, 'PTY_OK:41', after=cursor)
    response = client.post(url + '/resize', headers=HEADERS, json={'cols': 120, 'rows': 35})
    assert response.status_code == 200 and response.json()['cols'] == 120
    dimensions = "Write-Output ('SIZE:' + [Console]::WindowWidth + ':' + [Console]::WindowHeight)\r" if os.name == 'nt' else "stty size\n"
    client.post(url + '/input', headers=HEADERS, json={'data': dimensions})
    wait_for_output(client, terminal_id, 'SIZE:120:35' if os.name == 'nt' else '35 120')
    shell = app.state.terminals.get(ALICE, terminal_id).backend
    assert client.delete(url, headers=HEADERS).json()['state'] == 'closed'
    assert not shell.alive()
    assert client.post(url + '/input', headers=HEADERS, json={'data': 'x'}).status_code == 409
    assert client.delete(url, headers=HEADERS).status_code == 200


@pytest.mark.parametrize('identity', ['bob', 'other'])
def test_other_people_cannot_read_write_resize_or_close_terminal(env, identity):
    _, client = env
    terminal_id = create(client)['id']
    headers = {**HEADERS, 'x-test-identity': identity}
    url = '/v1/terminals/' + terminal_id
    assert client.get('/v1/terminals', headers=headers).json()['items'] == []
    assert client.get(url + '/output', headers=headers).status_code == 404
    assert client.post(url + '/input', headers=headers, json={'data': 'x'}).status_code == 404
    assert client.post(url + '/resize', headers=headers, json={'cols': 80, 'rows': 24}).status_code == 404
    assert client.delete(url, headers=headers).status_code == 404


@pytest.mark.parametrize('headers', [
    {}, {'X-Loom-Console': 'wrong'},
    {**HEADERS, 'Origin': 'https://evil.example'},
    {**HEADERS, 'Origin': 'null'},
    {**HEADERS, 'Origin': 'http://127.0.0.1:8797'},
    {**HEADERS, 'Sec-Fetch-Site': 'same-site'},
    {**HEADERS, 'Sec-Fetch-Site': 'cross-site'},
    {**HEADERS, 'X-Forwarded-For': '127.0.0.1'},
    {**HEADERS, 'x-test-identity': 'operator'},
])
def test_terminal_is_never_a_cross_origin_or_instance_shell(env, headers):
    _, client = env
    assert client.get('/v1/terminals', headers=headers).status_code == 403
    assert client.post('/v1/terminals', headers=headers,
                       json={'project_id': 'project-a'}).status_code == 403


@pytest.mark.parametrize('peer,base', [
    ('192.0.2.10', 'http://127.0.0.1:8796'),
    ('127.0.0.1', 'http://loom.example:8796'),
    ('127.0.0.1', 'http://localhost:8796'),
])
def test_remote_peer_or_nonliteral_host_cannot_open_terminal(tmp_path, peer, base):
    app = app_for(tmp_path)
    with TestClient(app, base_url=base, client=(peer, 50000)) as client:
        assert client.get('/v1/terminals', headers={'X-Loom-Console': 'local'}).status_code == 403


def test_disabled_terminal_routes_do_not_exist(tmp_path):
    app = app_for(tmp_path, enabled=False)
    with TestClient(app, base_url='http://127.0.0.1:8796', client=('127.0.0.1', 50000)) as client:
        assert client.get('/v1/terminals', headers=HEADERS).status_code == 404


def test_user_cannot_supply_a_cwd_or_invalid_dimensions(env):
    _, client = env
    for payload in [dict(project_id='project-a', cwd='C:/'),
                    dict(project_id='project-a', cols=0),
                    dict(project_id='project-a', rows=100000),
                    dict(project_id='project-a', cols=True)]:
        assert client.post('/v1/terminals', headers=HEADERS, json=payload).status_code == 422
    assert client.post('/v1/terminals', headers=HEADERS, json={'project_id': 'missing'}).status_code == 404


def test_shutdown_disconnect_releases_the_real_shell(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8796', client=('127.0.0.1', 50000)) as client:
        terminal = create(client)
        session = app.state.terminals.get(ALICE, terminal['id'])
        assert session.backend.alive()
    assert not session.backend.alive()
    assert not session.thread.is_alive()


def test_shell_exit_is_observed_and_reclaimed(env):
    app, client = env
    terminal_id = create(client)['id']
    client.post('/v1/terminals/' + terminal_id + '/input', headers=HEADERS,
                json={'data': 'exit\r' if os.name == 'nt' else 'exit\n'})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get('/v1/terminals/' + terminal_id + '/output', headers=HEADERS)
        if response.json()['state'] != 'running':
            break
        time.sleep(.05)
    assert response.json()['state'] == 'exited'
    session = app.state.terminals.get(ALICE, terminal_id)
    session.thread.join(timeout=2)
    assert not session.backend.alive() and not session.thread.is_alive()


def test_shell_does_not_inherit_application_secrets(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'terminal-fixture-not-a-real-key')
    monkeypatch.setenv('UNRECOGNIZED_NEW_PROVIDER_SECRET', 'terminal-fixture')
    monkeypatch.setenv('LOOM_TENANT_KEYS', 'terminal-fixture')
    values = shell_environment()
    assert not {'DEEPSEEK_API_KEY', 'LOOM_TENANT_KEYS', 'UNRECOGNIZED_NEW_PROVIDER_SECRET'} & values.keys()
    assert values['TERM'] == 'xterm-256color'


def test_output_buffer_is_bounded_and_reports_a_lost_cursor():
    session = TerminalSession.__new__(TerminalSession)
    import threading
    from collections import deque
    session.id, session.state = 'fixture', 'running'
    session.lock, session.events = threading.Lock(), deque()
    session.seq, session.output_chars = 0, 0
    session._append('x' * (session.max_output_chars * 2))
    result = session.output(0)
    assert session.output_chars <= session.max_output_chars
    assert result['truncated'] and len(result['events']) <= 32
