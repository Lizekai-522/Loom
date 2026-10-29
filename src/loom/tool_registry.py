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
    ('developer', 'fixer'), ('workspace.write', 'tests.run'), False, False, False,
    '仅执行批准任务的内存源码和冻结 JSON 用例；使用固定 Docker 镜像、无网容器及宿主判据。')
TOOLS = MappingProxyType({PYTHON_TESTS.tool_id: PYTHON_TESTS})


def catalog(tenant_id, config=None):
    return dict(schema_version='loom.tools/v1', tenant_id=tenant_id,
        tools=[asdict(item) for item in TOOLS.values()],
        input_contract=dict(files='bounded relative Python source strings',
            tests='one to 32 host-frozen JSON cases', entrypoint='main.py',
            schema_version='loom.python-tests-input/v1'),
        runtime_configured=bool(config is not None and config.sandbox_image),
        runtime_health_checked=False, model_dispatched=False, counts_as_success=False)


def run_python_tool(sandbox, spec, files, tests, *, tool_id='tests.run'):
    definition=TOOLS.get(tool_id)
    if definition is None:
        raise SandboxRejected('TOOL_NOT_REGISTERED')
    if (spec.get('scope')!=definition.scope or spec.get('role') not in definition.roles
        or not set(definition.required_capabilities)<=set(spec.get('tools') or ())):
        raise SandboxRejected('TOOL_CAPABILITY_DENIED')
    if sandbox is None:
        raise SandboxRejected('TOOL_RUNTIME_NOT_CONFIGURED')
    return sandbox.run_python(files, tests)
