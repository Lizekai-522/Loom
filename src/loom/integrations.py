"""MCP server/client (FR-036), declarative HTTP API plugins (FR-038) and
independent canary/rollback for domain packs (FR-049).

External calls are opt-in, host-pinned, GET-only and size/time bounded; the
platform's external-write switch stays off. MCP tools exposed by Loom go
through the same identities and schema validation (FR-037) as HTTP.
"""
from __future__ import annotations

from hashlib import sha256
import ipaddress
import json
import re
from urllib.parse import quote, urlencode, urlsplit

import httpx

from . import __version__
from .agents import check_call, validate_schema
from .platform_ledger import Ledger, conflict, require, text
from .store import Conflict, InvalidInput, LoomError, NotFound, canonical, digest
from .workflow import ROLE_SKILLS

PLUGIN_ID = re.compile(r'^[a-z][a-z0-9_-]{1,47}$')
MAX_RESPONSE = 65536
MCP_PROTOCOL = '2025-06-18'


def _https_base(url):
    parts = urlsplit(url) if isinstance(url, str) else None
    require(parts is not None and parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password
            and not parts.query and not parts.fragment and len(url) <= 500, 'base_url must be a plain https URL')
    host = parts.hostname.lower()
    # Named public hosts only: no IP literals, localhost or internal suffixes (SSRF guard).
    try:
        ipaddress.ip_address(host.strip('[]'))
        literal = True
    except ValueError:
        literal = False
    require(not literal and '.' in host and host != 'localhost'
            and not host.endswith(('.local', '.internal', '.localhost', '.lan', '.home.arpa')),
            'base_url must name a public host, not an IP address or internal name')
    return url.rstrip('/'), host


# ----- FR-038 ---------------------------------------------------------------
class HttpPlugins:
    KIND = 'http-plugin'
    CALLS = 'plugin-call'

    def __init__(self, store, *, ledger=None, transport=None):
        self.ledger = ledger or Ledger(store)
        self.transport = transport

    def register(self, tenant, key, manifest, *, actor):
        require(isinstance(manifest, dict), 'plugin manifest must be an object')
        plugin_id = manifest.get('plugin_id')
        require(isinstance(plugin_id, str) and PLUGIN_ID.fullmatch(plugin_id), 'plugin_id must be a lowercase identifier')
        base, host = _https_base(manifest.get('base_url'))
        roles = manifest.get('roles', [])
        require(isinstance(roles, list) and set(roles) <= set(ROLE_SKILLS), 'roles must be registered role names')
        endpoints = manifest.get('endpoints')
        require(isinstance(endpoints, list) and 1 <= len(endpoints) <= 20, 'one to 20 endpoints required')
        names = set()
        checked = []
        for endpoint in endpoints:
            require(isinstance(endpoint, dict), 'endpoint must be an object')
            name, method, path = endpoint.get('name'), endpoint.get('method', 'GET'), endpoint.get('path')
            require(isinstance(name, str) and PLUGIN_ID.fullmatch(name) and name not in names, 'endpoint names must be unique identifiers')
            require(method == 'GET', 'only GET endpoints are allowed while external writes are disabled')
            require(isinstance(path, str) and path.startswith('/') and '..' not in path and len(path) <= 200, 'invalid endpoint path')
            schema = endpoint.get('params', {'type': 'object', 'properties': {}, 'additionalProperties': False})
            require(not validate_schema(schema, {}) or schema.get('type') == 'object', 'params must be an object schema')
            require(not [e for e in validate_schema(schema, {}) if 'unsupported' in e], 'params schema uses unsupported keywords')
            for placeholder in re.findall(r'{([a-z_][a-z0-9_]*)}', path):
                require(placeholder in schema.get('required', []), f'path parameter {placeholder} must be required')
            names.add(name)
            checked.append(dict(name=name, method='GET', path=path, params=schema,
                                description=text(endpoint.get('description', name), 'description', 500)))
        record = dict(schema_version='loom.http-plugin/v1', plugin_id=plugin_id, version=text(manifest.get('version', '1.0.0'), 'version', 20),
                      base_url=base, host=host, roles=sorted(roles), endpoints=checked, enabled=bool(manifest.get('enabled', False)),
                      external_writes=False)
        return self.ledger.append(tenant, key, self.KIND, plugin_id, record, actor)['record']

    def list(self, tenant):
        return [r['record'] for r in self.ledger.current(tenant, self.KIND)]

    def invoke(self, tenant, key, plugin_id, endpoint_name, arguments, *, actor, dry_run=True):
        plugin = self.ledger.latest(tenant, self.KIND, plugin_id)['record']
        endpoint = next((e for e in plugin['endpoints'] if e['name'] == endpoint_name), None)
        if endpoint is None:
            raise NotFound('plugin endpoint not found')
        check_call(endpoint['params'], arguments)
        path, query = endpoint['path'], dict(arguments)
        for placeholder in re.findall(r'{([a-z_][a-z0-9_]*)}', path):
            path = path.replace('{' + placeholder + '}', quote(str(query.pop(placeholder)), safe=''))
        url = plugin['base_url'] + path + ('?' + urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in query.items()}) if query else '')
        conflict(urlsplit(url).hostname == plugin['host'], 'plugin URL left its registered host')
        if dry_run:
            return dict(plugin_id=plugin_id, endpoint=endpoint_name, method='GET', url=url, dry_run=True, called=False)
        conflict(plugin['enabled'], 'plugin is disabled; enable it in a new manifest version first')
        try:
            with httpx.Client(transport=self.transport, timeout=10.0, follow_redirects=False) as client:
                with client.stream('GET', url, headers={'Accept': 'application/json'}) as response:
                    body = b''
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_RESPONSE:
                            break
        except httpx.HTTPError:
            raise Conflict('PLUGIN_REQUEST_FAILED') from None
        truncated = len(body) > MAX_RESPONSE
        body = body[:MAX_RESPONSE]
        try:
            parsed = json.loads(body) if not truncated else None
        except ValueError:
            parsed = None
        record = dict(plugin_id=plugin_id, endpoint=endpoint_name, url_digest=digest(url), status=response.status_code,
                      body_sha256=sha256(body).hexdigest(), bytes=len(body), truncated=truncated)
        self.ledger.append(tenant, key, self.CALLS, plugin_id, record, actor)
        return dict(record, dry_run=False, called=True, json=parsed, text=None if parsed is not None else body.decode('utf-8', 'replace'))


# ----- FR-036: MCP client -----------------------------------------------------
class McpServers:
    KIND = 'mcp-server'

    def __init__(self, store, *, ledger=None, transport=None):
        self.ledger = ledger or Ledger(store)
        self.transport = transport

    def register(self, tenant, key, *, name, url, actor):
        require(isinstance(name, str) and PLUGIN_ID.fullmatch(name), 'name must be a lowercase identifier')
        base, host = _https_base(url)
        record = dict(schema_version='loom.mcp-server/v1', name=name, url=base, host=host, executable_by_roles=False)
        return self.ledger.append(tenant, key, self.KIND, name, record, actor)['record']

    def list(self, tenant):
        return [r['record'] for r in self.ledger.current(tenant, self.KIND)]

    def _rpc(self, url, method, params):
        payload = dict(jsonrpc='2.0', id=1, method=method, params=params)
        try:
            with httpx.Client(transport=self.transport, timeout=10.0, follow_redirects=False) as client:
                response = client.post(url, json=payload, headers={'Accept': 'application/json, text/event-stream',
                                                                    'MCP-Protocol-Version': MCP_PROTOCOL})
        except httpx.HTTPError:
            raise Conflict('MCP_REQUEST_FAILED') from None
        if len(response.content) > MAX_RESPONSE:
            raise Conflict('MCP_RESPONSE_TOO_LARGE')
        try:
            data = response.json()
        except ValueError:
            raise Conflict('MCP_RESPONSE_INVALID') from None
        if not isinstance(data, dict) or 'error' in data or 'result' not in data:
            raise Conflict('MCP_REMOTE_ERROR')
        return data['result']

    def discover(self, tenant, name):
        server = self.ledger.latest(tenant, self.KIND, name)['record']
        result = self._rpc(server['url'], 'tools/list', {})
        tools = result.get('tools', []) if isinstance(result, dict) else []
        return dict(server=name, tools=[dict(name=t.get('name'), description=str(t.get('description', ''))[:500],
                                             input_schema=t.get('inputSchema')) for t in tools[:100] if isinstance(t, dict)],
                    executable_by_roles=False)


# ----- FR-036: MCP server (Loom exposed as tools) ----------------------------
class McpServer:
    """JSON-RPC 2.0 handler. `tools` maps name -> (description, schema, handler(identity, args), capability)."""

    def __init__(self, tools):
        self.tools = tools

    def descriptor(self):
        return [dict(name=name, description=d, inputSchema=s) for name, (d, s, _, _) in self.tools.items()]

    def handle(self, message, identity, authorize):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return self._error(None, -32600, 'invalid request')
        request_id, method, params = message.get('id'), message['method'], message.get('params') or {}
        if 'id' not in message:
            return None  # notification
        if method == 'initialize':
            return self._result(request_id, dict(protocolVersion=MCP_PROTOCOL, capabilities=dict(tools=dict(listChanged=False)),
                                                 serverInfo=dict(name='loom', version=__version__)))
        if method == 'ping':
            return self._result(request_id, {})
        if method == 'tools/list':
            return self._result(request_id, dict(tools=self.descriptor()))
        if method == 'tools/call':
            name = params.get('name') if isinstance(params, dict) else None
            if name not in self.tools:
                return self._error(request_id, -32602, 'unknown tool')
            description, schema, handler, capability = self.tools[name]
            arguments = params.get('arguments') or {}
            errors = validate_schema(schema, arguments)
            if errors:
                return self._error(request_id, -32602, 'invalid arguments: ' + '; '.join(errors[:3]))
            if capability is not None and not authorize(identity, capability):
                return self._result(request_id, dict(content=[dict(type='text', text='permission denied')], isError=True))
            try:
                value = handler(identity, arguments)
            except LoomError as error:
                return self._result(request_id, dict(content=[dict(type='text', text=f'{error.code}: {error}')], isError=True))
            return self._result(request_id, dict(content=[dict(type='text', text=json.dumps(value, ensure_ascii=False)[:MAX_RESPONSE])],
                                                 structuredContent=value, isError=False))
        return self._error(request_id, -32601, 'method not found')

    @staticmethod
    def _result(request_id, result):
        return dict(jsonrpc='2.0', id=request_id, result=result)

    @staticmethod
    def _error(request_id, code, message):
        return dict(jsonrpc='2.0', id=request_id, error=dict(code=code, message=message))


# ----- FR-049: independent canary and rollback for domain packs ---------------
class PackRollouts:
    KIND = 'pack-rollout'

    def __init__(self, store, packs, *, ledger=None):
        self.ledger, self.packs = ledger or Ledger(store), packs

    def _versions(self, tenant, name):
        return {item['reference']['version'] for item in self.packs.catalog(tenant) if item['reference']['name'] == name}

    def state(self, tenant, name):
        try:
            return self.ledger.latest(tenant, self.KIND, name)['record']
        except NotFound:
            versions = sorted(self._versions(tenant, name))
            if not versions:
                raise
            return dict(schema_version='loom.pack-rollout/v1', pack=name, stable=versions[0], canary=None, canary_percent=0,
                        previous_stable=None, action='implicit')

    def change(self, tenant, key, name, *, action, version=None, percent=None, actor):
        current = self.state(tenant, name)
        record = dict(current)
        if action == 'canary':
            require(version in self._versions(tenant, name), 'version is not a published version of this pack')
            require(version != current['stable'], 'canary version must differ from stable')
            require(type(percent) is int and 1 <= percent <= 50, 'canary percent must be 1-50')
            record.update(canary=version, canary_percent=percent)
        elif action == 'promote':
            conflict(current['canary'] is not None, 'no canary to promote')
            record.update(previous_stable=current['stable'], stable=current['canary'], canary=None, canary_percent=0)
        elif action == 'rollback':
            if current['canary'] is not None:
                record.update(canary=None, canary_percent=0)
            else:
                conflict(current['previous_stable'] is not None, 'nothing to roll back to')
                record.update(stable=current['previous_stable'], previous_stable=current['stable'])
        else:
            raise InvalidInput('action must be canary, promote or rollback')
        record.update(action=action, schema_version='loom.pack-rollout/v1')
        return self.ledger.append(tenant, key, self.KIND, name, record, actor)['record']

    def resolve(self, tenant, name, subject):
        """Version for a new run; existing runs keep their frozen pack_ref."""
        state = self.state(tenant, name)
        bucket = int(sha256(f'{name}:{subject}'.encode()).hexdigest()[:8], 16) % 100
        use_canary = state['canary'] is not None and bucket < state['canary_percent']
        return dict(pack=name, version=state['canary'] if use_canary else state['stable'], canary=use_canary, bucket=bucket)
