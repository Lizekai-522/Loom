"""Actual Docker tests for approved reviewer -> operator -> one fixer attempt.

Both provider responses are HTTP mocks; no claim of a real model review or
independent verification. The initial implementation is a sandbox fixture;
the fixer output is actually executed and compared by the Docker host adapter.
"""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from loom.model_gateway import ModelGateway, ModelRoute
from loom.store import Conflict
from loom.review_decisions import ReviewDecisionService
from tests.test_review_inputs import review_case
from tests.test_reviewer_handoff import handoff, reviewer_claim, generate, approve_fix
from tests.test_approved_execution import OP, WORKER
from tests.test_approved_node import script


pytestmark = pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'), reason='explicit approved Docker image required')


@pytest.mark.parametrize('passes', [True, False])
def test_review_fixer_retests_original_acceptance_in_real_docker(handoff, passes, tmp_path):
    b = handoff
    # The fixture's initial source uses a sandbox double. The actual fixer
    # below must produce a real host-frozen report matching that same digest.
    reviewed = asyncio.run(generate(b, reviewer_claim(b)))
    decisions = ReviewDecisionService(b['store'])
    params = ('a', b['target'], reviewed['artifact_id'])
    preview = decisions.preview(*params)
    decision = decisions.decide(*params, 'review-decision-docker', actor='subject:operator',
        expected_preview_digest=preview['preview_digest'], decision='approve_fixer',
        reason='核对精确审查版本后批准一次原宿主验收修复', target_task_id=b['target']+':fix')
    assert decision['grant_id'] and not decision['model_dispatched']
    tests_file = tmp_path / 'host.json'
    tests_file.write_text(json.dumps({'tests': [{'name': t.name, 'input': t.input, 'expected': t.expected} for t in b['suite']]}), encoding='utf-8')
    args = SimpleNamespace(db=b['store'].path, tenant='a', run_id=b['target'], node_id='fix',
        expected_plan_digest=b['saved']['digest'], tests_file=tests_file, max_output_tokens=768,
        execute=False, preview_digest=None, identities_file=tmp_path/'not-read-identities',
        secrets_file=tmp_path/'not-read-secret', model_config=None, sandbox_image=os.environ['LOOM_SANDBOX_TEST_IMAGE'])
    args.preview_digest = asyncio.run(script.run(args))['preview_digest']; args.execute = True
    source = 'import json\nprint(json.dumps({"answer":' + ('2' if passes else '0') + '}))\n'
    prompts = []
    def response(request):
        prompts.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'fixer-docker-fixture', 'object': 'chat.completion', 'created': 1792700000,
            'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps({'source': source})}}],
            'usage': {'prompt_tokens': 25, 'completion_tokens': 10, 'total_tokens': 35}})
    gateway = ModelGateway([ModelRoute('deepseek-flash')], transport=httpx.MockTransport(response),
                           environ={'DEEPSEEK_API_KEY': 'reviewer-docker-public-fixture-only'})
    with patch.object(script, 'load_identities', return_value=(OP, WORKER)), \
         patch.object(script, 'load_project_secrets', return_value={'DEEPSEEK_API_KEY': 'reviewer-docker-public-fixture-only'}), \
         patch.object(script, 'load_model_config', return_value=([ModelRoute('deepseek-flash')], {})), \
         patch.object(script, 'ModelGateway', return_value=gateway):
        report = asyncio.run(script.run(args))
    result = report['execution']
    assert result['hard_criteria_passed'] is passes and not result['verified']
    assert result['status'] == ('submitted' if passes else 'awaiting_human')
    prompt = json.loads(prompts[0]['messages'][1]['content'])
    assert prompt['auxiliary_inputs']['items'][0]['body'] == b['body']
    assert 'secret-golden' not in json.dumps(prompt)
    assert b['store'].snapshot('a', b['target'])['reserved'] == 0
    if passes:
        assert b['store'].claim_next('a', b['target'], 'validator', channel='verification') is None
    if not passes:
        assert b['store'].snapshot('a', b['target'])['status'] == 'awaiting_human'
        with pytest.raises(Conflict): b['store'].claim_next('a', b['target'], 'another-fixer')
        with pytest.raises(Conflict): b['store'].claim_next('a', b['target'], 'validator', channel='verification')
        failures = [e for e in b['store'].events('a', b['target']) if e['type'] == 'execution.acceptance_failed']
        assert len(failures) == 1 and failures[0]['payload']['acceptance_report']['passed'] is False
