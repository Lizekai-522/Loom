"""Curated plugin API via MockTransport only; never sends real HTTP requests."""
import httpx
import pytest

from tests.test_assistant import env, local, bearer, OPERATOR, WORKER


@pytest.fixture
def market(env):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'name': 'example', 'contact': 'alice@example.com'})
    env['app'].state.platform['plugins'].transport = httpx.MockTransport(respond)
    return env, requests


def test_catalog_install_and_registration_are_real_and_idempotent(market):
    env, requests = market
    client = env['client']
    catalog = client.get('/v1/market/catalog', headers=local())
    assert catalog.status_code == 200
    entries = {item['id']: item for item in catalog.json()['items']}
    assert set(entries) == {'github-public', 'npm-public'}
    assert entries['github-public']['installed'] is False
    path = '/v1/market/github-public/install'
    one = client.post(path, headers=local('install-github'))
    again = client.post(path, headers=local('install-github'))
    assert one.status_code == 201 and one.json() == again.json()
    assert one.json()['base_url'] == 'https://api.github.com'
    assert one.json()['external_writes'] is False
    assert env['app'].state.platform['plugins'].list('a')[0]['enabled'] is True
    assert client.get('/v1/market/catalog', headers=local()).json()['items'][0]['installed'] is True
    assert requests == []


def test_enable_disable_gate_invocation_and_result_is_redacted(market):
    env, requests = market
    client = env['client']
    client.post('/v1/market/github-public/install', headers=local('install'))
    disabled = client.patch('/v1/market/github-public', headers=local('disable'), json={'enabled': False})
    assert disabled.status_code == 200 and disabled.json()['enabled'] is False
    body = {'arguments': {'owner': 'deepseek-ai', 'repo': 'deepseek-harness'}}
    path = '/v1/market/github-public/invoke'
    assert client.post(path, headers=local('blocked-call'), json=body).status_code == 409
    assert requests == []
    enabled = client.patch('/v1/market/github-public', headers=local('enable'), json={'enabled': True})
    assert enabled.status_code == 200 and enabled.json()['enabled'] is True
    result = client.post(path, headers=local('call'), json=body)
    assert result.status_code == 200
    assert result.json()['called'] is True and result.json()['status'] == 200
    assert result.json()['json']['name'] == 'example'
    assert 'alice@example.com' not in result.text
    assert len(requests) == 1
    assert requests[0].url.host == 'api.github.com'
    assert requests[0].url.path == '/repos/deepseek-ai/deepseek-harness'


@pytest.mark.parametrize('arguments', [
    {}, {'owner': 'x'}, {'owner': 'x', 'repo': 'y', 'url': 'http://127.0.0.1'},
    {'owner': 5, 'repo': 'x'}, {'owner': '', 'repo': 'x'},
    {'owner': 'x' * 101, 'repo': 'x'},
])
def test_invalid_arguments_never_reach_http_transport(market, arguments):
    env, requests = market
    env['client'].post('/v1/market/github-public/install', headers=local('install'))
    result = env['client'].post('/v1/market/github-public/invoke', headers=local('call'), json={'arguments': arguments})
    assert result.status_code == 422
    assert requests == []


@pytest.mark.parametrize('identity', [OPERATOR, WORKER])
def test_market_mutations_require_named_human_user(market, identity):
    env, requests = market
    client = env['client']
    client.post('/v1/market/github-public/install', headers=local('install-user'))
    assert client.post('/v1/market/github-public/install', headers=bearer(identity, 'denied-install')).status_code == 403
    assert client.patch('/v1/market/github-public', headers=bearer(identity, 'denied-enable'), json={'enabled': False}).status_code == 403
    assert client.post('/v1/market/github-public/invoke', headers=bearer(identity, 'denied-call'), json={'arguments': {'owner': 'x', 'repo': 'y'}}).status_code == 403
    assert requests == []


def test_unknown_and_uninstalled_plugins_and_extra_fields_are_refused(market):
    env, requests = market
    client = env['client']
    assert client.post('/v1/market/arbitrary/install', headers=local('unknown')).status_code == 404
    assert client.post('/v1/market/npm-public/invoke', headers=local('not-installed'), json={'arguments': {'package': 'npm'}}).status_code == 404
    assert client.patch('/v1/market/npm-public', headers=local('not-installed-enable'), json={'enabled': True}).status_code == 404
    client.post('/v1/market/npm-public/install', headers=local('npm-install'))
    assert client.patch('/v1/market/npm-public', headers=local('bad-bool'), json={'enabled': 'true'}).status_code == 422
    assert client.post('/v1/market/npm-public/invoke', headers=local('extra-field'), json={'arguments': {'package': 'npm'}, 'base_url': 'http://127.0.0.1'}).status_code == 422
    assert requests == []


def test_transport_errors_are_sanitized_and_redirects_are_not_followed(market):
    env, requests = market
    client = env['client']
    client.post('/v1/market/npm-public/install', headers=local('npm-install'))
    plugins = env['app'].state.platform['plugins']
    def timeout(request):
        raise httpx.ReadTimeout('fixture-private-transport-error', request=request)
    plugins.transport = httpx.MockTransport(timeout)
    response = client.post('/v1/market/npm-public/invoke', headers=local('timeout'), json={'arguments': {'package': 'npm'}})
    assert response.status_code == 409
    assert 'fixture-private-transport-error' not in response.text
    redirect_requests = []
    def redirect(request):
        redirect_requests.append(request)
        return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
    plugins.transport = httpx.MockTransport(redirect)
    result = client.post('/v1/market/npm-public/invoke', headers=local('redirect'), json={'arguments': {'package': '@scope/name'}})
    assert result.status_code == 200 and result.json()['status'] == 302
    assert len(redirect_requests) == 1 and redirect_requests[0].url.host == 'registry.npmjs.org'
