"""Dynamic agents (FR-002), capability profiles (FR-003), tool selection
(FR-031) and function-call schema validation (FR-037).

Registered agents specialise one of the seven code-owned roles and can only
narrow its skills/tools; registration never widens authority. Profiles are
derived from the audited execution ledger, never self-reported.
"""
from __future__ import annotations

import json
import math
import re

from .platform_ledger import Ledger, conflict, require, text
from .store import InvalidInput, NotFound, digest
from .tool_registry import TOOLS
from .workflow import ROLE_SKILLS, ROLE_TOOLS

MODEL_ALIASES = frozenset({'deepseek-flash', 'deepseek-v4-pro'})
SEMVER = re.compile(r'^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$')
AGENT_ID = re.compile(r'^[a-z][a-z0-9_-]{1,47}$')


# ----- FR-037: bounded JSON-schema subset, checked before any call -------------
def validate_schema(schema, value, path='$'):
    """Return a list of violations. Supports type, properties, required,
    additionalProperties, items, enum, minLength/maxLength, minimum/maximum,
    minItems/maxItems. Unknown keywords are rejected so nothing passes silently."""
    allowed = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'minLength', 'maxLength',
               'minimum', 'maximum', 'minItems', 'maxItems', 'description', 'title'}
    if not isinstance(schema, dict) or set(schema) - allowed:
        return [f'{path}: unsupported schema']
    errors = []
    kind = schema.get('type')
    checks = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
              'string': lambda v: isinstance(v, str), 'boolean': lambda v: isinstance(v, bool),
              'integer': lambda v: type(v) is int, 'number': lambda v: type(v) in (int, float) and math.isfinite(v),
              'null': lambda v: v is None}
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if any(k not in checks for k in kinds):
            return [f'{path}: unsupported type']
        if not any(checks[k](value) for k in kinds):
            return [f'{path}: expected {"/".join(kinds)}']
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path}: not one of the allowed values')
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 1 << 30):
            errors.append(f'{path}: length out of range')
    if type(value) in (int, float) and not isinstance(value, bool):
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            errors.append(f'{path}: number out of range')
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 1 << 30):
            errors.append(f'{path}: item count out of range')
        if 'items' in schema:
            for index, item in enumerate(value[:256]):
                errors += validate_schema(schema['items'], item, f'{path}[{index}]')
    if isinstance(value, dict):
        props = schema.get('properties', {})
        for name in schema.get('required', []):
            if name not in value:
                errors.append(f'{path}.{name}: required')
        if schema.get('additionalProperties', True) is False:
            errors += [f'{path}.{name}: not allowed' for name in value if name not in props]
        for name, sub in props.items():
            if name in value:
                errors += validate_schema(sub, value[name], f'{path}.{name}')
    return errors


def check_call(schema, arguments):
    errors = validate_schema(schema, arguments)
    if errors:
        raise InvalidInput('FUNCTION_ARGUMENTS_INVALID: ' + '; '.join(errors[:5]))
    return arguments


# ----- FR-002: dynamic agent registration / discovery / versions -------------
class AgentRegistry:
    KIND = 'agent'

    def __init__(self, store, *, ledger=None):
        self.ledger = ledger or Ledger(store)

    def register(self, tenant, key, definition, *, actor):
        if not isinstance(definition, dict):
            raise InvalidInput('agent definition must be an object')
        agent_id, base, version = definition.get('agent_id'), definition.get('base_role'), definition.get('version')
        require(isinstance(agent_id, str) and AGENT_ID.fullmatch(agent_id), 'agent_id must be a lowercase identifier')
        require(base in ROLE_SKILLS, 'base_role must be one of the seven registered roles')
        require(isinstance(version, str) and SEMVER.fullmatch(version), 'version must be semantic (x.y.z)')
        skills, tools = definition.get('skills', sorted(ROLE_SKILLS[base])), definition.get('tools', sorted(ROLE_TOOLS[base]))
        require(isinstance(skills, list) and skills and set(skills) <= ROLE_SKILLS[base] and len(set(skills)) == len(skills),
                'skills may only narrow the base role')
        require(isinstance(tools, list) and set(tools) <= ROLE_TOOLS[base] and len(set(tools)) == len(tools),
                'tools may only narrow the base role')
        model = definition.get('model_alias', 'deepseek-flash')
        require(model in MODEL_ALIASES, 'unknown model alias')
        require(model != 'deepseek-v4-pro' or base == 'reviewer', 'Pro is restricted to the reviewer role')
        description = text(definition.get('description', base), 'description', 1000)
        status = definition.get('status', 'active')
        require(status in {'active', 'retired'}, 'status must be active or retired')
        # Plan v2.2 6.2: null inherits the tenant default, a value overrides it.
        domain = definition.get('execution_domain')
        require(domain in (None, 'local', 'remote'), 'execution_domain must be local, remote or null')
        extra = set(definition) - {'agent_id', 'base_role', 'version', 'skills', 'tools', 'model_alias', 'description', 'status',
                                   'execution_domain'}
        require(not extra, 'unknown agent fields: ' + ', '.join(sorted(extra)))
        record = dict(schema_version='loom.agent/v1', agent_id=agent_id, base_role=base, version=version,
                      skills=sorted(skills), tools=sorted(tools), model_alias=model, description=description, status=status)
        if domain is not None:
            record['execution_domain'] = domain

        def check(db):
            prior = self.ledger.latest_in(db, tenant, self.KIND, agent_id)
            if prior is not None:
                old = tuple(int(x) for x in prior['record']['version'].split('.'))
                conflict(tuple(int(x) for x in version.split('.')) > old,
                         'a new agent version must be greater than the current one')
        result = self.ledger.append(tenant, key, self.KIND, agent_id, record, actor, check=check)
        return dict(result, definition_digest=digest(record), authority_widened=False)

    def list(self, tenant, *, role=None, skill=None, include_retired=False):
        items = [r for r in self.ledger.current(tenant, self.KIND)
                 if (include_retired or r['record']['status'] == 'active')
                 and (role is None or r['record']['base_role'] == role)
                 and (skill is None or skill in r['record']['skills'])]
        return [dict(r['record'], seq=r['seq'], hash=r['hash'], registered_at=r['ts']) for r in items]

    def get(self, tenant, agent_id):
        found = self.ledger.latest(tenant, self.KIND, agent_id)
        return dict(found['record'], seq=found['seq'], hash=found['hash'],
                    versions=[h['record']['version'] for h in self.ledger.history(tenant, self.KIND, agent_id)])


# ----- FR-003: capability profiles from the execution ledger ----------------
def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))], 3)


class CapabilityProfiles:
    KIND = 'capability-profile'

    def __init__(self, store, *, ledger=None):
        self.store, self.ledger = store, ledger or Ledger(store)

    def compute(self, tenant):
        with self.store.connect() as db:
            rows = db.execute("SELECT envelope FROM events WHERE tenant=? AND json_extract(envelope,'$.type') IN "
                              "('execution.requested','execution.request_claimed','execution.request_finished') ORDER BY rowid",
                              (tenant,)).fetchall()
        requests = {}
        for row in rows:
            event = json.loads(row['envelope'])
            payload = event['payload']
            if event['type'] == 'execution.requested':
                record = payload['record']
                requests[record['request_id']] = dict(role=record['role'], executor=record['executor_id'], claimed=None, finished=None, state='queued')
            elif payload.get('request_id') in requests:
                item = requests[payload['request_id']]
                if event['type'] == 'execution.request_claimed':
                    item['claimed'], item['state'] = event['ts'], 'running'
                else:
                    item['finished'], item['state'] = event['ts'], payload['state']
        profiles = {}
        for item in requests.values():
            profile = profiles.setdefault(item['role'], dict(role=item['role'], requests=0, succeeded=0, failed=0, running=0, durations=[]))
            profile['requests'] += 1
            if item['state'] == 'submitted':
                profile['succeeded'] += 1
            elif item['state'] in {'blocked', 'awaiting_human', 'indeterminate'}:
                profile['failed'] += 1
            elif item['state'] in {'queued', 'running'}:
                profile['running'] += 1
            if item['claimed'] and item['finished']:
                profile['durations'].append(item['finished'] - item['claimed'])
        result = []
        for role in sorted(profiles):
            p = profiles.pop(role)
            done = p['succeeded'] + p['failed']
            durations = p.pop('durations')
            result.append(dict(p, skills=sorted(ROLE_SKILLS.get(role, ())), success_rate=round(p['succeeded'] / done, 4) if done else None,
                               p50_seconds=_percentile(durations, 0.5), p95_seconds=_percentile(durations, 0.95),
                               current_load=p['running'], samples=len(durations)))
        return result

    def snapshot(self, tenant, key, *, actor):
        profiles = self.compute(tenant)
        record = dict(schema_version='loom.capability-profiles/v1', profiles=profiles, source='execution-ledger')
        return self.ledger.append(tenant, key, self.KIND, 'tenant', record, actor)

    def route(self, tenant, role, registry):
        """Score registered agents for a role: success rate, then latency, then load."""
        require(role in ROLE_SKILLS, 'unknown role')
        profile = next((p for p in self.compute(tenant) if p['role'] == role), None)
        candidates = registry.list(tenant, role=role)
        def score(agent):
            success = profile['success_rate'] if profile and profile['success_rate'] is not None else 0.5
            latency = profile['p50_seconds'] if profile and profile['p50_seconds'] is not None else 60.0
            load = profile['current_load'] if profile else 0
            return round(success * 100 - min(latency, 600) / 10 - load * 5 + len(agent['tools']) * 0.1, 3)
        ranked = sorted(({**a, 'score': score(a)} for a in candidates), key=lambda a: (-a['score'], a['agent_id']))
        return dict(role=role, profile=profile, candidates=ranked, selected=ranked[0]['agent_id'] if ranked else None,
                    builtin_fallback=role if not ranked else None)


# ----- FR-031: dynamic tool selection over every registered tool source --------
def select_tools(tenant, role, *, need=None, plugins=(), mcp_tools=()):
    require(role in ROLE_SKILLS, 'unknown role')
    granted = ROLE_TOOLS[role]
    options = []
    for tool in TOOLS.values():
        allowed = role in tool.roles and (role == 'validator' or set(tool.required_capabilities) <= granted)
        options.append(dict(tool_id=tool.tool_id, source='builtin', allowed=allowed, network=tool.network_access,
                            reason='role capability granted' if allowed else 'role lacks required capability'))
    for plugin in plugins:
        allowed = plugin.get('enabled', False) and role in plugin.get('roles', [])
        options.append(dict(tool_id='plugin:' + plugin['plugin_id'], source='http-plugin', allowed=allowed, network=True,
                            reason='enabled plugin for role' if allowed else 'plugin disabled or role not permitted'))
    for tool in mcp_tools:
        options.append(dict(tool_id='mcp:' + tool['name'], source='mcp', allowed=False, network=True,
                            reason='external MCP tools are discoverable but not executable by task roles'))
    if need is not None:
        options = [o for o in options if o['tool_id'] == need or o['tool_id'].endswith(':' + need)]
    chosen = [o for o in options if o['allowed']]
    return dict(tenant_id=tenant, role=role, need=need, selected=chosen[0]['tool_id'] if chosen else None, options=options,
                execution_authorized=False)


def get_agent_or_builtin(registry, tenant, agent_id):
    try:
        return registry.get(tenant, agent_id)
    except NotFound:
        if agent_id in ROLE_SKILLS:
            return dict(agent_id=agent_id, base_role=agent_id, builtin=True, skills=sorted(ROLE_SKILLS[agent_id]),
                        tools=sorted(ROLE_TOOLS[agent_id]))
        raise
