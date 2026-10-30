"""Actual Docker source evidence to independent review of a primary report."""
import asyncio
import os
from unittest.mock import patch

import pytest

from loom.sandbox import DockerSandbox,SandboxConfig
from tests.test_reviewer_handoff import handoff
from tests.test_review_inputs import review_case
from tests.test_reviewer_verification import prepare_reviewer_verification
from tests.test_verification_jobs import run_job

pytestmark=pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')


def test_review_report_is_verified_against_actual_host_execution(review_case,tmp_path):
    sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))
    with patch('tests.test_review_inputs.FakeSandbox',return_value=sandbox):
        case=handoff.__wrapped__(review_case)
    b=prepare_reviewer_verification(case,tmp_path/'actual-primary-review')
    result,requests=asyncio.run(run_job(b))
    assert result['state']=='verified' and result['task_verified'] and len(requests)==2
    assert not result['goal_accepted']
