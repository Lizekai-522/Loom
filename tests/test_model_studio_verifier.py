"""Second verifier model from another vendor (Alibaba Cloud Model Studio); simulated HTTP only."""
import asyncio
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from loom.model_gateway import GatewayError, ModelGateway, ModelRoute, load_model_config
from loom.model_pricing import (ACCEPTED_VERIFIER_PAIRS, QWEN, REVIEW_ONLY_MODELS, VERIFIER_MODELS, role_tariff)
from loom.store import InvalidInput
from loom.verification_signatures import MODELS, TrustedVerifier

QWEN_ID = 'qwen3.8-max-0902'
KEYS = {'DEEPSEEK_API_KEY': 'deepseek-fixture-only', 'DASHSCOPE_API_KEY': 'dashscope-fixture-only'}


def qwen_route(**overrides):
    values = dict(provider='dashscope', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1', model=QWEN_ID,
                  key_env='DASHSCOPE_API_KEY')
    return ModelRoute(QWEN_ID, **{**values, **overrides})


def completion(model, content='{"ok":true}'):
    return httpx.Response(200, json={'id': 'chatcmpl-qwen-1', 'object': 'chat.completion', 'created': 1792700000, 'model': model,
                                     'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}],
                                     'usage': {'prompt_tokens': 30, 'completion_tokens': 5, 'total_tokens': 35,
                                               'prompt_tokens_details': {'cached_tokens': 0}}})


def test_verifier_pair_is_cross_vendor_and_the_old_pair_stays_readable():
    assert VERIFIER_MODELS == ('deepseek-flash', QWEN_ID) and MODELS == frozenset(VERIFIER_MODELS)
    assert frozenset({'deepseek-flash', 'deepseek-v4-pro'}) in ACCEPTED_VERIFIER_PAIRS
    assert frozenset({'deepseek-v4-pro', QWEN_ID}) not in ACCEPTED_VERIFIER_PAIRS
    assert QWEN_ID in REVIEW_ONLY_MODELS


def test_only_the_beijing_compatible_endpoint_is_accepted():
    qwen_route()
    qwen_route(base_url='https://ws-abc123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/')
    for url in ('https://dashscope-intl.aliyuncs.com/compatible-mode/v1',            # other region, other key
                'https://ws-abc.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1',
                'https://dashscope.aliyuncs.com/api/v1',                              # native API, not compatible mode
                'http://dashscope.aliyuncs.com/compatible-mode/v1',
                'https://dashscope.aliyuncs.com.evil.example/compatible-mode/v1',
                'https://api.deepseek.com'):
        with pytest.raises(GatewayError):
            qwen_route(base_url=url)
    with pytest.raises(GatewayError):
        ModelRoute('flash', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')   # DeepSeek route on another host
    with pytest.raises(GatewayError):
        qwen_route(thinking='enabled', reasoning_effort='max')                           # DeepSeek-only effort value
    qwen_route(thinking='enabled', reasoning_effort='xhigh')


def test_request_uses_model_studio_fields_and_checks_identity():
    sent = []

    def handler(request):
        sent.append((str(request.url), request.headers['authorization'], json.loads(request.content)))
        return completion(QWEN_ID)

    async def run():
        async with ModelGateway([qwen_route()], transport=httpx.MockTransport(handler), environ=KEYS) as gateway:
            assert gateway.readiness(QWEN_ID).ready
            return await gateway.generate(QWEN_ID, [{'role': 'user', 'content': 'Return JSON please'}], 512, json_mode=True)
    result = asyncio.run(run())
    url, auth, body = sent[0]
    assert url == 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
    assert auth == 'Bearer dashscope-fixture-only'                                     # never the DeepSeek key
    assert body['enable_thinking'] is False and body['max_completion_tokens'] == 512
    assert 'thinking' not in body and 'max_tokens' not in body and body['response_format'] == {'type': 'json_object'}
    assert result.provider == 'dashscope' and result.model_id == QWEN_ID and result.usage.total_tokens == 35

    async def mismatch():
        async with ModelGateway([qwen_route()], transport=httpx.MockTransport(lambda r: completion('qwen3.8-flash')),
                                environ=KEYS) as gateway:
            await gateway.generate(QWEN_ID, [{'role': 'user', 'content': 'hi'}], 64)
    with pytest.raises(GatewayError) as error:
        asyncio.run(mismatch())
    assert error.value.code == 'MODEL_IDENTITY_MISMATCH'                                # a silently substituted model fails


def test_json_mode_without_the_word_json_fails_before_any_request():
    calls = []

    async def run():
        async with ModelGateway([qwen_route()], transport=httpx.MockTransport(lambda r: calls.append(r) or completion(QWEN_ID)),
                                environ=KEYS) as gateway:
            await gateway.generate(QWEN_ID, [{'role': 'user', 'content': '只返回结构化结果'}], 64, json_mode=True)
    with pytest.raises(GatewayError) as error:
        asyncio.run(run())
    assert error.value.code == 'INVALID_MODEL_INPUT' and calls == []


def test_qwen_tariff_bounds_the_yuan_price_and_is_reviewer_only():
    # CNY 12 / 36 per million tokens; any exchange rate above 6.5 CNY per USD is covered.
    assert QWEN.cost(1_000_000, 0) * 6.5 >= 12_000_000 and QWEN.cost(0, 1_000_000) * 6.5 >= 36_000_000
    assert role_tariff({'role': 'reviewer', 'model_alias': QWEN_ID}, qwen_route()) is QWEN
    with pytest.raises(InvalidInput):
        role_tariff({'role': 'analyst', 'model_alias': QWEN_ID})
    with pytest.raises(InvalidInput):
        role_tariff({'role': 'reviewer', 'model_alias': QWEN_ID}, ModelRoute(QWEN_ID, model='deepseek-flash'))


def test_example_config_carries_the_second_vendor_route_and_keeps_roles():
    routes, roles = load_model_config('configs/models.dual.example.json')
    by_alias = {route.alias: route for route in routes}
    assert by_alias[QWEN_ID].provider == 'dashscope' and by_alias[QWEN_ID].key_env == 'DASHSCOPE_API_KEY'
    assert by_alias[QWEN_ID].thinking == 'disabled' and roles['reviewer'] == 'deepseek-v4-pro'


def test_worker_refuses_the_old_same_vendor_pair(tmp_path):
    from loom.verification_jobs import VerificationJobs
    from loom.verification_worker import VerificationWorker
    from loom.store import Store
    from tests.test_approved_execution import OP, WORKER
    old = ('deepseek-flash', 'deepseek-v4-pro')
    signing = {model: Ed25519PrivateKey.generate() for model in old}
    trusted = {model: TrustedVerifier(model, 'verifier:' + model, model, key.public_key()) for model, key in signing.items()}
    gateway = ModelGateway([ModelRoute(model, model=model) for model in old], environ=KEYS)
    try:
        with pytest.raises(InvalidInput):
            VerificationWorker(VerificationJobs(Store(tmp_path / 'x.db')), gateway, directory=tmp_path / 'jobs', operator=OP,
                               worker=WORKER, signing_keys=signing, trusted_keys=trusted)
    finally:
        asyncio.run(gateway.aclose())
