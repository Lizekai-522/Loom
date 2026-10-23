"""Bounded, non-retrying DeepSeek HTTP gateway; configuration is operator-owned.

This module does not reserve budgets or sign verification results. Provider
usage and model reports are not independent evidence of task correctness.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx


_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_KEY_ENV = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
# Official documentation verified 2026-10-23; this is a moving API alias,
# not a cryptographically pinned model snapshot.
VERIFIED_MODELS = frozenset({("deepseek", "deepseek-flash")})


class GatewayError(ValueError):
    """Public errors contain static codes, never HTTP bodies or input content."""

    def __init__(self, code: str, *, status_code: int | None = None):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _require(condition: bool, code: str = "INVALID_MODEL_CONFIG") -> None:
    if not condition:
        raise GatewayError(code)


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _strict_json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
        return number

    def reject(_):
        raise ValueError()

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=reject, parse_float=finite)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise GatewayError("INVALID_JSON") from None


@dataclass(frozen=True)
class ModelRoute:
    alias: str
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = 60
    max_response_bytes: int = 1_048_576
    max_request_bytes: int = 262_144
    max_output_tokens: int = 8192
    thinking: str = "disabled"
    reasoning_effort: str = "high"

    def __post_init__(self) -> None:
        _require(isinstance(self.alias, str) and bool(_ALIAS.fullmatch(self.alias)))
        _require(self.provider == "deepseek")
        _require(isinstance(self.model, str) and bool(_ALIAS.fullmatch(self.model)))
        _require(isinstance(self.key_env, str) and bool(_KEY_ENV.fullmatch(self.key_env)))
        _require(isinstance(self.base_url, str))
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme == "https" and url.hostname == "api.deepseek.com"
                     and url.port in (None, 443) and url.username is None and url.password is None
                     and url.path in ("", "/", "/v1", "/v1/") and not url.query and not url.fragment
                     and not any(ord(char) <= 32 for char in self.base_url))
        except ValueError:
            valid = False
        _require(valid)
        # Compare the bounded range before converting an arbitrary Python int
        # to float inside isfinite; a huge JSON integer must fail statically.
        _require(type(self.timeout_seconds) in (int, float) and 0 < self.timeout_seconds <= 300
                 and math.isfinite(self.timeout_seconds))
        _require(_integer(self.max_response_bytes, 256, 8_388_608))
        _require(_integer(self.max_request_bytes, 256, 4_194_304))
        _require(_integer(self.max_output_tokens, 1, 393_216))
        _require(self.thinking in ("enabled", "disabled"))
        _require(self.reasoning_effort in ("low", "high", "max"))

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class ModelResult:
    content: str = field(repr=False)
    model_id: str
    provider: str
    route_alias: str
    usage: ModelUsage
    request_id: str = field(repr=False)
    finish_reason: str
    response_model: str

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


@dataclass(frozen=True)
class GatewayReadiness:
    route_alias: str
    provider: str
    model_id: str
    key_env: str
    ready: bool
    reason: str
    network_checked: bool = False


def load_model_config(path: str | Path) -> tuple[tuple[ModelRoute, ...], dict[str, str]]:
    """Read a trusted server config. Secret values and unknown fields are refused."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65_537)
        _require(len(raw) <= 65_536)
        data = _strict_json(raw)
        _require(isinstance(data, dict) and set(data) == {"routes", "roles"})
        _require(isinstance(data["routes"], list) and 1 <= len(data["routes"]) <= 32)
        _require(isinstance(data["roles"], dict))
        routes = tuple(ModelRoute(**route) for route in data["routes"])
        _validate_routes(routes, data["roles"])
        return routes, dict(data["roles"])
    except (OSError, TypeError, GatewayError):
        raise GatewayError("INVALID_MODEL_CONFIG") from None


def _validate_routes(routes: Sequence[ModelRoute], roles: Mapping[str, str]) -> None:
    _require(isinstance(routes, (list, tuple)) and 1 <= len(routes) <= 32)
    _require(all(isinstance(route, ModelRoute) for route in routes))
    aliases = {route.alias for route in routes}
    _require(len(aliases) == len(routes))
    _require(isinstance(roles, Mapping) and len(roles) <= 64)
    _require(all(isinstance(role, str) and bool(_ALIAS.fullmatch(role))
                 and isinstance(alias, str) and alias in aliases for role, alias in roles.items()))


class ModelGateway:
    def __init__(self, routes: Sequence[ModelRoute], *, role_aliases: Mapping[str, str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 environ: Mapping[str, str] | None = None):
        roles = {} if role_aliases is None else role_aliases
        _validate_routes(routes, roles)
        self.routes = MappingProxyType({route.alias: route for route in routes})
        self.role_aliases = MappingProxyType(dict(roles))
        self._environ = os.environ if environ is None else environ
        self._client = httpx.AsyncClient(transport=transport if transport is not None else httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                                        follow_redirects=False, trust_env=False,
                                        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))

    async def __aenter__(self) -> ModelGateway:
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _route(self, alias: str) -> ModelRoute:
        if not isinstance(alias, str) or alias not in self.routes:
            raise GatewayError("UNKNOWN_MODEL_ROUTE")
        return self.routes[alias]

    def _key(self, route: ModelRoute) -> str | None:
        value = self._environ.get(route.key_env)
        if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,8192}", value):
            return None
        return value

    def readiness(self, route_alias: str) -> GatewayReadiness:
        route = self._route(route_alias)
        reason = "READY"
        if (route.provider, route.model) not in VERIFIED_MODELS:
            reason = "MODEL_UNVERIFIED"
        elif self._key(route) is None:
            reason = "MODEL_KEY_MISSING_OR_INVALID"
        return GatewayReadiness(route.alias, route.provider, route.model, route.key_env, reason == "READY", reason)

    async def generate_for_role(self, role: str, messages: Sequence[Mapping[str, str]],
                                max_output_tokens: int, *, json_mode: bool = False) -> ModelResult:
        if not isinstance(role, str) or role not in self.role_aliases:
            raise GatewayError("UNKNOWN_MODEL_ROLE")
        return await self.generate(self.role_aliases[role], messages, max_output_tokens, json_mode=json_mode)

    def prepare_request(self, route_alias: str, messages: Sequence[Mapping[str, str]],
                        max_output_tokens: int, *, json_mode: bool = False) -> bytes:
        """Validate exact wire bytes without looking up credentials or doing I/O.

        Trusted executors can reject oversized input before recording a paid
        intent. Dispatch uses this same serializer; headers and keys are absent.
        """
        route = self._route(route_alias)
        _require((route.provider, route.model) in VERIFIED_MODELS, "MODEL_UNVERIFIED")
        _require(type(json_mode) is bool, "INVALID_MODEL_INPUT")
        _require(_integer(max_output_tokens, 1, route.max_output_tokens), "INVALID_MODEL_INPUT")
        _require(isinstance(messages, (list, tuple)) and 1 <= len(messages) <= 128, "INVALID_MODEL_INPUT")
        copied = []
        for message in messages:
            _require(isinstance(message, Mapping) and set(message) == {"role", "content"}, "INVALID_MODEL_INPUT")
            _require(isinstance(message["role"], str) and message["role"] in {"system", "user", "assistant"}
                     and isinstance(message["content"], str) and bool(message["content"].strip()), "INVALID_MODEL_INPUT")
            copied.append({"role": message["role"], "content": message["content"]})
        payload = {"model": route.model, "messages": copied, "max_tokens": max_output_tokens,
                   "stream": False, "thinking": {"type": route.thinking}}
        if route.thinking == "enabled":
            payload["reasoning_effort"] = route.reasoning_effort
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, TypeError):
            raise GatewayError("INVALID_MODEL_INPUT") from None
        _require(len(raw) <= route.max_request_bytes, "MODEL_REQUEST_TOO_LARGE")
        return raw

    async def generate(self, route_alias: str, messages: Sequence[Mapping[str, str]],
                       max_output_tokens: int, *, json_mode: bool = False) -> ModelResult:
        route = self._route(route_alias)
        state = self.readiness(route_alias)
        if not state.ready:
            raise GatewayError(state.reason)
        raw = self.prepare_request(route_alias, messages, max_output_tokens, json_mode=json_mode)
        # Resolve the key only at dispatch; neither routes nor public results hold it.
        key = self._key(route)
        if key is None:
            raise GatewayError("MODEL_KEY_MISSING_OR_INVALID")
        try:
            async with asyncio.timeout(route.timeout_seconds):
                async with self._client.stream("POST", route.endpoint, content=raw,
                        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                 "Accept": "application/json", "Accept-Encoding": "identity"},
                        timeout=httpx.Timeout(route.timeout_seconds)) as response:
                    if response.status_code != 200:
                        # Do not parse or echo error bodies, locations or request objects.
                        raise GatewayError("MODEL_HTTP_ERROR", status_code=response.status_code)
                    _require(response.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json",
                             "INVALID_MODEL_RESPONSE")
                    _require(response.headers.get("content-encoding", "identity").lower() == "identity",
                             "INVALID_MODEL_RESPONSE")
                    length = response.headers.get("content-length")
                    if length is not None:
                        _require(length.isascii() and length.isdigit() and len(length) <= 20 and int(length) <= route.max_response_bytes,
                                 "MODEL_RESPONSE_TOO_LARGE")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > route.max_response_bytes:
                            raise GatewayError("MODEL_RESPONSE_TOO_LARGE")
                        data.extend(chunk)
                    return _parse_result(bytes(data), route, max_output_tokens)
        except (httpx.TimeoutException, TimeoutError):
            raise GatewayError("MODEL_TIMEOUT") from None
        except httpx.HTTPError:
            raise GatewayError("MODEL_TRANSPORT_ERROR") from None


def _parse_result(raw: bytes, route: ModelRoute, output_limit: int) -> ModelResult:
    data = _strict_json(raw)
    _require(isinstance(data, dict), "INVALID_MODEL_RESPONSE")
    _require(data.get("object") == "chat.completion", "INVALID_MODEL_RESPONSE")
    _require(data.get("model") == route.model, "MODEL_IDENTITY_MISMATCH")
    request_id = data.get("id")
    _require(isinstance(request_id, str) and bool(_REQUEST_ID.fullmatch(request_id)), "INVALID_MODEL_RESPONSE")
    _require(_integer(data.get("created"), 0, 2**63 - 1), "INVALID_MODEL_RESPONSE")
    choices = data.get("choices")
    _require(isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict), "INVALID_MODEL_RESPONSE")
    choice = choices[0]
    _require(type(choice.get("index")) is int and choice["index"] == 0, "INVALID_MODEL_RESPONSE")
    _require(choice.get("finish_reason") in ("stop", "length"), "MODEL_INCOMPLETE_RESPONSE")
    message = choice.get("message")
    _require(isinstance(message, dict) and message.get("role") == "assistant"
             and isinstance(message.get("content"), str) and bool(message["content"].strip()), "INVALID_MODEL_RESPONSE")
    _require(message.get("tool_calls") in (None, []) and message.get("function_call") is None,
             "INVALID_MODEL_RESPONSE")
    _require(message.get("reasoning_content") is None or isinstance(message["reasoning_content"], str), "INVALID_MODEL_RESPONSE")
    usage = data.get("usage")
    _require(isinstance(usage, dict), "INVALID_MODEL_USAGE")
    for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        _require(_integer(usage.get(field_name), 0, 1_048_576), "INVALID_MODEL_USAGE")
    prompt, completion, total = (usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens"))
    _require(prompt + completion == total and completion <= output_limit, "INVALID_MODEL_USAGE")
    hit, miss = usage.get("prompt_cache_hit_tokens"), usage.get("prompt_cache_miss_tokens")
    if "prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage:
        _require(_integer(hit, 0, prompt) and _integer(miss, 0, prompt) and hit + miss == prompt, "INVALID_MODEL_USAGE")
    details = usage.get("prompt_tokens_details")
    if "prompt_tokens_details" in usage:
        _require(isinstance(details, dict) and _integer(details.get("cached_tokens"), 0, prompt), "INVALID_MODEL_USAGE")
        _require(hit is None or details["cached_tokens"] == hit, "INVALID_MODEL_USAGE")
    reasoning = None
    if "completion_tokens_details" in usage:
        details = usage["completion_tokens_details"]
        _require(isinstance(details, dict) and _integer(details.get("reasoning_tokens"), 0, completion), "INVALID_MODEL_USAGE")
        reasoning = details["reasoning_tokens"]
    return ModelResult(message["content"], route.model, route.provider, route.alias,
                       ModelUsage(prompt, completion, total, hit, miss, reasoning), request_id,
                       choice["finish_reason"], data["model"])
