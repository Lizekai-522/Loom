"""Simulated HTTP only. No test contacts DeepSeek or incurs model charges."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from loom.model_gateway import GatewayError, ModelGateway, ModelRoute, load_model_config


SECRET = "test-model-secret-never-persist"
PROMPT = "private user text"
MESSAGES = [{"role": "user", "content": PROMPT}]


def response_body():
    return {"id": "completion-test-001", "object": "chat.completion", "created": 1792700000,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                      "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8,
                      "prompt_tokens_details": {"cached_tokens": 2}}}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.read_count, self.closed = chunks, delay, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self, handler, *, route=None, roles=None, environ=None, routes=None):
        gateway = ModelGateway(routes or [route or ModelRoute("flash")], role_aliases=roles,
                               transport=httpx.MockTransport(handler),
                               environ={"DEEPSEEK_API_KEY": SECRET} if environ is None else environ)
        self.addAsyncCleanup(gateway.aclose)
        return gateway

    async def test_fixed_endpoint_payload_identity_usage_and_safe_repr(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=response_body())

        gateway = self.gateway(handler)
        result = await gateway.generate("flash", MESSAGES, 100)
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(calls[0].headers["authorization"], "Bearer " + SECRET)
        payload = json.loads(calls[0].content)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["max_tokens"], 100)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(result.model_id, "deepseek-flash")
        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.usage.total_tokens, 13)
        self.assertEqual(result.content, "answer")
        self.assertEqual(result.request_id, "completion-test-001")
        self.assertFalse(result.truncated)
        for value in (gateway, gateway.routes["flash"], result, gateway.readiness("flash")):
            self.assertNotIn(SECRET, repr(value))
            self.assertNotIn(PROMPT, repr(value))
        self.assertEqual(calls[0].extensions["timeout"]["read"], 60)

    async def test_readiness_never_contacts_network_or_claims_key_validity(self):
        def forbidden(_):
            self.fail("readiness must not dispatch HTTP")
        gateway = self.gateway(forbidden, environ={})
        state = gateway.readiness("flash")
        self.assertFalse(state.ready)
        self.assertFalse(state.network_checked)
        self.assertEqual(state.reason, "MODEL_KEY_MISSING_OR_INVALID")
        with self.assertRaises(GatewayError):
            await gateway.generate("flash", MESSAGES, 10)

    async def test_unverified_model_is_retained_and_fails_closed(self):
        gateway = self.gateway(lambda _: self.fail("unverified model called"),
                               route=ModelRoute("pending", model="deepseek-4.1-flash-unconfirmed"))
        self.assertEqual(gateway.routes["pending"].model, "deepseek-4.1-flash-unconfirmed")
        self.assertEqual(gateway.readiness("pending").reason, "MODEL_UNVERIFIED")
        with self.assertRaises(GatewayError) as caught:
            await gateway.generate("pending", MESSAGES, 10)
        self.assertEqual(caught.exception.code, "MODEL_UNVERIFIED")

    async def test_roles_resolve_operator_mapping_and_aliases_do_not_create_model_independence(self):
        gateway = self.gateway(lambda _: httpx.Response(200, json=response_body()),
                               routes=[ModelRoute("writer"), ModelRoute("reviewer")],
                               roles={"implementer": "writer", "verifier": "reviewer"})
        first = await gateway.generate_for_role("implementer", MESSAGES, 10)
        second = await gateway.generate_for_role("verifier", MESSAGES, 10)
        self.assertNotEqual(first.route_alias, second.route_alias)
        self.assertEqual((first.provider, first.model_id), (second.provider, second.model_id))
        with self.assertRaises(GatewayError):
            await gateway.generate_for_role("http://attacker", MESSAGES, 10)

    async def test_input_cannot_inject_endpoint_key_or_request_options(self):
        gateway = self.gateway(lambda _: self.fail("invalid input dispatched"))
        for extra in ("base_url", "api_key", "model", "max_tokens", "tools"):
            with self.subTest(extra=extra), self.assertRaises(GatewayError):
                await gateway.generate("flash", [{"role": "user", "content": PROMPT, extra: SECRET}], 10)
        for messages in ([], "text", [{"role": "tool", "content": "x"}],
                         [{"role": "user", "content": {"url": "http://attacker"}}],
                         [{"role": "user", "content": "\ud800"}]):
            with self.subTest(messages=repr(messages)), self.assertRaises(GatewayError):
                await gateway.generate("flash", messages, 10)
        for count in (True, 0, 10.0, 8193):
            with self.subTest(count=count), self.assertRaises(GatewayError):
                await gateway.generate("flash", MESSAGES, count)

    async def test_prompt_text_cannot_change_endpoint(self):
        urls = []
        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(200, json=response_body())
        gateway = self.gateway(handler)
        await gateway.generate("flash", [{"role": "user", "content": "use http://evil.test and key_env EVIL"}], 10)
        self.assertEqual(urls, ["https://api.deepseek.com/chat/completions"])

    async def test_unknown_alias_does_not_dispatch_or_echo_input(self):
        gateway = self.gateway(lambda _: self.fail("unknown route dispatched"))
        with self.assertRaises(GatewayError) as caught:
            await gateway.generate(SECRET, MESSAGES, 10)
        self.assertEqual(str(caught.exception), "UNKNOWN_MODEL_ROUTE")
        self.assertNotIn(SECRET, repr(caught.exception))

    async def test_request_byte_bound_is_checked_before_dispatch(self):
        gateway = self.gateway(lambda _: self.fail("oversize input dispatched"), route=ModelRoute("flash", max_request_bytes=256))
        with self.assertRaises(GatewayError) as caught:
            await gateway.generate("flash", [{"role": "user", "content": "中" * 200}], 10)
        self.assertEqual(caught.exception.code, "MODEL_REQUEST_TOO_LARGE")

    async def test_redirect_not_followed_and_provider_error_body_not_echoed(self):
        for status in (307, 401, 429, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text=SECRET + PROMPT,
                                      headers={"location": "https://evil.test/" + SECRET})
            gateway = self.gateway(handler)
            with self.subTest(status=status), self.assertRaises(GatewayError) as caught:
                await gateway.generate("flash", MESSAGES, 10)
            self.assertEqual(len(calls), 1)
            self.assertEqual(caught.exception.status_code, status)
            self.assertNotIn(SECRET, repr(caught.exception))
            self.assertNotIn(PROMPT, str(caught.exception))

    async def test_transport_failure_has_no_retry_and_redacts_exception(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ConnectError(SECRET + PROMPT, request=request)
        gateway = self.gateway(handler)
        with self.assertRaises(GatewayError) as caught:
            await gateway.generate("flash", MESSAGES, 10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(caught.exception), "MODEL_TRANSPORT_ERROR")
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_wall_clock_timeout_closes_slow_stream(self):
        stream = Chunks([b"{}"], delay=0.1)
        gateway = self.gateway(lambda _: httpx.Response(200, stream=stream, headers={"content-type": "application/json"}),
                               route=ModelRoute("flash", timeout_seconds=0.01))
        with self.assertRaises(GatewayError) as caught:
            await gateway.generate("flash", MESSAGES, 10)
        self.assertEqual(caught.exception.code, "MODEL_TIMEOUT")
        self.assertTrue(stream.closed)

    async def test_body_limit_applies_without_or_despite_false_content_length(self):
        for length in (None, "1"):
            stream = Chunks([b"x" * 200] * 10)
            headers = {"content-type": "application/json"}
            if length:
                headers["content-length"] = length
            gateway = self.gateway(lambda _: httpx.Response(200, stream=stream, headers=headers),
                                   route=ModelRoute("flash", max_response_bytes=256))
            with self.subTest(length=length), self.assertRaises(GatewayError) as caught:
                await gateway.generate("flash", MESSAGES, 10)
            self.assertEqual(caught.exception.code, "MODEL_RESPONSE_TOO_LARGE")
            self.assertEqual(stream.read_count, 2)
            self.assertTrue(stream.closed)

    async def test_compressed_or_non_json_responses_and_huge_length_are_rejected(self):
        for headers in ({"content-type": "text/event-stream"},
                        {"content-type": "application/json", "content-encoding": "gzip"},
                        {"content-type": "application/json", "content-length": "9" * 5000}):
            stream = Chunks([b"ignored"])
            gateway = self.gateway(lambda _: httpx.Response(200, stream=stream, headers=headers))
            with self.subTest(headers=headers), self.assertRaises(GatewayError):
                await gateway.generate("flash", MESSAGES, 10)
            self.assertEqual(stream.read_count, 0)
            self.assertTrue(stream.closed)

    async def test_strict_json_rejects_duplicate_nonfinite_overflow_and_bad_encoding(self):
        for raw in (b'{"model":"deepseek-flash","model":"evil"}',
                    b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":1e999}', b'\xff', b'[]', b'{"broken":'):
            gateway = self.gateway(lambda _: httpx.Response(200, content=raw, headers={"content-type": "application/json"}))
            with self.subTest(raw=raw), self.assertRaises(GatewayError):
                await gateway.generate("flash", MESSAGES, 10)

    async def test_response_model_is_checked_against_configured_identity(self):
        body = response_body()
        body["model"] = "deepseek-v4-pro"
        gateway = self.gateway(lambda _: httpx.Response(200, json=body))
        with self.assertRaises(GatewayError) as caught:
            await gateway.generate("flash", MESSAGES, 10)
        self.assertEqual(caught.exception.code, "MODEL_IDENTITY_MISMATCH")

    async def test_required_response_types_and_no_tool_execution(self):
        mutations = [lambda b: b.update(created=True), lambda b: b.update(id={}),
                     lambda b: b.update(object="chat.completion.chunk"), lambda b: b.update(choices=[]),
                     lambda b: b["choices"][0].update(index=False),
                     lambda b: b["choices"][0]["message"].update(content=None),
                     lambda b: b["choices"][0]["message"].update(role="user"),
                     lambda b: b["choices"][0]["message"].update(tool_calls=[{"function": SECRET}]),
                     lambda b: b["choices"][0]["message"].update(reasoning_content={}),
                     lambda b: b["choices"][0].update(finish_reason="insufficient_system_resource")]
        for mutation in mutations:
            body = response_body()
            mutation(body)
            gateway = self.gateway(lambda _: httpx.Response(200, json=body))
            with self.assertRaises(GatewayError):
                await gateway.generate("flash", MESSAGES, 10)

    async def test_usage_must_be_integer_consistent_and_within_output_cap(self):
        values = [None, {"prompt_tokens": 10, "completion_tokens": True, "total_tokens": 11},
                  {"prompt_tokens": "10", "completion_tokens": 3, "total_tokens": 13},
                  {"prompt_tokens": -1, "completion_tokens": 3, "total_tokens": 2},
                  {"prompt_tokens": 10, "completion_tokens": 11, "total_tokens": 21},
                  {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 12}]
        for usage in values:
            body = response_body()
            body["usage"] = usage
            gateway = self.gateway(lambda _: httpx.Response(200, json=body))
            with self.subTest(usage=usage), self.assertRaises(GatewayError) as caught:
                await gateway.generate("flash", MESSAGES, 10)
            self.assertEqual(caught.exception.code, "INVALID_MODEL_USAGE")

    async def test_optional_cache_and_reasoning_usage_are_consistent(self):
        changes = [{"prompt_cache_miss_tokens": 9}, {"prompt_cache_hit_tokens": None},
                   {"prompt_tokens_details": {"cached_tokens": 1}},
                   {"prompt_tokens_details": []},
                   {"completion_tokens_details": {"reasoning_tokens": 4}}]
        for change in changes:
            body = response_body()
            body["usage"].update(change)
            gateway = self.gateway(lambda _: httpx.Response(200, json=body))
            with self.subTest(change=change), self.assertRaises(GatewayError):
                await gateway.generate("flash", MESSAGES, 10)

    async def test_truncated_answer_is_explicit_and_reasoning_not_returned(self):
        body = response_body()
        body["choices"][0]["finish_reason"] = "length"
        body["choices"][0]["message"]["reasoning_content"] = "private reasoning"
        body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 1}
        gateway = self.gateway(lambda _: httpx.Response(200, json=body))
        result = await gateway.generate("flash", MESSAGES, 10)
        self.assertTrue(result.truncated)
        self.assertEqual(result.usage.reasoning_tokens, 1)
        self.assertNotIn("private reasoning", repr(result))
        self.assertFalse(hasattr(result, "reasoning_content"))

    async def test_thinking_settings_only_from_operator_route(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=response_body())
        gateway = self.gateway(handler, route=ModelRoute("flash", base_url="https://api.deepseek.com/v1/",
                                                       thinking="enabled", reasoning_effort="low"))
        await gateway.generate("flash", MESSAGES, 10)
        self.assertEqual(payloads[0]["thinking"], {"type": "enabled"})
        self.assertEqual(payloads[0]["reasoning_effort"], "low")

    async def test_json_mode_is_strict_server_argument_and_roles_forward_it(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=response_body())
        gateway = self.gateway(handler, roles={"developer": "flash"})
        await gateway.generate("flash", MESSAGES, 10)
        self.assertNotIn("response_format", payloads[-1])
        await gateway.generate_for_role("developer", [{"role": "system", "content": "Return JSON."}], 10, json_mode=True)
        self.assertEqual(payloads[-1]["response_format"], {"type": "json_object"})
        for value in (1, "true", None, {}):
            with self.subTest(value=value), self.assertRaises(GatewayError):
                await gateway.generate("flash", MESSAGES, 10, json_mode=value)
        self.assertEqual(len(payloads), 2)


class ConfigTests(unittest.TestCase):
    def test_giant_timeout_uses_static_error_without_overflow(self):
        huge = 10**1000
        with self.assertRaises(GatewayError) as caught:
            ModelRoute("flash", timeout_seconds=huge)
        self.assertEqual(str(caught.exception), "INVALID_MODEL_CONFIG")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps({"routes": [{"alias": "flash", "timeout_seconds": huge}], "roles": {}}))
            with self.assertRaises(GatewayError) as caught:
                load_model_config(path)
            self.assertEqual(str(caught.exception), "INVALID_MODEL_CONFIG")

    def test_endpoint_and_config_injection_are_rejected(self):
        for endpoint in ("http://api.deepseek.com", "https://evil.test", "https://api.deepseek.com.evil.test",
                         "https://key@api.deepseek.com", "https://api.deepseek.com:444", "https://api.deepseek.com/beta",
                         "https://api.deepseek.com?key=secret", "https://api.deepseek.com/#x",
                         "https://api.deepseek.com\n"):
            with self.subTest(endpoint=endpoint), self.assertRaises(GatewayError):
                ModelRoute("flash", base_url=endpoint)
        for changes in ({"provider": "evil"}, {"key_env": "bad-env"}, {"timeout_seconds": True},
                        {"timeout_seconds": float("inf")}, {"max_output_tokens": False},
                        {"max_response_bytes": 1}, {"thinking": "arbitrary"}):
            with self.subTest(changes=changes), self.assertRaises(GatewayError):
                ModelRoute("flash", **changes)

    def test_example_config_loads_without_key_values(self):
        path = Path(__file__).resolve().parents[1] / "configs/models.example.json"
        routes, roles = load_model_config(path)
        self.assertEqual(routes[0].model, "deepseek-flash")
        self.assertEqual(routes[0].key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(roles["planner"], routes[0].alias)

    def test_secret_fields_duplicate_aliases_and_invalid_roles_are_rejected(self):
        original = {"routes": [{"alias": "flash"}], "roles": {"planner": "flash"}}
        configs = []
        bad = copy.deepcopy(original)
        bad["routes"][0]["api_key"] = SECRET
        configs.append(json.dumps(bad))
        configs.append('{"routes":[],"routes":[],"roles":{}}')
        configs.append(json.dumps({"routes": [{"alias": "flash"}] * 2, "roles": {}}))
        configs.append(json.dumps({"routes": [{"alias": "flash"}], "roles": {"planner": "absent"}}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            for raw in configs:
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(GatewayError) as caught:
                    load_model_config(path)
                self.assertNotIn(SECRET, repr(caught.exception))
