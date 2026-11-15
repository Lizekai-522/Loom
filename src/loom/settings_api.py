"""Local settings HTTP surface: every private-configuration step becomes clickable.

Installed only for the loopback console (never for public previews). Every write
requires a named human principal. The API process writes operator-approved
configuration into the private work-root files and never echoes secret values
back; structural changes take effect after the executor restart that the settings
page offers as one click, while content-level changes (model routes, secrets) are
picked up by the executor on its next turn without a restart.
"""
import json
import os
import re
import secrets as pysecrets
import subprocess
import time
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from loom.auth import is_human, named_controllers
from loom.executor_config import load_executor_config, load_host_tests
from loom.model_gateway import load_model_config
from loom.quotas import Limit, QuotaPolicy, QuotaService
from loom.store import Conflict, InvalidInput, NotFound

from monsoon.mail import MailConfig, SECRET_NAME as MAIL_SECRET_NAME
from monsoon.web import SearchConfig

from .execution import freeze_tests

IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
SUBJECT = re.compile(r'[a-z][a-z0-9_-]{1,47}')
ROLES = ('operator', 'worker', 'viewer')

# Verified provider/model combinations (see loom.model_gateway.VERIFIED_MODELS).
MODEL_PROVIDERS = {
    'deepseek': {'base_url': 'https://api.deepseek.com', 'key_env': 'DEEPSEEK_API_KEY',
                 'models': ('deepseek-flash', 'deepseek-v4-pro')},
    'dashscope': {'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'key_env': 'DASHSCOPE_API_KEY',
                  'models': ('qwen3.8-max-0902',)},
}
ROLE_ALIASES = ('analyst', 'architect', 'tester', 'developer', 'reviewer', 'fixer',
                'validator', 'planner', 'implementer', 'verifier')
SEARCH_KEY_ENV = {'bocha': 'BOCHA_API_KEY', 'tavily': 'TAVILY_API_KEY'}
RESTART_COOLDOWN = 60.0


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)


class ModelIn(Body):
    provider: str
    model: str
    api_key: str = Field(min_length=8, max_length=4096)


class SearchIn(Body):
    provider: str
    key: str | None = Field(default=None, min_length=1, max_length=4096)
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    max_results: int = Field(default=5, ge=1, le=10)


class MailIn(Body):
    provider: str
    address: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=128)
    owner_addresses: list[str] = Field(default_factory=list, max_length=8)
    password: str = Field(min_length=1, max_length=4096)
    send: bool = False
    poll_seconds: int = Field(default=60, ge=30, le=3600)
    initial_days: int = Field(default=7, ge=1, le=30)
    initial_limit: int = Field(default=100, ge=1, le=500)


class SandboxIn(Body):
    image: str = Field(min_length=71, max_length=71)


class HostTestsIn(Body):
    tests: list[dict] = Field(min_length=1, max_length=128)


class VerificationIn(Body):
    pool_limit_microusd: int = Field(ge=0, le=10_000_000)
    dashscope_key: str | None = Field(default=None, min_length=8, max_length=4096)


class QuotaLimitIn(Body):
    max_calls: int = Field(ge=0, le=10000)
    max_in_flight: int = Field(ge=0, le=256)


class QuotaIn(Body):
    window_seconds: int = Field(ge=1, le=3600)
    tenant: QuotaLimitIn
    model: QuotaLimitIn
    tool: QuotaLimitIn
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)


class MemberIn(Body):
    subject: str = Field(min_length=2, max_length=48)
    roles: list[str] = Field(min_length=1, max_length=3)
    display: str = Field(default='', max_length=40)


class RestartIn(Body):
    scope: str = 'executor'


def install(app, store, *, executor_config, identities, status, Reader, Writer, Key):
    private = executor_config.source_path.parent          # <work>/loom-private
    work = private.parent
    config_path = executor_config.source_path
    identities_path = executor_config.identities_file
    secrets_path = executor_config.secrets_file
    model_config_path = executor_config.model_config
    _restart_state = {'at': 0.0}
    with store.transaction() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS settings_rotate_receipts (
            tenant TEXT NOT NULL, key_hash TEXT NOT NULL, actor TEXT NOT NULL,
            subject TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY (tenant, key_hash))''')

    def human(identity):
        """Settings changes are decisions of the named person, never of instances."""
        if not is_human(identity):
            raise HTTPException(403, 'settings changes require a named human user session')
        return identity

    # ----- bounded private-file helpers -----------------------------------------
    def write_private(path, data):
        text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
        temporary = Path(str(path) + '.new')
        temporary.write_text(text, encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(path)

    def read_private(path, default=None):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return default

    def update_executor_config(mutate):
        """Validate the next executor configuration before it can take effect."""
        data = read_private(config_path)
        if type(data) is not dict:
            raise InvalidInput('executor configuration unreadable')
        mutate(data)
        temporary = Path(str(config_path) + '.new')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.chmod(0o600)
        try:
            load_executor_config(temporary)
        except InvalidInput as error:
            temporary.unlink(missing_ok=True)
            raise InvalidInput('CONFIG_WOULD_BE_INVALID: ' + str(error)) from None
        temporary.replace(config_path)

    def ensure_private_model_config():
        """Keep model routes in a private file; the packaged example stays pristine."""
        if model_config_path == private / 'models.json':
            return model_config_path
        target = private / 'models.json'
        if not target.exists():
            write_private(target, read_private(model_config_path, {'routes': [], 'roles': {}}))
        update_executor_config(lambda data: data.__setitem__('model_config', str(target)))
        return target

    def merge_secret(path, key, value):
        data = read_private(path, {})
        if type(data) is not dict:
            data = {}
        data[key] = value
        write_private(path, data)

    def probe_request_path():
        return private / '.probe-request'

    # ----- overview ---------------------------------------------------------------
    @app.get('/v1/settings/overview')
    def overview(identity: Reader):
        human(identity)
        try:
            saved_config = load_executor_config(config_path)
        except InvalidInput:
            raise Conflict('EXECUTOR_CONFIG_INVALID') from None
        saved_model_config_path = saved_config.model_config
        heartbeat = status.read(identity.tenant_id)
        heartbeat_ok = heartbeat.get('state') == 'recent'
        needs_restart = False
        if heartbeat_ok and isinstance(heartbeat.get('config_mtime'), (int, float)):
            try:
                needs_restart = config_path.stat().st_mtime != heartbeat['config_mtime']
            except OSError:
                needs_restart = False
        routes, roles = [], {}
        model_error = None
        try:
            routes, roles = load_model_config(saved_model_config_path)
        except Exception as error:
            model_error = getattr(error, 'code', None) or 'MODEL_CONFIG_INVALID'
        secrets_data = read_private(secrets_path, {})
        secret_names = sorted(k for k in (secrets_data or {}) if isinstance(k, str) and re.fullmatch(r'[A-Z_][A-Z0-9_]{0,127}', k))
        search = saved_config.search or {}
        mail = saved_config.mail or {}
        search_secret_present = False
        if search.get('secrets_file'):
            path = Path(search['secrets_file'])
            if not path.is_absolute():
                path = private / path
            search_secret_present = path.is_file()
        mail_secret_present = False
        if mail.get('secrets_file'):
            path = Path(mail['secrets_file'])
            if not path.is_absolute():
                path = private / path
            mail_secret_present = path.is_file()
        model = None
        if routes:
            primary = next((r for r in routes if r.alias == roles.get('analyst')), routes[0])
            model = dict(alias=primary.alias, provider=primary.provider, model=primary.model,
                         base_url=primary.base_url)
        quota = QuotaService(store)
        policy_row = None
        quotas_enabled = False
        try:
            with store.connect() as db:
                quotas_enabled = quota._enabled(db)
                policy_row = quota._policy(db, identity.tenant_id)
        except Exception:
            quotas_enabled = False
        members = read_private(identities_path, {}).get('identities', [])
        verification_dir = private / 'verification'
        return dict(
            executor=dict(state=heartbeat.get('state'), observed_age_seconds=heartbeat.get('observed_age_seconds'),
                          started_at=heartbeat.get('started_at'), config_mtime=heartbeat.get('config_mtime')),
            needs_restart=needs_restart,
            model=dict(configured=bool(routes), detail=model, error=model_error, probe=heartbeat.get('model_probe'),
                       config_live=(saved_model_config_path == model_config_path)),
            secrets=dict(present=bool(secret_names), key_names=secret_names),
            sandbox=dict(image_set=saved_config.sandbox_image is not None,
                         image=(saved_config.sandbox_image or '')[:19], probe=heartbeat.get('sandbox_probe')),
            search=dict(provider=search.get('provider', 'none'), configured=bool(search and search.get('provider', 'none') != 'none'),
                        key_present=search_secret_present),
            mail=dict(configured=bool(mail), address=mail.get('address'), send=bool(mail.get('send', False)),
                      password_present=mail_secret_present),
            verification=dict(configured=saved_config.verification_config is not None,
                              workspace_ready=verification_dir.is_dir()),
            quotas=dict(enabled=quotas_enabled,
                        revision=policy_row[1]['revision'] if policy_row else None,
                        policy=policy_row[0].__dict__ if policy_row else None),
            members=dict(count=len(members), subjects=[dict(subject=m.get('subject_id'), roles=m.get('roles'))
                                                        for m in members]),
            host_tests=dict(count=len(saved_config.host_tests)),
        )

    # ----- model and secrets --------------------------------------------------------
    @app.put('/v1/settings/model')
    def save_model(body: ModelIn, identity: Writer, idempotency_key: Key):
        human(identity)
        spec = MODEL_PROVIDERS.get(body.provider)
        if spec is None or body.model not in spec['models']:
            raise InvalidInput('provider/model must be one of the verified combinations')
        target = ensure_private_model_config()
        data = read_private(target, {'routes': [], 'roles': {}})
        routes = [dict(r) for r in data.get('routes', []) if isinstance(r, dict)]
        merge_secret(secrets_path, spec['key_env'], body.api_key)
        primary = dict(alias=body.model, provider=body.provider, base_url=spec['base_url'],
                       model=body.model, key_env=spec['key_env'], timeout_seconds=60,
                       max_response_bytes=1_048_576, max_request_bytes=262_144, max_output_tokens=8192,
                       thinking='disabled', reasoning_effort='high')
        kept = [route for route in routes if route.get('alias') != body.model]
        data['routes'] = [primary] + kept
        # Review-only models cannot execute generator contracts. Keep them as
        # separate routes; selecting Flash can configure the generator roles.
        roles = dict(data.get('roles') or {})
        if body.model == 'deepseek-flash':
            roles.update({role: primary['alias'] for role in ROLE_ALIASES})
        data['roles'] = roles
        candidate_path = Path(str(target) + '.candidate')
        write_private(candidate_path, data)
        try:
            load_model_config(candidate_path)
        except Exception as error:
            candidate_path.unlink(missing_ok=True)
            raise InvalidInput('MODEL_CONFIG_INVALID: ' + str(getattr(error, 'code', '') or '')) from None
        candidate_path.replace(target)
        # The worker caches the assistant route at startup. Bump the persisted
        # config mtime so its heartbeat exposes that a restart is needed.
        update_executor_config(lambda config: config.__setitem__('model_config', str(target)))
        return dict(saved=True, provider=body.provider, model=body.model,
                    restart_required=True,
                    detail='模型路由已保存；请重启执行器。审查模型作为独立路由，不替换生成模型。')

    @app.post('/v1/settings/model/test', status_code=202)
    def test_model(identity: Writer, idempotency_key: Key):
        human(identity)
        probe_request_path().write_text('', encoding='utf-8')
        probe_request_path().chmod(0o600)
        return dict(requested=True, detail='已请求一次真实连通性测试：执行器将在几秒内完成一次小额调用，结果出现在「模型」卡片。')

    # ----- search ---------------------------------------------------------------------
    @app.put('/v1/settings/search')
    def save_search(body: SearchIn, identity: Writer, idempotency_key: Key):
        human(identity)
        if body.provider not in ('none', 'bocha', 'tavily', 'searxng'):
            raise InvalidInput('unsupported search provider')
        candidate = dict(provider=body.provider)
        if body.provider in SEARCH_KEY_ENV:
            if not body.key:
                raise InvalidInput('this provider needs an API key')
            key_env = SEARCH_KEY_ENV[body.provider]
            candidate.update(key_env=key_env, secrets_file='search-secrets.json', max_results=body.max_results)
            merge_secret(private / 'search-secrets.json', key_env, body.key)
        elif body.provider == 'searxng':
            candidate.update(base_url=body.base_url, max_results=body.max_results)
        else:
            candidate = None
        try:
            SearchConfig.parse(candidate)
        except InvalidInput as error:
            raise InvalidInput('SEARCH_CONFIG_INVALID: ' + str(error)) from None

        def mutate(data):
            if candidate is None:
                data.pop('search', None)
            else:
                data['search'] = candidate
        update_executor_config(mutate)
        return dict(saved=True, provider=body.provider, restart_required=True,
                    detail='搜索配置已保存，重启执行器后生效。')

    # ----- mail -----------------------------------------------------------------------
    @app.put('/v1/settings/mail')
    def save_mail(body: MailIn, identity: Writer, idempotency_key: Key):
        human(identity)
        candidate = dict(provider=body.provider, address=body.address, subject=body.subject,
                         owner_addresses=body.owner_addresses, secrets_file='mail-secrets.json',
                         poll_seconds=body.poll_seconds, initial_days=body.initial_days,
                         initial_limit=body.initial_limit, send=body.send)
        try:
            MailConfig.parse(candidate)
        except InvalidInput as error:
            raise InvalidInput('MAIL_CONFIG_INVALID: ' + str(error)) from None
        merge_secret(private / 'mail-secrets.json', MAIL_SECRET_NAME, body.password)

        def mutate(data):
            data['mail'] = candidate
        update_executor_config(mutate)
        return dict(saved=True, address=body.address, send=body.send, restart_required=True,
                    detail='邮箱配置已保存，重启执行器后生效。' + ('发信已启用：每封发出前仍需你批准草稿。' if body.send else '当前只收不发。'))

    @app.delete('/v1/settings/mail')
    def remove_mail(identity: Writer, idempotency_key: Key):
        human(identity)

        def mutate(data):
            data.pop('mail', None)
        update_executor_config(mutate)
        return dict(saved=True, restart_required=True, detail='邮箱配置已移除，重启执行器后生效。')

    # ----- sandbox image ------------------------------------------------------------
    @app.put('/v1/settings/sandbox-image')
    def save_sandbox(body: SandboxIn, identity: Writer, idempotency_key: Key):
        human(identity)
        if not IMAGE.fullmatch(body.image):
            raise InvalidInput('sandbox image must be sha256:<64 hex>, as printed by docker image inspect')
        (work / 'loom-sandbox-image.id').write_text(body.image + '\n', encoding='utf-8')

        def mutate(data):
            data['sandbox_image'] = body.image
        update_executor_config(mutate)
        return dict(saved=True, image=body.image[:19], restart_required=True,
                    detail='沙箱镜像已登记，重启执行器后生效；执行器会自动做一次冻结校验。')

    # ----- host tests -----------------------------------------------------------------
    @app.post('/v1/settings/host-tests', status_code=201)
    def register_host_tests(body: HostTestsIn, identity: Writer, idempotency_key: Key):
        human(identity)
        directory = private / 'host-tests'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        candidate = directory / ('candidate-' + idempotency_key[:40] + '.json')
        write_private(candidate, {'tests': body.tests})
        try:
            tests = load_host_tests(candidate)
        except InvalidInput as error:
            candidate.unlink(missing_ok=True)
            raise InvalidInput('HOST_TESTS_INVALID: ' + str(error)) from None
        digest_value = freeze_tests(tests)[1]
        final = directory / (digest_value + '.json')
        candidate.replace(final)
        final.chmod(0o600)

        def mutate(data):
            tests_map = dict(data.get('host_tests') or {})
            tests_map[digest_value] = str(final)
            data['host_tests'] = tests_map
        update_executor_config(mutate)
        return dict(saved=True, evaluation_digest=digest_value, host_case_count=len(tests),
                    restart_required=True, detail='宿主用例已冻结登记；批准计划中的摘要一致时执行器才会使用它。')

    # ----- independent dual-model verification ---------------------------------------
    def _build_verification_directory(directory, tenant, pool_limit):
        """Mirror scripts/configure_verification.py in-process: Ed25519 signing
        keys per verified model, a secrets file for their passphrases, and a
        runtime config the executor can load. Never prints or returns secrets."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from loom.verification_config import MODELS, load_verification_config
        directory.mkdir(parents=True)
        passwords = {model: pysecrets.token_urlsafe(40) for model in MODELS}
        private_keys, public_keys = {}, {}
        for model in MODELS:
            private_keys[model] = str(directory / (model + '.private.pem'))
            public_keys[model] = str(directory / (model + '.public.pem'))
            key = Ed25519PrivateKey.generate()
            blobs = [(private_keys[model], key.private_bytes(serialization.Encoding.PEM,
                                                             serialization.PrivateFormat.PKCS8,
                                                             serialization.BestAvailableEncryption(passwords[model].encode()))),
                     (public_keys[model], key.public_key().public_bytes(serialization.Encoding.PEM,
                                                                        serialization.PublicFormat.SubjectPublicKeyInfo))]
            for name, blob in blobs:
                with Path(name).open('xb') as stream:
                    os.chmod(name, 0o600)
                    stream.write(blob)
                    stream.flush()
                    os.fsync(stream.fileno())
        secret_path = directory / 'secrets.json'
        with secret_path.open('x', encoding='utf-8') as handle:
            json.dump(passwords, handle)
        secret_path.chmod(0o600)
        config = dict(schema_version='loom.verification-runtime/v1', tenant_id=tenant,
                      identities_file=str(Path(identities_path).resolve()),
                      model_config=str(Path(model_config_path).resolve()),
                      model_secrets_file=str(Path(secrets_path).resolve()),
                      workspace=str(directory / 'jobs'), secrets_file=str(secret_path),
                      private_keys=private_keys, public_keys=public_keys, pool_limit_microusd=pool_limit)
        path = directory / 'config.json'
        with path.open('x', encoding='utf-8') as handle:
            json.dump(config, handle, indent=2)
        loaded = load_verification_config(path)
        loaded.identities()
        loaded.load_signers()
        return path

    @app.post('/v1/settings/verification', status_code=201)
    def configure_verification(body: VerificationIn, identity: Writer, idempotency_key: Key):
        human(identity)
        directory = private / 'verification'
        if executor_config.verification_config is not None or directory.exists():
            raise Conflict('verification is already configured; regenerate in a new private directory instead')
        controllers = named_controllers(identities, identity.tenant_id)
        if len(controllers) != 1:
            raise Conflict('exactly one named operator identity is required for verification configuration')
        if body.dashscope_key:
            merge_secret(secrets_path, 'DASHSCOPE_API_KEY', body.dashscope_key)
            target = ensure_private_model_config()
            data = read_private(target, {'routes': [], 'roles': {}})
            routes = [dict(r) for r in data.get('routes', []) if isinstance(r, dict)]
            if not any(r.get('provider') == 'dashscope' for r in routes):
                spec = MODEL_PROVIDERS['dashscope']
                routes.append(dict(alias='qwen3.8-max-0902', provider='dashscope', base_url=spec['base_url'],
                                   model='qwen3.8-max-0902', key_env=spec['key_env'], timeout_seconds=120,
                                   max_response_bytes=1_048_576, max_request_bytes=262_144, max_output_tokens=8192,
                                   thinking='disabled', reasoning_effort='low'))
                data['routes'] = routes
                write_private(target, data)
                load_model_config(target)
        path = _build_verification_directory(directory, identity.tenant_id, body.pool_limit_microusd)

        def mutate(data):
            data['verification_config'] = str(path)
        update_executor_config(mutate)
        return dict(saved=True, workspace=str(directory / 'jobs'), pool_limit_microusd=body.pool_limit_microusd,
                    restart_required=True,
                    detail='独立复核已生成双模型签名密钥与运行配置；重启执行器后生效。签名密钥只保存在私有目录。')

    # ----- quotas -----------------------------------------------------------------------
    @app.get('/v1/settings/quotas')
    def read_quotas(identity: Reader):
        human(identity)
        quota = QuotaService(store)
        with store.connect() as db:
            enabled = quota._enabled(db)
            current = quota._policy(db, identity.tenant_id)
        return dict(enabled=enabled, revision=current[1]['revision'] if current else 0,
                    policy=current[0].__dict__ if current else None)

    @app.put('/v1/settings/quotas')
    def save_quotas(body: QuotaIn, identity: Writer, idempotency_key: Key):
        human(identity)
        controllers = named_controllers(identities, identity.tenant_id)
        if len(controllers) != 1:
            raise Conflict('exactly one named operator identity is required to persist quotas')
        policy = QuotaPolicy(body.window_seconds,
                             Limit(**body.tenant.model_dump()), Limit(**body.model.model_dump()),
                             Limit(**body.tool.model_dump()))
        result = QuotaService(store).configure(controllers[0], policy, key=idempotency_key,
                                               expected_revision=body.expected_revision, reason=body.reason)
        return dict(saved=True, **result)

    # ----- members -----------------------------------------------------------------------
    @app.get('/v1/settings/members')
    def list_members(identity: Reader):
        human(identity)
        data = read_private(identities_path, {})
        items = [dict(subject=r.get('subject_id'), roles=r.get('roles'))
                 for r in data.get('identities', []) if isinstance(r, dict)]
        return dict(items=items)

    @app.post('/v1/settings/members', status_code=201)
    def add_member(body: MemberIn, identity: Writer, idempotency_key: Key):
        human(identity)
        if not SUBJECT.fullmatch(body.subject):
            raise InvalidInput('subject must be lowercase letters/digits/-/_ (2-48)')
        roles = [r for r in body.roles if r in ROLES]
        if not roles or len(roles) != len(body.roles):
            raise InvalidInput('roles must be a subset of operator/worker/viewer; the console requires exactly one human user, so "user" cannot be added here')
        data = read_private(identities_path, {})
        rows = data.get('identities', [])
        if any(r.get('subject_id') == body.subject for r in rows):
            raise Conflict('member already exists; use rotate to issue a new token')
        tenant = identity.tenant_id
        rows.append(dict(tenant_id=tenant, subject_id=body.subject, roles=roles,
                         token=pysecrets.token_urlsafe(32)))
        data['identities'] = rows
        data['schema_version'] = 'loom.local-identities/v1'
        invite = private / 'invites'
        invite.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = invite / (body.subject + '.txt')
        token = rows[-1]['token']
        temporary = Path(str(target) + '.new')
        temporary.write_text(f'访问令牌（Bearer，仅发给本人）：{token}\n用于 /docs 授权或 API 调用；不要转发。\n', encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(target)
        write_private(identities_path, data)
        from loom.auth import load_identities as _validate
        _validate(identities_path)
        return dict(added=body.subject, roles=roles, invite_file=str(target), restart_required=True,
                    detail='成员已写入身份文件；API 与执行器都需重启后才会认得新令牌。')

    def _console_guard(row):
        """The named human session backs the loopback console; it cannot be
        rotated or removed here, or the operator could lock themselves out."""
        if 'user' in (row.get('roles') or []):
            raise Conflict('the console human session cannot be changed here')

    @app.post('/v1/settings/members/{subject}/rotate')
    def rotate_member(subject: str, identity: Writer, idempotency_key: Key):
        human(identity)
        key_hash = sha256(idempotency_key.encode('utf-8')).hexdigest()
        with store.transaction() as db:
            receipt = db.execute('SELECT actor,subject,state FROM settings_rotate_receipts WHERE tenant=? AND key_hash=?',
                                 (identity.tenant_id, key_hash)).fetchone()
            if receipt:
                if receipt['actor'] != identity.subject_id or receipt['subject'] != subject:
                    raise Conflict('idempotency key reused for a different operation')
                if receipt['state'] == 'done':
                    return dict(rotated=subject, restart_required=True)
                raise Conflict('rotation outcome requires manual reconciliation')
            pending = db.execute("SELECT 1 FROM settings_rotate_receipts WHERE tenant=? AND subject=? AND state='pending'",
                                 (identity.tenant_id, subject)).fetchone()
            if pending:
                raise Conflict('another rotation requires reconciliation')
            db.execute('INSERT INTO settings_rotate_receipts VALUES(?,?,?,?,?)',
                       (identity.tenant_id, key_hash, identity.subject_id, subject, 'pending'))
        data = read_private(identities_path, {})
        row = next((r for r in data.get('identities', []) if r.get('subject_id') == subject), None)
        if row is None:
            raise NotFound('no such member')
        _console_guard(row)
        row['token'] = pysecrets.token_urlsafe(32)
        write_private(identities_path, data)
        invite = private / 'invites' / (subject + '.txt')
        if invite.exists():
            temporary = Path(str(invite) + '.new')
            temporary.write_text(f"访问令牌（Bearer，仅发给本人）：{row['token']}\n旧令牌在重启后立即失效。\n", encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(invite)
        with store.transaction() as db:
            db.execute("UPDATE settings_rotate_receipts SET state='done' WHERE tenant=? AND key_hash=?",
                       (identity.tenant_id, key_hash))
        return dict(rotated=subject, restart_required=True)

    @app.post('/v1/settings/members/{subject}/remove')
    def remove_member(subject: str, identity: Writer, idempotency_key: Key):
        human(identity)
        data = read_private(identities_path, {})
        rows = [r for r in data.get('identities', []) if isinstance(r, dict)]
        row = next((r for r in rows if r.get('subject_id') == subject), None)
        if row is None:
            raise NotFound('no such member')
        _console_guard(row)
        kept = [r for r in rows if r.get('subject_id') != subject]
        for role, minimum in (('worker', 1), ('operator', 1)):
            if sum(1 for r in kept if role in (r.get('roles') or [])) < minimum:
                raise Conflict(f'at least one {role} identity must remain')
        data['identities'] = kept
        write_private(identities_path, data)
        invite = private / 'invites' / (subject + '.txt')
        if invite.exists():
            invite.unlink(missing_ok=True)
        return dict(removed=subject, restart_required=True)

    # ----- executor operations --------------------------------------------------------
    @app.get('/v1/settings/executor-logs')
    def executor_logs(identity: Reader, tail: int = 200):
        human(identity)
        path = private / 'logs' / 'executor.log'
        try:
            raw = path.read_bytes()[-200_000:]
        except OSError:
            return dict(exists=False, lines=[])
        lines = raw.decode('utf-8', errors='replace').splitlines()[-max(1, min(tail, 500)):]
        return dict(exists=True, lines=lines)

    @app.post('/v1/settings/restart')
    def restart(body: RestartIn, identity: Writer, idempotency_key: Key):
        human(identity)
        now = time.time()
        if now - _restart_state['at'] < RESTART_COOLDOWN:
            raise Conflict('a restart was requested less than a minute ago')
        _restart_state['at'] = now
        executor_container = os.environ.get('MS_EXECUTOR_CONTAINER', 'loom-executor')
        try:
            result = subprocess.run(['docker', 'restart', executor_container], capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise OSError(result.stderr.strip()[:200])
        except (OSError, subprocess.TimeoutExpired) as error:
            _restart_state['at'] = 0.0
            raise Conflict('RESTART_FAILED: ' + (str(error)[:160] or 'docker CLI unavailable') +
                           '；请手动执行 docker restart ' + executor_container) from None
        api_container = os.environ.get('MS_API_CONTAINER')
        api_scheduled = False
        if body.scope == 'all' and api_container:
            subprocess.Popen(['sh', '-c', f'sleep 1 && docker restart {api_container}'],
                             start_new_session=True)
            api_scheduled = True
        return dict(restarted=True, executor=executor_container, api_restarting=api_scheduled,
                    detail='执行器已重启；心跳恢复后设置页会显示最新状态。' + ('API 容器将在 1 秒后重启。' if api_scheduled else ''))
