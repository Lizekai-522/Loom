"""Single-host, invite-only preview boundary. Never enables loopback auto-login."""
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from http.cookies import SimpleCookie, CookieError
import json
from pathlib import Path
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .store import Store, Conflict, LoomError


COOKIE = '__Host-loom-preview'
MAX_BODY = 262144
REMEMBER_SECONDS = 30 * 86400
MAX_SESSIONS = 64


class PreviewStore(Store):
    """Bound total admitted exposure durably, inside the command transaction.

    Receipts are checked by Store before this callback, so a lost response can
    still be recovered at the cap. Existing goals cannot raise their budgets.
    """
    def __init__(self, path, *, max_goals=20, cost_limit=1000000, token_limit=3200000):
        self.preview_limits = (max_goals, cost_limit, token_limit)
        super().__init__(path)

    def _command(self, tenant, key, operation, args, apply):
        if operation == 'workflow.create_goal':
            original = apply
            def apply(db):
                count, cost, tokens = self.preview_limits
                if (args['cost_limit'] > cost or args['token_limit'] > tokens
                        or db.execute('SELECT count(*) FROM runs').fetchone()[0] >= count):
                    raise Conflict('public preview goal or budget limit reached')
                return original(db)
        return super()._command(tenant, key, operation, args, apply)


@dataclass(frozen=True)
class PreviewAccess:
    origin: str
    access_code: str = field(repr=False)
    bearer: str = field(repr=False)
    session_seconds: int = REMEMBER_SECONDS

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (url.scheme != 'https' or not url.hostname or url.netloc != url.hostname
                or url.path or url.query or url.fragment
                or not re.fullmatch(r'[a-z0-9]+(?:[-.][a-z0-9]+)*', url.hostname)
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.access_code)
                or not re.fullmatch(r'[!-~]{32,4096}', self.bearer)
                or type(self.session_seconds) is not int or not 60 <= self.session_seconds <= REMEMBER_SECONDS):
            raise ValueError('PUBLIC_PREVIEW_CONFIG_INVALID')


LOGIN = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loom · 项目空间</title><link rel="stylesheet" href="/preview/style.css">
<main><p>LOOM · 受邀体验</p><h1>开始你的项目</h1>
<p>输入访问码，直接进入项目空间。与受邀测试者共用体验空间，请使用测试内容。</p>
<form method="post" action="/preview/login"><label for="code">体验访问码</label>
<input id="code" name="code" type="password" autocomplete="off" required maxlength="128">
<button type="submit">进入项目空间</button></form>
<p>验证一次后，本浏览器会记住 30 天，无需重复输入。</p>
<p>单目标预算上限 $1、3,200,000 词元；最多 20 个目标。按模型上下文上限保守预留，实际用量结算。新建会话默认自动推进，遇到预算、未知调用或验收失败才暂停。
Flash 已接通；独立复核另用操作员配置的项目额度，最多预留 $1.85，原生成额度保持。进入后可检查复核执行器状态并明确申请。</p></main></html>'''
STYLE = 'body{font:14px/1.8 system-ui,"Microsoft YaHei",sans-serif;background:#fafafa;color:#222326;margin:0}main{box-sizing:border-box;max-width:490px;margin:9vh auto;padding:36px;background:white;border:1px solid #e8e8e8;border-radius:16px}h1{font-size:28px;letter-spacing:-1px;font-weight:550;margin:16px 0}input,button{font:inherit;display:block;box-sizing:border-box;border:1px solid #ddd;border-radius:8px;padding:11px 13px;margin:12px 0;width:100%}input:focus-visible,button:focus-visible{outline:2px solid #5c78c9;outline-offset:3px}button{background:#222326;color:white;border:0;cursor:pointer}p{font-size:12px;color:#75767c}label{font-size:12px}@media(max-width:560px){main{margin:5vh 16px;padding:25px}}'


class PublicPreview:
    """ASGI guard for a separately configured backend and isolated database.

    The tunnel can reach only this guard. Client Authorization, identity and
    forwarding headers never reach the API; a server-held operator is injected
    only after an invite login and exact HTTPS origin checks. No worker routes.
    """
    def __init__(self, app, config, *, example=None,verified_example=None,revocations=None):
        self.app, self.config = app, config
        self.example = example
        self.verified_example=verified_example
        # In-memory cache of admitted device tokens. Authority is the HMAC
        # signature plus expiry, so evicting an entry never locks a device out.
        self.sessions = {}
        # Logged-out token hashes -> signed expiry. Persisted when a private
        # path is configured, so a restart cannot revive a logged-out cookie.
        self.revocations_path = None if revocations is None else Path(revocations)
        self.revoked = self._load_revocations()
        self.login_attempts = deque()
        self.writes = deque()
        self.policy = None

    def _load_revocations(self):
        if self.revocations_path is None:
            return {}
        try:
            raw = self.revocations_path.read_bytes()
            data = json.loads(raw) if len(raw) <= 1048576 else {}
            now = time.time()
            return {k: v for k, v in data.items() if re.fullmatch(r'[0-9a-f]{64}', k) and type(v) in (int, float) and v > now}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _revoke(self, hashed, expiry):
        now = time.time()
        self.revoked = {k: v for k, v in self.revoked.items() if v > now}
        self.revoked[hashed] = expiry
        self._persist()

    def _persist(self):
        if self.revocations_path is not None:
            temporary = self.revocations_path.with_name(self.revocations_path.name + '.' + secrets.token_hex(6) + '.new')
            try:
                temporary.write_text(json.dumps(self.revoked), encoding='utf-8')
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                os.replace(temporary, self.revocations_path)
            except OSError:
                pass

    def _admit(self, hashed, until):
        if len(self.sessions) >= MAX_SESSIONS:
            # Evict the entry expiring first; it is re-admitted by signature on its next request.
            self.sessions.pop(min(self.sessions, key=self.sessions.get), None)
        self.sessions[hashed] = until

    @staticmethod
    def _device_key(config):
        return sha256(('loom-preview-device/v1|' + config.origin + '|' + config.access_code + '|' + config.bearer).encode()).digest()

    def _issue(self, config):
        expiry = int(time.time()) + config.session_seconds
        body = f'v1.{expiry}.{secrets.token_urlsafe(24)}'
        return body + '.' + hmac_new(self._device_key(config), body.encode(), 'sha256').hexdigest()

    def _device_valid(self, config, token):
        """Signed, expiring device token so a remembered browser survives restarts."""
        try:
            version, expiry, nonce, mac = token.split('.')
            body = f'{version}.{expiry}.{nonce}'
            expected = hmac_new(self._device_key(config), body.encode(), 'sha256').hexdigest()
            return (version == 'v1' and compare_digest(mac, expected) and re.fullmatch(r'[A-Za-z0-9_-]{16,64}', nonce) is not None
                    and time.time() < int(expiry) <= time.time() + config.session_seconds)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def allowed(method, path):
        if method == 'GET':
            return (path in {'/', '/console', '/console/', '/console/expert', '/health', '/v1/me', '/v1/goals', '/v1/sessions', '/v1/interventions', '/v1/executor-status', '/v1/tools', '/v1/quotas', '/v1/verification-capabilities', '/v1/domain-packs'}
                    or re.fullmatch(r'/v1/domain-packs/[a-z][a-z0-9_-]{0,47}/[0-9.]{1,20}(?:/templates/[a-z][a-z0-9_-]{0,47})?', path)
                    or re.fullmatch(r'/console/assets/[A-Za-z0-9_-]+\.(?:css|js)', path)
                    or re.fullmatch(r'/v1/(?:goals|runs)/[A-Za-z0-9:_-]+(?:/[A-Za-z0-9:_-]+)*', path)
                    or re.fullmatch(r'/v1/sessions/[A-Za-z0-9_-]+', path)
                    # Read-only platform registries (agents, profiles, knowledge, memory, reports).
                    or re.fullmatch(r'/v1/(?:agents(?:/[a-z0-9_-]+)?|capability-profiles|agent-routing|tool-selection|knowledge|'
                                    r'knowledge-search|memory|memory-search|topology/report|evaluation/sampling|plugins|mcp-servers|'
                                    r'pack-rollouts/[a-z0-9_-]+(?:/resolve)?)', path))
        if method == 'PUT':
            return bool(re.fullmatch(r'/v1/goals/[A-Za-z0-9_-]+/plan', path))
        if method == 'POST':
            return (path in {'/v1/goals', '/v1/sessions'} or bool(re.fullmatch(r'/v1/sessions/[A-Za-z0-9_-]+/resume', path)) or bool(re.fullmatch(
                r'/v1/(?:goals/[A-Za-z0-9_-]+/plan/approve|runs/[A-Za-z0-9_-]+/(?:control/(?:pause|resume|terminate)|pause|resume|terminate|tasks/[A-Za-z0-9:_-]+/(?:auxiliary-inputs|execution-requests|verification-requests)|execution-requests/[A-Za-z0-9_-]+/cancel|artifacts/[A-Za-z0-9_-]+/(?:development-suites|review-decisions)))', path)))
        return False

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            if scope['type'] == 'lifespan':
                return await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        async def respond(response):
            await response(scope, receive, send)
        def denied(status, code):
            return JSONResponse({'code': code}, status_code=status)
        try:
            config = self.config()
        except (OSError, ValueError, TypeError):
            return await respond(denied(503, 'PUBLIC_PREVIEW_NOT_READY'))
        policy = sha256((config.origin + config.access_code + config.bearer).encode()).hexdigest()
        if policy != self.policy:
            # Rotation changes the HMAC key, invalidating every earlier token.
            self.sessions.clear()
            if self.policy is not None:
                self.revoked = {}
                self._persist()
            self.policy = policy
        # No trust in X-Forwarded-*; cloudflared preserves the public Host.
        if request.headers.get('host') != urlsplit(config.origin).netloc:
            return await respond(denied(421, 'PUBLIC_HOST_REQUIRED'))
        path, method = request.url.path, request.method
        headers = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
                   'Referrer-Policy':'same-origin' if path == '/preview/login' else 'no-referrer',
                   'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                   'Strict-Transport-Security':'max-age=86400'}
        async def secured_send(message):
            if message['type'] == 'http.response.start':
                names = {key.lower().encode() for key in headers}
                message = {**message, 'headers':[(k,v) for k,v in message.get('headers',[]) if k.lower() not in names]
                           + [(k.lower().encode(), v.encode()) for k,v in headers.items()]}
            await send(message)
        async def reply(response):
            await response(scope, receive, secured_send)
        if method not in {'GET','POST','PUT'}:
            return await reply(denied(405, 'PUBLIC_METHOD_NOT_ALLOWED'))
        if method != 'GET' and request.headers.get('origin') != config.origin:
            return await reply(denied(403, 'PUBLIC_ORIGIN_REQUIRED'))
        # Stream-bound before FastAPI parses any input; no unbounded body read.
        body = bytearray()
        if method != 'GET':
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    return await reply(denied(413, 'PUBLIC_BODY_LIMIT'))
        now = time.monotonic()
        # Expired cache entries simply drop out; the signed expiry rejects them anyway.
        self.sessions = {key:value for key,value in self.sessions.items() if value > now}
        if path == '/preview/style.css' and method == 'GET':
            return await reply(Response(STYLE, media_type='text/css'))
        if path == '/preview/login' and method == 'GET':
            return await reply(HTMLResponse(LOGIN))
        if path == '/preview/login' and method == 'POST':
            while self.login_attempts and self.login_attempts[0] < now - 60:
                self.login_attempts.popleft()
            if len(self.login_attempts) >= 12:
                return await reply(denied(429, 'PUBLIC_LOGIN_RATE_LIMIT'))
            self.login_attempts.append(now)
            try:
                fields = parse_qs(body.decode('ascii'), strict_parsing=True, max_num_fields=1)
                supplied = fields['code'][0]
                valid = len(fields['code']) == 1 and supplied.isascii() and compare_digest(supplied, config.access_code)
            except (ValueError, UnicodeError, KeyError):
                valid = False
            if not valid:
                return await reply(HTMLResponse(LOGIN.replace('<form ', '<p>访问码无效，请核对本机私有体验文件。</p><form '), status_code=401))
            session = self._issue(config)
            self._admit(sha256(session.encode()).hexdigest(), now + config.session_seconds)
            response = RedirectResponse('/console', status_code=303)
            response.set_cookie(COOKIE, session, secure=True, httponly=True, samesite='strict', path='/', max_age=config.session_seconds)
            return await reply(response)
        try:
            cookies = SimpleCookie(request.headers.get('cookie',''))
            session = cookies[COOKIE].value
            hashed = sha256(session.encode()).hexdigest()
            authenticated = hashed in self.sessions and hashed not in self.revoked
            if not authenticated and hashed not in self.revoked and self._device_valid(config, session):
                # Remembered device after a restart or cache eviction: re-admit for its signed remaining time.
                remaining = int(session.split('.')[1]) - time.time()
                self._admit(hashed, now + remaining)
                authenticated = True
        except (KeyError, ValueError, CookieError, IndexError):
            authenticated = False
        if not authenticated:
            response = RedirectResponse('/preview/login',status_code=303) if method == 'GET' and path in {'/','/console','/console/'} else denied(401,'PUBLIC_SESSION_REQUIRED')
            return await reply(response)
        if path == '/preview/logout' and method == 'POST':
            self.sessions.pop(hashed,None)
            try:
                expiry = int(session.split('.')[1])
            except (IndexError, ValueError):
                expiry = time.time() + config.session_seconds
            self._revoke(hashed, expiry)
            response = Response(status_code=204)
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
            return await reply(response)
        example_callback={'/preview/examples':self.example,'/preview/examples/verified':self.verified_example}.get(path)
        is_example = method == 'POST' and example_callback is not None
        if not is_example and not self.allowed(method, path):
            return await reply(denied(403, 'PUBLIC_ROUTE_NOT_ALLOWED'))
        if method != 'GET':
            while self.writes and self.writes[0] < now-60:
                self.writes.popleft()
            if len(self.writes) >= 60:
                return await reply(denied(429,'PUBLIC_WRITE_RATE_LIMIT'))
            self.writes.append(now)
        if is_example:
            key=request.headers.get('idempotency-key','')
            if not re.fullmatch(r'[A-Za-z0-9:_-]{1,160}',key) or body != b'{}':
                return await reply(denied(422,'PUBLIC_EXAMPLE_INPUT_INVALID'))
            try:
                result=example_callback(key)
            except LoomError:
                return await reply(denied(409,'PUBLIC_EXAMPLE_NOT_READY'))
            return await reply(JSONResponse(result,status_code=201))
        if path == '/':
            return await reply(RedirectResponse('/console',status_code=303))
        # Copy to avoid mutating another request. Never forward client identity.
        forwarded = {**scope, 'scheme':'https', 'state':{**scope.get('state',{}),'public_preview':True},
                     'headers':[(k,v) for k,v in scope['headers'] if k.lower() not in {b'authorization',b'x-loom-console',b'cookie'} and not k.lower().startswith((b'x-forwarded-',b'cf-'))]
                     + [(b'authorization',('Bearer '+config.bearer).encode())]}
        consumed = False
        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type':'http.request','body':bytes(body),'more_body':False}
            return await receive()
        await self.app(forwarded, replay if method != 'GET' else receive, secured_send)
