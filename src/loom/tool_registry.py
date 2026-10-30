"""Code-owned adapters; registration never grants a task execution authority."""
from dataclasses import asdict, dataclass
from types import MappingProxyType

from .sandbox import SandboxRejected


@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    version: str
    label: str
    scope: str
    roles: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    network_access: bool
    host_files_access: bool
    automatic_retry: bool
    description: str


PYTHON_TESTS = ToolDefinition(
    'tests.run', 'loom.python-tests/v1', 'Python 隔离测试', 'local-workspace',
    ('developer', 'fixer', 'validator'), ('workspace.write', 'tests.run'), False, False, False,
    '仅执行批准任务的内存源码和冻结 JSON 用例；使用固定 Docker 镜像、无网容器及宿主判据。')
TOOLS = MappingProxyType({PYTHON_TESTS.tool_id: PYTHON_TESTS})


def catalog(tenant_id, config=None):
    return dict(schema_version='loom.tools/v1', tenant_id=tenant_id,
        tools=[{**asdict(item),'role_requirements':{
            role:list(('workspace.read','tests.run','artifacts.read','artifacts.write') if role=='validator' else item.required_capabilities)
            for role in item.roles}} for item in TOOLS.values()],
        input_contract=dict(files='bounded relative Python source strings',
            tests='one to 32 host-frozen JSON cases', entrypoint='main.py',
            schema_version='loom.python-tests-input/v1'),
        runtime_configured=bool(config is not None and config.sandbox_image),
        runtime_health_checked=False, model_dispatched=False, counts_as_success=False)


def run_python_tool(sandbox, spec, files, tests, *, tool_id='tests.run',validation_input=None):
    definition=TOOLS.get(tool_id)
    if definition is None:
        raise SandboxRejected('TOOL_NOT_REGISTERED')
    validator=spec.get('role')=='validator'
    capabilities=('workspace.read','tests.run','artifacts.read','artifacts.write') if validator else definition.required_capabilities
    if (spec.get('scope')!=definition.scope or spec.get('role') not in definition.roles
        or not set(capabilities)<=set(spec.get('tools') or ())):
        raise SandboxRejected('TOOL_CAPABILITY_DENIED')
    if validator:
        from .store import digest
        if (not spec.get('validation_source') or not validation_input
                or validation_input['subject']['schema_version']!='loom.review-subject-ref/v1'
                or validation_input['subject']['task_id']!=spec['run_id']+':'+spec['validation_source']['source_node']
                or files!=validation_input['files'] or digest(files)!=validation_input['subject']['source_sha256']):
            raise SandboxRejected('VALIDATION_SOURCE_CHANGED')
    if sandbox is None:
        raise SandboxRejected('TOOL_RUNTIME_NOT_CONFIGURED')
    return sandbox.run_python(files, tests)
