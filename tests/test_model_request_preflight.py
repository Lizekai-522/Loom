"""Exact pre-dispatch body bounds, without provider credentials or real HTTP."""
import json
import unittest
from unittest.mock import patch

import httpx

from loom.model_gateway import GatewayError, ModelGateway, ModelRoute


def reply():
    return {"id": "preflight-fixture", "object": "chat.completion", "created": 1792700000,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "fixture answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}


class RequestPreflightTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self, *, route=None, environ=None, calls=None):
        def handler(request):
            if calls is not None:
                calls.append(request)
            return httpx.Response(200, json=reply())
        result = ModelGateway([route or ModelRoute("flash")],
                              transport=httpx.MockTransport(handler), environ=environ or {})
        self.addAsyncCleanup(result.aclose)
        return result

    async def test_preflight_never_reads_key_and_wire_matches_for_all_envelopes(self):
        messages = [{"role": "system", "content": "Platform contract."},
                    {"role": "user", "content": '中文\\\"\n\t\u0000 and untrusted text.'}]
        for thinking in ("disabled", "enabled"):
            for json_mode in (False, True):
                with self.subTest(thinking=thinking, json_mode=json_mode):
                    calls = []
                    gateway = self.gateway(route=ModelRoute("flash", thinking=thinking), calls=calls,
                                           environ={"DEEPSEEK_API_KEY": "preflight-fixture-key-only"})
                    with patch.object(gateway, "_key", side_effect=AssertionError("credential lookup in preflight")):
                        raw = gateway.prepare_request("flash", messages, 20, json_mode=json_mode)
                    self.assertEqual(calls, [])
                    self.assertNotIn(b"preflight-fixture-key-only", raw)
                    self.assertEqual(json.loads(raw)["messages"], messages)
                    await gateway.generate("flash", messages, 20, json_mode=json_mode)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0].content, raw)

    async def test_exact_request_limit_includes_escaped_text_and_envelope(self):
        messages = [{"role": "user", "content": '汉字\\\"\n\t' * 80}]
        measuring = self.gateway()
        raw = measuring.prepare_request("flash", messages, 20, json_mode=True)
        self.assertGreater(len(raw), len(messages[0]["content"].encode("utf-8")))
        boundary = self.gateway(route=ModelRoute("flash", max_request_bytes=len(raw)))
        self.assertEqual(boundary.prepare_request("flash", messages, 20, json_mode=True), raw)
        too_small = self.gateway(route=ModelRoute("flash", max_request_bytes=len(raw) - 1))
        with self.assertRaises(GatewayError) as caught:
            too_small.prepare_request("flash", messages, 20, json_mode=True)
        self.assertEqual(caught.exception.code, "MODEL_REQUEST_TOO_LARGE")

    async def test_invalid_input_is_static_and_credential_free(self):
        gateway = self.gateway()
        for messages, limit in (([{"role": "tool", "content": "forbidden"}], 20),
                                ([{"role": "user", "content": "\ud800"}], 20),
                                ([{"role": "user", "content": "data", "url": "file:///ignored"}], 20),
                                ([{"role": "user", "content": "data"}], True)):
            with self.subTest(messages_type=type(messages).__name__, limit_type=type(limit).__name__):
                with self.assertRaises(GatewayError) as caught:
                    gateway.prepare_request("flash", messages, limit)
                self.assertEqual(caught.exception.code, "INVALID_MODEL_INPUT")
