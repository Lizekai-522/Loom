"""Version-pinned pack executes in actual Docker; model HTTP is simulated."""
import os

import pytest

from loom.domain_packs import DomainPackService
from loom.sandbox import DockerSandbox, SandboxConfig
from tests.test_approved_node import node_case, execute

pytestmark = pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')


def test_published_domain_pack_frozen_into_actual_execution(node_case):
    case = node_case('developer',domain_pack=True)
    sandbox = DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    result,requests = execute(case,sandbox=sandbox)
    assert len(requests) == 1 and result['passed']
    assert result['execution']['hard_criteria_passed']
    assert not result['counts_as_success']
    spec = case['workflow'].get_task_spec('a',case['run'],case['run']+':work')
    pack = DomainPackService(case['store']).get('a','live-software','1.0.0')
    assert spec['pack_ref'] == pack['reference']
    assert case['store'].snapshot('a',case['run'])['reserved'] == 0
