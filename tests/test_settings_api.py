"""Settings hub (local console only): every private-configuration step clickable.

Covers the write paths without real model calls: overview readiness, model+key
save (strict re-validation), search/mail config updates, sandbox image and host
test registration, quota persistence, member management, executor logs, and the
restart guard. Model HTTP is never invoked here.
"""
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from loom.api import create_app
from loom.auth import LocalIdentity
from loom.executor_config import load_executor_config
from loom.model_gateway import ModelRoute
from loom.store import Store


USER = LocalIdentity('a', 'alice', frozenset({'user'}), 'settings-fixture-human-user-token-00000')
OPERATOR = LocalIdentity('a', 'ms1-operator', frozenset({'operator'}), 'settings-fixture-operator-token-0000000')
WORKER = LocalIdentity('a', 'ms1-worker', frozenset({'worker'}), 'settings-fixture-worker-token-000000000')


def write_executable_config(root: Path) -> Path:
    """A minimal valid executor config inside a private-looking work root."""
    private = root / 'loom-private'
    private.mkdir(parents=True, exist_ok=True)
    identities = private / 'ids.json'
    identities.write_text(json.dumps(dict(schema_version='loom.local-identities/v1', identities=[
        dict(tenant_id='a', subject_id='alice', roles=['user'], token=USER.token),
        dict(tenant_id='a', subject_id='ms1-operator', roles=['operator'], token=OPERATOR.token),
        dict(tenant_id='a', subject_id='ms1-worker', roles=['worker'], token=WORKER.token)])), encoding='utf-8')
    identities.chmod(0o600)
    secrets = private / 'model-secrets.json'
    secrets.write_text('{"DEEPSEEK_API_KEY": "settings-fixture-only-not-real"}\n', encoding='utf-8')
    secrets.chmod(0o600)
    models = private / 'models.json'
    models.write_text(json.dumps({'routes': [dict(alias='deepseek-flash', provider='deepseek',
                                                  base_url='https://api.deepseek.com', model='deepseek-flash',
                                                  key_env='DEEPSEEK_API_KEY')], 'roles': {'analyst': 'deepseek-flash'}}), encoding='utf-8')
    models.chmod(0o600)
    config = private / 'local-executor.json'
    config.write_text(json.dumps(dict(schema_version='loom.local-executor/v1', executor_id='settings-dev',
                                      identities_file=str(identities), secrets_file=str(secrets),
                                      model_config=str(models), sandbox_image=None, host_tests={})), encoding='utf-8')
    config.chmod(0o600)
    return config


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.private = root / 'loom-private'
        self.config_path = write_executable_config(root)
        self.store = Store(root / 'settings.db')
        self.config = load_executor_config(self.config_path)
        self.app = create_app(self.store, {}, identities=(USER, OPERATOR, WORKER),
                              local_console=True, executor_config=self.config)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8796', client=('127.0.0.1', 50000))
        self.local = lambda: {'X-Loom-Console': 'local', 'Idempotency-Key': 'settings-test'}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    # ----- surface --------------------------------------------------------------
    def test_settings_routes_absent_without_local_console(self):
        from loom.executor_config import load_executor_config as load
        config = load(self.config_path)
        app = create_app(Store(Path(self.tmp.name) / 'other.db'), {}, identities=(USER, OPERATOR, WORKER),
                         executor_config=config)
        client = TestClient(app)
        self.assertEqual(client.get('/v1/settings/overview').status_code, 404)
        self.assertEqual(client.get('/console/settings').status_code, 404)
        client.close()

    def test_overview_reports_current_readiness(self):
        data = self.client.get('/v1/settings/overview', headers=self.local()).json()
        self.assertTrue(data['model']['configured'])
        self.assertEqual(data['model']['detail']['model'], 'deepseek-flash')
        self.assertEqual(data['model']['probe'], None)          # no executor process in tests
        self.assertFalse(data['sandbox']['image_set'])
        self.assertFalse(data['search']['configured'])
        self.assertFalse(data['mail']['configured'])
        self.assertFalse(data['verification']['configured'])
        self.assertFalse(data['quotas']['enabled'])
        self.assertEqual(data['members']['count'], 3)

    def test_overview_denies_instance_principals(self):
        response = self.client.get('/v1/settings/overview',
                                   headers={'Authorization': 'Bearer ' + WORKER.token})
        self.assertEqual(response.status_code, 403)

    # ----- model + key ------------------------------------------------------------
    def test_save_model_writes_routes_and_secret_without_echo(self):
        response = self.client.put('/v1/settings/model', headers=self.local(),
                                   json={'provider': 'deepseek', 'model': 'deepseek-v4-pro', 'api_key': 'sk-test-abcdefg'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['saved'])
        models = json.loads((self.private / 'models.json').read_text(encoding='utf-8'))
        self.assertEqual(set(models['roles'].values()), {'deepseek-flash'})
        self.assertEqual(models['routes'][0]['model'], 'deepseek-v4-pro')
        secrets = json.loads((self.private / 'model-secrets.json').read_text(encoding='utf-8'))
        self.assertEqual(secrets['DEEPSEEK_API_KEY'], 'sk-test-abcdefg')
        overview = self.client.get('/v1/settings/overview', headers=self.local()).json()
        self.assertNotIn('sk-test-abcdefg', json.dumps(overview))
        self.assertEqual(overview['model']['detail']['model'], 'deepseek-flash')

    def test_save_model_rejects_unverified_combination(self):
        response = self.client.put('/v1/settings/model', headers=self.local(),
                                   json={'provider': 'openai', 'model': 'gpt-x', 'api_key': 'sk-test-abcdefg'})
        self.assertEqual(response.status_code, 422)

    def test_save_model_dashscope_preserves_generator_and_verifier_routes(self):
        response = self.client.put('/v1/settings/model', headers=self.local(),
                                   json={'provider': 'dashscope', 'model': 'qwen3.8-max-0902', 'api_key': 'sk-dash-test-12345'})
        self.assertEqual(response.status_code, 200, response.text)
        models = json.loads((self.private / 'models.json').read_text(encoding='utf-8'))
        self.assertEqual(models['routes'][0]['provider'], 'dashscope')
        self.assertEqual(set(models['roles'].values()), {'deepseek-flash'})
        self.assertEqual({r['alias'] for r in models['routes']}, {'deepseek-flash', 'qwen3.8-max-0902'})

    def test_model_test_writes_probe_request_only(self):
        response = self.client.post('/v1/settings/model/test', headers=self.local(), json={})
        self.assertEqual(response.status_code, 202)
        self.assertTrue((self.private / '.probe-request').exists())

    def test_rotation_same_key_does_not_replace_token_again(self):
        result = self.client.post('/v1/settings/members', headers=self.local(),
                                  json={'subject': 'member1', 'roles': ['viewer']})
        self.assertEqual(result.status_code, 201)
        headers = {'X-Loom-Console': 'local', 'Idempotency-Key': 'rotate-unique'}
        first = self.client.post('/v1/settings/members/member1/rotate', headers=headers, json={})
        self.assertEqual(first.status_code, 200)
        identities = json.loads((self.private / 'ids.json').read_text())
        token = next(r['token'] for r in identities['identities'] if r['subject_id'] == 'member1')
        second = self.client.post('/v1/settings/members/member1/rotate', headers=headers, json={})
        self.assertEqual(second.status_code, 200)
        identities = json.loads((self.private / 'ids.json').read_text())
        self.assertEqual(token, next(r['token'] for r in identities['identities'] if r['subject_id'] == 'member1'))

    # ----- search / mail ------------------------------------------------------------
    def test_save_search_bocha_requires_key_and_writes_files(self):
        missing = self.client.put('/v1/settings/search', headers=self.local(), json={'provider': 'bocha'})
        self.assertEqual(missing.status_code, 422)
        response = self.client.put('/v1/settings/search', headers=self.local(),
                                   json={'provider': 'bocha', 'key': 'bocha-key-0001'})
        self.assertEqual(response.status_code, 200, response.text)
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(config['search']['provider'], 'bocha')
        secret = json.loads((self.private / 'search-secrets.json').read_text(encoding='utf-8'))
        self.assertEqual(secret['BOCHA_API_KEY'], 'bocha-key-0001')

    def test_save_search_none_removes_the_section(self):
        self.client.put('/v1/settings/search', headers=self.local(), json={'provider': 'bocha', 'key': 'bocha-key-0001'})
        response = self.client.put('/v1/settings/search', headers=self.local(), json={'provider': 'none'})
        self.assertEqual(response.status_code, 200)
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertNotIn('search', config)

    def test_search_overview_uses_saved_config_and_rejects_unknown_provider(self):
        response = self.client.put('/v1/settings/search', headers=self.local(),
                                   json={'provider': 'searxng', 'base_url': 'https://example.com'})
        self.assertEqual(response.status_code, 200)
        overview = self.client.get('/v1/settings/overview', headers=self.local()).json()
        self.assertEqual(overview['search']['provider'], 'searxng')
        response = self.client.put('/v1/settings/search', headers=self.local(),
                                   json={'provider': 'typo'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(json.loads(self.config_path.read_text())['search']['provider'], 'searxng')

    def test_save_mail_validates_and_stores_password_privately(self):
        bad = self.client.put('/v1/settings/mail', headers=self.local(),
                              json={'provider': 'aliyun-qiye', 'address': 'not-an-address', 'subject': 'alice', 'password': 'pw-123456'})
        self.assertEqual(bad.status_code, 422)
        response = self.client.put('/v1/settings/mail', headers=self.local(),
                                   json={'provider': 'aliyun-qiye', 'address': 'agent@example.com', 'subject': 'alice',
                                         'owner_addresses': ['alice@example.com'], 'password': 'pw-123456', 'send': False})
        self.assertEqual(response.status_code, 200, response.text)
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(config['mail']['address'], 'agent@example.com')
        secret = json.loads((self.private / 'mail-secrets.json').read_text(encoding='utf-8'))
        self.assertEqual(secret['MAIL_PASSWORD'], 'pw-123456')

    # ----- sandbox / host tests --------------------------------------------------------
    def test_sandbox_image_requires_immutable_digest(self):
        bad = self.client.put('/v1/settings/sandbox-image', headers=self.local(), json={'image': 'loom-sandbox:release'})
        self.assertEqual(bad.status_code, 422)
        digest = 'sha256:' + 'a' * 64
        response = self.client.put('/v1/settings/sandbox-image', headers=self.local(), json={'image': digest})
        self.assertEqual(response.status_code, 200)
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(config['sandbox_image'], digest)

    def test_host_tests_freeze_and_register(self):
        tests = {'tests': [dict(name='case-1', input='{"text":"a b"}', expected='{"words":2}')]}
        response = self.client.post('/v1/settings/host-tests', headers=self.local(), json=tests)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body['host_case_count'], 1)
        self.assertRegex(body['evaluation_digest'], r'[0-9a-f]{64}')
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertIn(body['evaluation_digest'], config['host_tests'])
        registered = Path(config['host_tests'][body['evaluation_digest']])
        self.assertTrue(registered.is_file())
        again = self.client.post('/v1/settings/host-tests', headers=self.local(), json=tests)
        self.assertEqual(again.status_code, 201)

    def test_host_tests_reject_invalid_payload(self):
        response = self.client.post('/v1/settings/host-tests', headers=self.local(), json={'tests': [{'oops': 1}]})
        self.assertEqual(response.status_code, 422)

    # ----- quotas ----------------------------------------------------------------------
    def test_quotas_configure_then_report_revision(self):
        policy = {'window_seconds': 60,
                  'tenant': {'max_calls': 30, 'max_in_flight': 4},
                  'model': {'max_calls': 20, 'max_in_flight': 2},
                  'tool': {'max_calls': 30, 'max_in_flight': 4},
                  'expected_revision': 0, 'reason': 'settings test'}
        saved = self.client.put('/v1/settings/quotas', headers=self.local(), json=policy)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()['changed'])
        current = self.client.get('/v1/settings/quotas', headers=self.local()).json()
        self.assertTrue(current['enabled'])
        self.assertEqual(current['revision'], 1)
        replay = self.client.put('/v1/settings/quotas', headers={**self.local(), 'Idempotency-Key': 'settings-test'}, json=policy)
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.json()['changed'])

    # ----- members ----------------------------------------------------------------------
    def test_member_add_lists_and_rotate_writes_invite(self):
        response = self.client.post('/v1/settings/members', headers=self.local(),
                                    json={'subject': 'tester01', 'roles': ['worker'], 'display': '测试'})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body['invite_file'])
        invite = Path(body['invite_file'])
        self.assertTrue(invite.is_file())
        # The token must never appear in any API response.
        listed = self.client.get('/v1/settings/members', headers=self.local())
        self.assertNotIn(invite.read_text(encoding='utf-8').split('：')[1].split('\n')[0], listed.text)
        listed = listed.json()
        self.assertIn('tester01', [m['subject'] for m in listed['items']])
        rotated = self.client.post('/v1/settings/members/tester01/rotate', headers=self.local(), json={})
        self.assertEqual(rotated.status_code, 200)
        removed = self.client.post('/v1/settings/members/tester01/remove', headers=self.local(), json={})
        self.assertEqual(removed.status_code, 200)
        listed = self.client.get('/v1/settings/members', headers=self.local()).json()
        self.assertNotIn('tester01', [m['subject'] for m in listed['items']])

    def test_member_cannot_add_human_role_or_remove_last_worker(self):
        bad = self.client.post('/v1/settings/members', headers=self.local(),
                               json={'subject': 'human2', 'roles': ['user']})
        self.assertEqual(bad.status_code, 422)
        removed = self.client.post('/v1/settings/members/ms1-worker/remove', headers=self.local(), json={})
        self.assertEqual(removed.status_code, 409)
        console = self.client.post('/v1/settings/members/alice/remove', headers=self.local(), json={})
        self.assertEqual(console.status_code, 409)
        rotate = self.client.post('/v1/settings/members/alice/rotate', headers=self.local(), json={})
        self.assertEqual(rotate.status_code, 409)

    # ----- logs + restart ------------------------------------------------------------------
    def test_executor_logs_read_private_file(self):
        logs = self.private / 'logs'
        logs.mkdir(exist_ok=True)
        (logs / 'executor.log').write_text('line-1\nline-2\n', encoding='utf-8')
        data = self.client.get('/v1/settings/executor-logs', headers=self.local()).json()
        self.assertTrue(data['exists'])
        self.assertEqual(data['lines'][-2:], ['line-1', 'line-2'])
        missing = self.client.get('/v1/settings/executor-logs',
                                  headers={**self.local(), 'Idempotency-Key': 'settings-test2'}).json()
        self.assertTrue(missing['exists'])

    def test_restart_reports_docker_failure_without_raising(self):
        import os
        os.environ['MS_EXECUTOR_CONTAINER'] = 'definitely-not-a-container'
        try:
            response = self.client.post('/v1/settings/restart', headers=self.local(), json={'scope': 'executor'})
            self.assertEqual(response.status_code, 409)
            self.assertIn('RESTART_FAILED', str(response.json().get('detail', '')))
        finally:
            os.environ.pop('MS_EXECUTOR_CONTAINER', None)


class ChatLaneFailureTests(unittest.TestCase):
    """The conversation lane must say why no model answered, never queue forever."""

    def setUp(self):
        import asyncio
        self.tmp = tempfile.TemporaryDirectory()
        store = Store(Path(self.tmp.name) / 'chat.db')
        app = create_app(store, {}, identities=(USER, OPERATOR, WORKER), local_console=True)
        self.client = TestClient(app, base_url='http://127.0.0.1:8796', client=('127.0.0.1', 50000))
        thread = self.client.get('/v1/assistant/thread', headers={'X-Loom-Console': 'local'}).json()['thread']
        response = self.client.post(f"/v1/assistant/threads/{thread['id']}/messages",
                                    json={'text': 'hello'}, headers={'X-Loom-Console': 'local', 'Idempotency-Key': 'c1'})
        self.assertEqual(response.status_code, 201)
        from monsoon.runtime import AssistantRuntime
        from monsoon.store import AssistantStore

        def broken_factory():
            raise RuntimeError('model config missing')

        self.runtime = AssistantRuntime(store, gateway_factory=broken_factory)
        self.data = AssistantStore(store)
        asyncio.run(self.runtime.chat_once())
        self.events = self.data.events('a', thread['id'], after_seq=0, limit=100)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_turn_marked_failed_with_notice(self):
        # Later events override earlier ones: the initial 'message' row is queued,
        # the failure arrives as a subsequent update in the same event log.
        latest = {}
        for e in self.events['events']:
            d = e['data']
            if e['type'] in ('message', 'message.updated') and d.get('id'):
                latest[d['id']] = d
        user = [m for m in latest.values() if m.get('role') == 'user']
        self.assertEqual(user[0]['status'], 'failed')
        notices = [e['data'] for e in self.events['events'] if e['data'].get('kind') == 'notice']
        self.assertTrue(notices)
        self.assertIn('设置', notices[0]['text'])
        self.assertIn('MODEL_NOT_CONFIGURED', json.dumps(notices[0].get('meta', {})))


if __name__ == '__main__':
    unittest.main()
