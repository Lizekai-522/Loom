"""Pro review provenance to Flash fixer, with actual Docker and mock model HTTP."""
import asyncio
import json
import os

import httpx
import pytest

from loom.execution import LocalTaskRunner
from loom.model_gateway import ModelGateway,ModelRoute
from loom.quotas import QuotaService
from loom.sandbox import DockerSandbox,SandboxConfig
from tests.test_deepseek_pro import pro_case,generate_pro
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff,reviewer_claim,approve_fix
from tests.test_approved_execution import OP,WORKER
from tests.test_quotas import configure

pytestmark=pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')

def test_pro_review_to_flash_fixer_executes_real_host_tests_without_dual_sign(pro_case):
    b=pro_case;configure(b['store'])
    reviewed=asyncio.run(generate_pro(b,reviewer_claim(b)))
    approve_fix(b,reviewed['artifact'])
    params=('a',b['target'],'fix',b['saved']['digest'],b['suite'])
    preview=b['admission'].preview(*params,max_output_tokens=128)
    claim=b['admission'].claim(*params,preview['preview_digest'],operator=OP,worker=WORKER,max_output_tokens=128)
    def handler(request):
        return httpx.Response(200,json=dict(id='flash-fixer-after-pro',object='chat.completion',created=1792700000,model='deepseek-flash',
            choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content=json.dumps(dict(source='import json\nprint(json.dumps({"answer":2}))\n'))))],
            usage=dict(prompt_tokens=25,completion_tokens=10,total_tokens=35)))
    async def run():
        async with ModelGateway([ModelRoute('deepseek-flash')],transport=httpx.MockTransport(handler),environ={'DEEPSEEK_API_KEY':'pro-flash-public-integration-fixture'}) as gateway:
            return await LocalTaskRunner(b['store'],gateway,DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE']))).run_python(claim,b['suite'],max_output_tokens=128)
    result=asyncio.run(run())
    assert result['status']=='submitted' and result['hard_criteria_passed'] and not result['verified']
    quota=QuotaService(b['store']).read('a')
    assert {r['resource'] for r in quota['resources'] if r['kind']=='model'}=={'deepseek/deepseek-v4-pro','deepseek/deepseek-flash'}
    assert quota['tenant_usage']==dict(calls=3,in_flight=0)
    assert b['store'].claim_next('a',b['target'],'validator',channel='verification') is None
