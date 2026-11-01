"""Byte-bounded reservations for contracts with an approved request byte limit."""
import json

import httpx
import pytest

from loom.model_gateway import ModelGateway, ModelRoute
from loom.model_pricing import FLASH, REQUEST_TOKEN_OVERHEAD, prompt_token_bound
from loom.store import InvalidInput
from tests.test_approved_node import node_case, execute, BODIES, script
from tests.test_execution import SOURCE


def reserved(b):
    events = b['store'].events('a', b['run'])
    return [(e['payload']['cost'], e['payload']['tokens']) for e in events if e['type'] == 'budget.reserved']


def test_bound_keeps_full_context_for_legacy_contracts_and_caps_limited_ones():
    assert prompt_token_bound({'limits': {}}, FLASH) == FLASH.context_limit
    assert prompt_token_bound({'limits': {'context_limit_bytes': 65536}}, FLASH) == 65536 + REQUEST_TOKEN_OVERHEAD
    assert prompt_token_bound({'limits': {'context_limit_bytes': 262144}}, FLASH) == 262144 + REQUEST_TOKEN_OVERHEAD
    with pytest.raises(InvalidInput):
        prompt_token_bound({'limits': {'context_limit_bytes': 10}}, FLASH)


@pytest.mark.parametrize('role', ['analyst', 'developer'])
def test_legacy_contract_reservation_is_unchanged(node_case, role):
    b = node_case(role)
    result, _ = execute(b)
    assert result['passed'] and reserved(b) == [(FLASH.cost(FLASH.context_limit, 1024), FLASH.context_limit + 1024)]


@pytest.mark.parametrize('role', ['analyst', 'developer'])
def test_limited_contract_reserves_from_the_byte_limit(node_case, role):
    b = node_case(role, context_limit=131072)
    result, requests = execute(b)
    bound = 131072 + REQUEST_TOKEN_OVERHEAD
    assert result['passed'] and reserved(b) == [(FLASH.cost(bound, 1024), bound + 1024)]
    # The bound really covers the request: bytes sent are within the approved limit.
    assert len(json.dumps(requests[0], ensure_ascii=False).encode()) <= 131072
    assert b['store'].snapshot('a', b['run'])['reserved'] == 0


def test_provider_usage_above_the_bound_is_recorded_as_overrun_not_hidden(node_case, monkeypatch):
    b = node_case('analyst', context_limit=4096)
    def handler(request):
        return httpx.Response(200, json={'id': 'overrun-fixture', 'object': 'chat.completion', 'created': 1792700000,
            'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': json.dumps(BODIES['analyst'])}}],
            'usage': {'prompt_tokens': 50000, 'completion_tokens': 10, 'total_tokens': 50010}})
    gateway = ModelGateway([ModelRoute('deepseek-flash')], transport=httpx.MockTransport(handler),
                           environ={'DEEPSEEK_API_KEY': 'overrun-public-fixture-only'})
    monkeypatch.setattr('tests.test_approved_node.gateway_for', lambda body, requests: gateway)
    result, _ = execute(b)
    assert not result['passed']
    overrun = [e['payload'] for e in b['store'].events('a', b['run']) if e['type'] == 'budget.overrun']
    assert overrun and overrun[-1]['actual_tokens'] == 50010 and overrun[-1]['run_status'] == 'awaiting_human'
    # Spend is recorded at the provider-reported amount; nothing is silently capped.
    assert b['store'].snapshot('a', b['run'])['tokens_spent'] == 50010
