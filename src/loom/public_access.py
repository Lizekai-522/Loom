"""Single-host, invite-only preview boundary. Never enables loopback auto-login."""
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest
from http.cookies import SimpleCookie, CookieError
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .store import Store, Conflict, LoomError


COOKIE = '__Host-loom-preview'
MAX_BODY = 262144


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
    session_seconds: int = 14400

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (url.scheme != 'https' or not url.hostname or url.netloc != url.hostname
                or url.path or url.query or url.fragment
                or not re.fullmatch(r'[a-z0-9]+(?:[-.][a-z0-9]+)*', url.hostname)
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.access_code)
                or not re.fullmatch(r'[!-~]{32,4096}', self.bearer)
                or type(self.session_seconds) is not int or not 60 <= self.session_seconds <= 14400):
            raise ValueError('PUBLIC_PREVIEW_CONFIG_INVALID')


LOGIN = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loom 公网体验</title><link rel="stylesheet" href="/preview/style.css">
<main><p>LOOM · 临时公网体验</p><h1>进入项目工作台</h1>
<p>本机提供真实模型与隔离执行。与受邀测试者共用体验空间，请使用测试内容。</p>
<form method="post" action="/preview/login"><label for="code">体验访问码</label>
<input id="code" name="code" type="password" autocomplete="off" required maxlength="128">
<button type="submit">进入工作台</button></form>
<p>单目标预算上限 $1、3,200,000 词元；最多 20 个目标。按模型上下文上限保守预留，实际用量结算。执行前仍须明确确认。
Flash 已接通，独立双模型验收待配置。</p></main></html>'''
STYLE = 'body{font:16px/1.7 system-ui;background:#f6f5f1;color:#24292b;margin:0}main{max-width:620px;margin:10vh auto;padding:32px}input,button{font:inherit;display:block;box-sizing:border-box;padding:12px;margin:12px 0;width:100%}button{background:#255943;color:white;border:0;cursor:pointer}p{color:#48544d}'


class PublicPreview:
    """ASGI guard for a separately configured backend and isolated database.

    The tunnel can reach only this guard. Client Authorization, identity and
    forwarding headers never reach the API; a server-held operator is injected
    only after an invite login and exact HTTPS origin checks. No worker routes.
    """
    def __init__(self, app, config, *, example=None):
        self.app, self.config = app, config
        self.example = example
        self.sessions = {}
        self.login_attempts = deque()
        self.writes = deque()
        self.policy = None

    @staticmethod
    def allowed(method, path):
        if method == 'GET':
            return (path in {'/', '/console', '/console/', '/health', '/v1/me', '/v1/goals', '/v1/interventions', '/v1/executor-status'}
                    or re.fullmatch(r'/console/assets/[A-Za-z0-9_-]+\.(?:css|js)', path)
                    or re.fullmatch(r'/v1/(?:goals|runs)/[A-Za-z0-9:_-]+(?:/[A-Za-z0-9:_-]+)*', path))
        if method == 'PUT':
            return bool(re.fullmatch(r'/v1/goals/[A-Za-z0-9_-]+/plan', path))
        if method == 'POST':
            return (path == '/v1/goals' or bool(re.fullmatch(
                r'/v1/(?:goals/[A-Za-z0-9_-]+/plan/approve|runs/[A-Za-z0-9_-]+/(?:control/(?:pause|resume|terminate)|pause|resume|terminate|tasks/[A-Za-z0-9:_-]+/(?:auxiliary-inputs|execution-requests)|execution-requests/[A-Za-z0-9_-]+/cancel|artifacts/[A-Za-z0-9_-]+/(?:development-suites|review-decisions)))', path)))
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
            self.sessions.clear()
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
            if len(self.sessions) >= 16:
                return await reply(denied(429, 'PUBLIC_SESSION_LIMIT'))
            session = secrets.token_urlsafe(32)
            self.sessions[sha256(session.encode()).hexdigest()] = now + config.session_seconds
            response = RedirectResponse('/console', status_code=303)
            response.set_cookie(COOKIE, session, secure=True, httponly=True, samesite='strict', path='/', max_age=config.session_seconds)
            return await reply(response)
        try:
            cookies = SimpleCookie(request.headers.get('cookie',''))
            session = cookies[COOKIE].value
            authenticated = sha256(session.encode()).hexdigest() in self.sessions
        except (KeyError, ValueError, CookieError):
            authenticated = False
        if not authenticated:
            response = RedirectResponse('/preview/login',status_code=303) if method == 'GET' and path in {'/','/console','/console/'} else denied(401,'PUBLIC_SESSION_REQUIRED')
            return await reply(response)
        if path == '/preview/logout' and method == 'POST':
            self.sessions.pop(sha256(session.encode()).hexdigest(),None)
            response = Response(status_code=204)
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
            return await reply(response)
        is_example = path == '/preview/examples' and method == 'POST' and self.example is not None
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
                result=self.example(key)
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
