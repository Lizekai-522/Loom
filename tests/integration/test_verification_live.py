"""Actual Docker evidence to two simulated model reviews and real signed Gate-V."""
import asyncio
import os
from dataclasses import replace

import pytest

from loom.sandbox import DockerSandbox,SandboxConfig
from loom.verification_jobs import VerificationJobs
from tests.test_verification_jobs import verification_case,run_job
from tests.test_approved_node import node_case,execute
from tests.test_approved_execution import OP

pytestmark=pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')

@pytest.mark.parametrize('negative',[False,True])
def test_real_host_evidence_and_crypto_pair_release_or_reject(verification_case,negative):
    # Build a separate real candidate; no fake host report is promoted to real.
    b=verification_case;root=b['directory'].parent/'actual-host';root.mkdir()
    source=node_case.__wrapped__(root)('developer')
    report,_=execute(source,sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE'])))
    assert report['execution']['hard_criteria_passed']
    jobs=VerificationJobs(source['store']);task=source['run']+':work';preview=jobs.preview('a',source['run'],task)
    request=jobs.request(OP,source['run'],task,'actual-verify',expected_preview_digest=preview['preview_digest'],reason='实际Docker证据后的独立复核')
    b.update(store=source['store'],run=source['run'],jobs=jobs,job=request['job_id'],task=task,preview=preview,directory=root/'private-verify')
    result,requests=asyncio.run(run_job(b,negative=negative))
    assert len(requests)==2 and result['state']==('disputed' if negative else 'verified')
    snapshot=source['store'].snapshot('a',source['run'])
    assert snapshot['status']==('awaiting_human' if negative else 'succeeded')
    assert snapshot['tasks'][0]['status']==('submitted' if negative else 'done')
