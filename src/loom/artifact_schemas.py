"""Code-owned candidate data schemas; these values never confer authority."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from .store import InvalidInput, canonical


ArtifactKind = Literal["requirements", "design", "public-checks"]
ARTIFACT_SCHEMAS = {"requirements": "loom.requirements/v1", "design": "loom.design/v1",
                    "public-checks": "loom.public-checks/v1"}
ARTIFACT_ROLES = {"requirements": "analyst", "design": "architect", "public-checks": "tester"}
MAX_BODY_BYTES = 32768
MAX_CONTEXT_BYTES = 65536
Name = Annotated[str, Field(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_-]*$")]
Text = Annotated[str, Field(min_length=1, max_length=4000)]


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False,
                              frozen=True, revalidate_instances="always")


class ArtifactInput(ArtifactModel):
    source_node: Name
    kind: ArtifactKind
    schema_version: str = Field(min_length=1, max_length=64)
    required: bool = False

    @model_validator(mode="after")
    def known_schema(self):
        if ARTIFACT_SCHEMAS[self.kind] != self.schema_version:
            raise ValueError("artifact kind and schema version differ")
        return self


class RoleOutput(ArtifactModel):
    kind: Literal["requirements", "design"]
    schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def known_schema(self):
        if self.schema_version != ARTIFACT_SCHEMAS[self.kind]:
            raise ValueError("role output kind and schema version differ")
        return self


class RoleGenerationRequest(ArtifactModel):
    route: Name
    provider: Literal["deepseek"]
    model: Literal["deepseek-flash"]
    prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(ge=1, le=8192)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    role_output: RoleOutput
    response_contract: Literal["role-artifact/v1"]
    context_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$",
                                       exclude_if=lambda value: value is None)


class RoleModelUsage(ArtifactModel):
    prompt_tokens: int = Field(ge=0, le=1048576)
    completion_tokens: int = Field(ge=0, le=1048576)
    total_tokens: int = Field(ge=0, le=1048576)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, le=1048576)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0, le=1048576)
    reasoning_tokens: int | None = Field(default=None, ge=0, le=1048576)

    @model_validator(mode="after")
    def consistent_usage(self):
        if self.prompt_tokens + self.completion_tokens != self.total_tokens:
            raise ValueError("model usage total differs")
        hit, miss = self.prompt_cache_hit_tokens, self.prompt_cache_miss_tokens
        if (hit is None) != (miss is None) or (hit is not None and hit + miss != self.prompt_tokens):
            raise ValueError("model cache usage differs")
        if self.reasoning_tokens is not None and self.reasoning_tokens > self.completion_tokens:
            raise ValueError("model reasoning usage exceeds output")
        return self


class RoleModelResult(ArtifactModel):
    content: str = Field(min_length=1, max_length=1048576)
    model_id: Literal["deepseek-flash"]
    provider: Literal["deepseek"]
    route_alias: Name
    usage: RoleModelUsage
    request_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")
    finish_reason: Literal["stop", "length"]
    response_model: Literal["deepseek-flash"]


class ModelProvenance(ArtifactModel):
    schema_version: Literal["loom.model-provenance/v1"]
    call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    epoch: int = Field(ge=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["analyst", "architect"]
    role_output: RoleOutput
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    route: Name
    provider: Literal["deepseek"]
    model: Literal["deepseek-flash"]
    usage: RoleModelUsage
    usage_cost_upper_microusd: int = Field(ge=0)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(ge=1, le=8192)
    response_contract: Literal["role-artifact/v1"]
    model_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_intent_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_done_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$",
                                       exclude_if=lambda value: value is None)


class RoleOutputError(InvalidInput):
    code = "ROLE_OUTPUT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def decode_role_output(kind, content):
    """Decode one bounded model artifact, never permit fenced/duplicate JSON."""
    import json

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject(_):
        raise ValueError

    try:
        if type(kind) is not str or kind not in {"requirements", "design"} or type(content) is not str:
            raise ValueError
        if len(content.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError
        body = json.loads(content, object_pairs_hook=pairs, parse_constant=reject)
        return validate_body(kind, body)
    except (InvalidInput, TypeError, ValueError, RecursionError, UnicodeError, OverflowError):
        raise RoleOutputError() from None


class RequirementItem(ArtifactModel):
    id: Name
    text: Text


class RequirementsBody(ArtifactModel):
    schema_version: Literal["loom.requirements/v1"]
    summary: Text
    items: list[RequirementItem] = Field(min_length=1, max_length=64)
    uncertainties: list[Text] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_items(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("duplicate requirement ID")
        return self


class DesignComponent(ArtifactModel):
    id: Name
    responsibility: Text


class DesignBody(ArtifactModel):
    schema_version: Literal["loom.design/v1"]
    overview: Text
    components: list[DesignComponent] = Field(min_length=1, max_length=64)
    steps: list[Text] = Field(default_factory=list, max_length=32)
    risks: list[Text] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_components(self):
        if len({item.id for item in self.components}) != len(self.components):
            raise ValueError("duplicate component ID")
        return self


class PublicCase(ArtifactModel):
    name: Name
    input: JsonValue
    expected: JsonValue


class PublicChecksBody(ArtifactModel):
    schema_version: Literal["loom.public-checks/v1"]
    visibility: Literal["development"]
    cases: list[PublicCase] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.name for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate development case name")
        return self


BODY_MODELS = {"requirements": RequirementsBody, "design": DesignBody, "public-checks": PublicChecksBody}


def validate_body(kind, body):
    """Bound structure before validation; errors never echo supplied data."""
    if type(kind) is not str or kind not in BODY_MODELS or type(body) is not dict:
        raise InvalidInput("invalid candidate kind or body")
    count = 0

    def bounded(value, depth=0):
        nonlocal count
        count += 1
        if depth > 12 or count > 4096:
            raise ValueError
        if type(value) is dict:
            if any(type(key) is not str or len(key) > 128 for key in value):
                raise ValueError
            for child in value.values():
                bounded(child, depth + 1)
        elif type(value) is list:
            for child in value:
                bounded(child, depth + 1)
        elif value is not None and type(value) not in {str, int, float, bool}:
            raise ValueError
        elif type(value) is str and len(value) > 8192:
            raise ValueError

    try:
        bounded(body)
        if len(canonical(body).encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError
        result = BODY_MODELS[kind].model_validate(body).model_dump(mode="json")
        if len(canonical(result).encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError
        return result
    except (ValidationError, TypeError, ValueError, RecursionError, OverflowError, UnicodeError):
        raise InvalidInput("candidate body violates its schema or static size limit") from None
