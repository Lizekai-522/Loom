"""HTTP routes for FR-002/003/004/005/019/027/031/032/036/037/038/049/054.

Reads need the `read` capability; registry, review, plugin and rollout writes
need a named operator (`control`); knowledge proposals and memory need
`goal.write`. Every write takes an Idempotency-Key like the rest of the API.
"""

from typing import Annotated, Any, Literal

from fastapi import Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .adaptation import agreement, deviation_bounds, repair_plan, topology_report
from .agents import AgentRegistry, CapabilityProfiles, select_tools, validate_schema
from .integrations import HttpPlugins, McpServer, McpServers, PackRollouts
from .knowledge import KnowledgeBase, Memory
from .platform_ledger import Ledger
from .store import InvalidInput


class Body_(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class KnowledgeIn(Body_):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=10)


class ReviewIn(Body_):
    decision: Literal['approved', 'rejected']
    note: str = Field(min_length=1, max_length=2000)


class MemoryIn(Body_):
    scope: Literal['session', 'task', 'tenant']
    scope_id: str | None = Field(default=None, max_length=100)
    content: str = Field(min_length=1, max_length=4000)


class RepairIn(Body_):
    expected_revision: int = Field(ge=0)
    operation: dict[str, Any]


class SamplesIn(Body_):
    samples: list[Any] = Field(min_length=1, max_length=50)


class ValidateIn(Body_):
    schema_: dict[str, Any] = Field(alias='schema')
    arguments: Any = None


class InvokeIn(Body_):
    endpoint: str = Field(min_length=1, max_length=48)
    arguments: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True


class McpServerIn(Body_):
    name: str = Field(min_length=2, max_length=48)
    url: str = Field(min_length=8, max_length=500)


class RolloutIn(Body_):
    action: Literal['canary', 'promote', 'rollback']
    version: str | None = Field(default=None, max_length=20)
    percent: int | None = Field(default=None, ge=1, le=50)


def install(app, store, *, Tenant, Reader, Writer, Controller, Key, registry_auth, sessions, workflows, packs, http_transport=None):
    ledger = Ledger(store)
    agents = AgentRegistry(store, ledger=ledger)
    profiles = CapabilityProfiles(store, ledger=ledger)
    knowledge = KnowledgeBase(store, ledger=ledger)
    memory = Memory(store, ledger=ledger)
    plugins = HttpPlugins(store, ledger=ledger, transport=http_transport)
    mcp_servers = McpServers(store, ledger=ledger, transport=http_transport)
    rollouts = PackRollouts(store, packs, ledger=ledger)

    def actor(identity):
        return 'subject:' + identity.subject_id

    # FR-002 / FR-003
    @app.get('/v1/agents')
    def list_agents(tenant: Tenant, role: str | None = None, skill: str | None = None, include_retired: bool = False):
        return {'items': agents.list(tenant, role=role, skill=skill, include_retired=include_retired)}

    @app.post('/v1/agents', status_code=201)
    def register_agent(identity: Controller, idempotency_key: Key, definition: dict[str, Any] = Body(...)):
        return agents.register(identity.tenant_id, idempotency_key, definition, actor=actor(identity))

    @app.get('/v1/agents/{agent_id}')
    def get_agent(agent_id: str, tenant: Tenant):
        return agents.get(tenant, agent_id)

    @app.get('/v1/capability-profiles')
    def capability_profiles(tenant: Tenant):
        return {'items': profiles.compute(tenant), 'source': 'execution-ledger'}

    @app.post('/v1/capability-profiles/snapshots', status_code=201)
    def snapshot_profiles(identity: Controller, idempotency_key: Key):
        return profiles.snapshot(identity.tenant_id, idempotency_key, actor=actor(identity))

    @app.get('/v1/agent-routing')
    def route_agent(tenant: Tenant, role: str):
        return profiles.route(tenant, role, agents)

    # FR-031 / FR-037
    @app.get('/v1/tool-selection')
    def tool_selection(tenant: Tenant, role: str, need: str | None = None):
        return select_tools(tenant, role, need=need, plugins=plugins.list(tenant), mcp_tools=[])

    @app.post('/v1/function-calls/validate')
    def validate_call(body: ValidateIn, tenant: Tenant):
        errors = validate_schema(body.schema_, body.arguments)
        return {'valid': not errors, 'errors': errors[:20]}

    # FR-004
    @app.get('/v1/knowledge')
    def list_knowledge(tenant: Tenant, status: Literal['proposed', 'approved', 'rejected'] | None = None):
        return {'items': knowledge.list(tenant, status=status)}

    @app.post('/v1/knowledge', status_code=201)
    def propose_knowledge(body: KnowledgeIn, identity: Writer, idempotency_key: Key):
        return knowledge.propose(identity.tenant_id, idempotency_key, title=body.title, body=body.body, tags=body.tags, actor=actor(identity))

    @app.post('/v1/knowledge/{entry_id}/review')
    def review_knowledge(entry_id: str, body: ReviewIn, identity: Controller, idempotency_key: Key):
        return knowledge.review(identity.tenant_id, idempotency_key, entry_id, decision=body.decision, note=body.note, actor=actor(identity))

    @app.get('/v1/knowledge-search')
    def search_knowledge(tenant: Tenant, q: str = Query(min_length=1, max_length=500), limit: int = Query(5, ge=1, le=20)):
        return {'items': knowledge.recall(tenant, q, limit=limit), 'approved_only': True}

    # FR-005
    @app.get('/v1/memory')
    def list_memory(tenant: Tenant, scope: Literal['session', 'task', 'tenant'] | None = None, scope_id: str | None = None):
        return {'items': memory.list(tenant, scope=scope, scope_id=scope_id)}

    @app.post('/v1/memory', status_code=201)
    def write_memory(body: MemoryIn, identity: Writer, idempotency_key: Key):
        return memory.write(identity.tenant_id, idempotency_key, scope=body.scope, scope_id=body.scope_id, content=body.content,
                            actor=actor(identity))

    @app.get('/v1/memory-search')
    def search_memory(tenant: Tenant, q: str = Query(min_length=1, max_length=500), session_id: str | None = None,
                      task_id: str | None = None, limit: int = Query(5, ge=1, le=20)):
        return {'items': memory.recall(tenant, q, session_id=session_id, task_id=task_id, limit=limit)}

    # FR-019 / FR-027 / FR-054
    @app.get('/v1/topology/report')
    def report_topology(tenant: Tenant):
        return {'items': topology_report(sessions.outcome_rows(tenant))}

    @app.post('/v1/consistency')
    def consistency(body: SamplesIn, tenant: Tenant):
        result = agreement(body.samples)
        return {**result, 'low_confidence': result['agreement'] < 0.67}

    @app.get('/v1/evaluation/sampling')
    def sampling_report(tenant: Tenant):
        reviewed, deviations = sessions.review_outcomes(tenant)
        return {**deviation_bounds(reviewed, deviations), 'sampling_rate': sessions.sampling_rate,
                'source': 'independent dual-model review outcomes of automatic sessions'}

    # FR-032
    @app.post('/v1/goals/{run_id}/plan/repairs')
    def plan_repair(run_id: str, body: RepairIn, identity: Writer, idempotency_key: Key):
        plan = workflows.get_plan(identity.tenant_id, run_id)
        if plan['definition'] is None:
            raise InvalidInput('the goal has no saved plan to repair')
        repaired = repair_plan(plan['definition'], body.operation)
        return workflows.save_plan(identity.tenant_id, run_id, idempotency_key, repaired, body.expected_revision, actor=actor(identity))

    # FR-038
    @app.get('/v1/plugins')
    def list_plugins(tenant: Tenant):
        return {'items': plugins.list(tenant)}

    @app.post('/v1/plugins', status_code=201)
    def register_plugin(identity: Controller, idempotency_key: Key, manifest: dict[str, Any] = Body(...)):
        return plugins.register(identity.tenant_id, idempotency_key, manifest, actor=actor(identity))

    @app.post('/v1/plugins/{plugin_id}/invoke')
    def invoke_plugin(plugin_id: str, body: InvokeIn, identity: Controller, idempotency_key: Key):
        return plugins.invoke(identity.tenant_id, idempotency_key, plugin_id, body.endpoint, body.arguments,
                              actor=actor(identity), dry_run=body.dry_run)

    # FR-036: MCP client registry
    @app.get('/v1/mcp-servers')
    def list_mcp(tenant: Tenant):
        return {'items': mcp_servers.list(tenant)}

    @app.post('/v1/mcp-servers', status_code=201)
    def register_mcp(body: McpServerIn, identity: Controller, idempotency_key: Key):
        return mcp_servers.register(identity.tenant_id, idempotency_key, name=body.name, url=body.url, actor=actor(identity))

    @app.get('/v1/mcp-servers/{name}/tools')
    def discover_mcp(name: str, identity: Controller):
        return mcp_servers.discover(identity.tenant_id, name)

    # FR-036: Loom as an MCP server
    obj = {'type': 'object'}
    session_schema = {'type': 'object', 'additionalProperties': False, 'required': ['session_id'],
                      'properties': {'session_id': {'type': 'string', 'minLength': 1, 'maxLength': 100}}}
    tools = {
        'loom_create_session': ('用一句话需求新建自动会话（可附验收样例）。',
                                {'type': 'object', 'additionalProperties': False, 'required': ['prompt', 'idempotency_key'],
                                 'properties': {'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                                                'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                                                'examples': {'type': 'array', 'maxItems': 10, 'items': obj}}},
                                lambda identity, a: sessions.create(identity, 'mcp:' + a['idempotency_key'], prompt=a['prompt'],
                                                                    examples=a.get('examples')), 'control'),
        'loom_get_session': ('读取会话的步骤、状态、源码与停止原因。', session_schema,
                             lambda identity, a: sessions.get(identity.tenant_id, a['session_id']), 'read'),
        'loom_list_sessions': ('列出会话线程。', {'type': 'object', 'additionalProperties': False, 'properties': {}},
                               lambda identity, a: {'items': sessions.list(identity.tenant_id)}, 'read'),
        'loom_search_knowledge': ('检索已通过独立审核的知识条目。',
                                  {'type': 'object', 'additionalProperties': False, 'required': ['query'],
                                   'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 500}}},
                                  lambda identity, a: {'items': knowledge.recall(identity.tenant_id, a['query'])}, 'read'),
        'loom_list_agents': ('列出已注册的 Agent 定义。', {'type': 'object', 'additionalProperties': False, 'properties': {}},
                             lambda identity, a: {'items': agents.list(identity.tenant_id)}, 'read'),
    }
    server = McpServer(tools)

    @app.post('/mcp', include_in_schema=False)
    async def mcp_endpoint(request: Request, identity: Reader):
        try:
            message = await request.json()
        except ValueError:
            return JSONResponse(server._error(None, -32700, 'parse error'))
        if isinstance(message, list):
            return JSONResponse(server._error(None, -32600, 'batch requests are not supported'))
        reply = server.handle(message, identity, registry_auth)
        return Response(status_code=202) if reply is None else JSONResponse(reply)

    # FR-049
    @app.get('/v1/pack-rollouts/{name}')
    def rollout_state(name: str, tenant: Tenant):
        return rollouts.state(tenant, name)

    @app.post('/v1/pack-rollouts/{name}')
    def rollout_change(name: str, body: RolloutIn, identity: Controller, idempotency_key: Key):
        return rollouts.change(identity.tenant_id, idempotency_key, name, action=body.action, version=body.version,
                               percent=body.percent, actor=actor(identity))

    @app.get('/v1/pack-rollouts/{name}/resolve')
    def rollout_resolve(name: str, tenant: Tenant, subject: str = Query(min_length=1, max_length=200)):
        return rollouts.resolve(tenant, name, subject)

    return dict(agents=agents, profiles=profiles, knowledge=knowledge, memory=memory, plugins=plugins,
                mcp_servers=mcp_servers, rollouts=rollouts, mcp=server)
