"""Existing developer nodes in actual Docker; provider responses are mocks."""
import os

import pytest

from loom.artifacts import ArtifactService
from tests.test_approved_node import node_case,execute,BODIES

pytestmark=pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')


@pytest.mark.parametrize('with_input',[False,True])
def test_unified_developer_with_optional_approved_input_in_real_docker(node_case,with_input):
    b=node_case('developer',with_input=with_input)
    if with_input:
        args=b['args'];host=args.tests_file;args.tests_file=None;args.node_id='supply'
        generated,_=execute(b,BODIES['analyst'])
        artifact=ArtifactService(b['store']).get_artifact('a',b['run'],generated['execution']['artifact_id'])
        ArtifactService(b['store']).approve_input('a',b['run'],b['run']+':work',artifact['artifact_id'],'live-input',actor='subject:operator',
            expected_plan_digest=b['saved']['digest'],expected_source_epoch=1,expected_target_epoch=0,expected_revision=1,expected_content_hash=artifact['content_hash'])
        args.node_id='work';args.tests_file=host;args.execute=False
    from loom.sandbox import DockerSandbox,SandboxConfig
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    result,requests=execute(b,{'source':'import json\nprint(json.dumps({"answer":2}))\n'},sandbox=sandbox)
    assert result['passed'] and result['execution']['hard_criteria_passed'] and result['execution']['status']=='submitted'
    assert len(requests)==1
    assert b['store'].snapshot('a',b['run'])['reserved']==0
