"""Code-owned candidate data schemas; these values never confer authority."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from .store import InvalidInput, canonical


ArtifactKind = Literal["requirements", "design", "public-checks", "review-findings"]
ARTIFACT_SCHEMAS = {"requirements": "loom.requirements/v1", "design": "loom.design/v1",
                    "public-checks": "loom.public-checks/v1", "review-findings": "loom.review-findings/v1"}
ARTIFACT_ROLES = {"requirements": "analyst", "design": "architect", "public-checks": "tester", "review-findings": "reviewer"}
MAX_BODY_BYTES = 32768
MAX_CONTEXT_BYTES = 65536
Name = Annotated[str, Field(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_-]*$")]
Text = Annotated[str, Field(min_length=1, max_length=4000)]


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False,
                              frozen=True, revalidate_instances="always")


Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ReviewIdentifier = Annotated[str, Field(min_length=1, max_length=200)]


class ReviewSubjectReference(ArtifactModel):
    schema_version: Literal["loom.review-subject-ref/v1", "loom.review-subject-ref/v2"]
    run_id: ReviewIdentifier
    task_id: ReviewIdentifier
    epoch: int = Field(ge=1)
    plan_digest: Hash
    content_hash: Hash
    source_sha256: Hash
    evaluation_digest: Hash
    evaluation_event_hash: Hash
    submission_event_hash: Hash
    artifact_id: ReviewIdentifier | None = Field(default=None, exclude_if=lambda value: value is None)
    artifact_kind: Literal['requirements','design','public-checks'] | None = Field(default=None, exclude_if=lambda value: value is None)
    artifact_schema_version: str | None = Field(default=None, max_length=64, exclude_if=lambda value: value is None)
    artifact_content_hash: Hash | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def typed_reference(self):
        fields=(self.artifact_id,self.artifact_kind,self.artifact_schema_version,self.artifact_content_hash)
        if self.schema_version=='loom.review-subject-ref/v1':
            if any(value is not None for value in fields):raise ValueError('program reference cannot include document fields')
        elif (any(value is None for value in fields)
              or self.artifact_schema_version!=ARTIFACT_SCHEMAS[self.artifact_kind]):
            raise ValueError('document reference requires its complete typed binding')
        return self


class ReviewFinding(ArtifactModel):
    id: Name
    severity: Literal["blocker", "major", "minor", "info"]
    path: Literal["main.py", "artifact.json"]
    start_line: int = Field(ge=1, le=262144)
    end_line: int = Field(ge=1, le=262144)
    description: Text
    recommendation: Text

    @model_validator(mode="after")
    def ordered_lines(self):
        if self.end_line < self.start_line:
            raise ValueError("finding line range is reversed")
        return self


class ReviewFindingsBody(ArtifactModel):
    schema_version: Literal["loom.review-findings/v1"]
    subject: ReviewSubjectReference
    summary: Text
    verdict: Literal["findings", "no_findings"]
    findings: list[ReviewFinding] = Field(max_length=32)

    @model_validator(mode="after")
    def consistent_findings(self):
        if (bool(self.findings) != (self.verdict == "findings")
                or len({item.id for item in self.findings}) != len(self.findings)):
            raise ValueError("findings and verdict differ or IDs repeat")
        path='main.py' if self.subject.schema_version=='loom.review-subject-ref/v1' else 'artifact.json'
        if any(finding.path!=path for finding in self.findings):raise ValueError('finding path differs from the typed subject')
        return self


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


class DevelopmentSuiteReference(ArtifactModel):
    """Exact existing review reference; no copied model origin or case body."""
    schema_version: Literal["loom.development-suite-ref/v1"]
    source_run_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    suite_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    freeze_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: Literal["loom.public-checks-compiler/v1"]
    development_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RoleOutput(ArtifactModel):
    kind: ArtifactKind
    schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def known_schema(self):
        if self.schema_version != ARTIFACT_SCHEMAS[self.kind]:
            raise ValueError("role output kind and schema version differ")
        return self


class RoleGenerationRequest(ArtifactModel):
    route: Name
    provider: Literal["deepseek"]
    model: Literal["deepseek-flash","deepseek-v4-pro"]
    prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(ge=1, le=8192)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    role_output: RoleOutput
    response_contract: Literal["role-artifact/v1"]
    context_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$",
                                       exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def restricted_pro_request(self):
        if self.model=='deepseek-v4-pro' and (self.route!='deepseek-v4-pro' or self.role_output.kind!='review-findings'):
            raise ValueError('Pro requires the canonical reviewer route')
        return self


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
    model_id: Literal["deepseek-flash","deepseek-v4-pro"]
    provider: Literal["deepseek"]
    route_alias: Name
    usage: RoleModelUsage
    request_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")
    finish_reason: Literal["stop", "length"]
    response_model: Literal["deepseek-flash","deepseek-v4-pro"]


class ModelProvenance(ArtifactModel):
    schema_version: Literal["loom.model-provenance/v1"]
    call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    epoch: int = Field(ge=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["analyst", "architect", "tester", "reviewer"]
    role_output: RoleOutput
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    route: Name
    provider: Literal["deepseek"]
    model: Literal["deepseek-flash","deepseek-v4-pro"]
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
        if type(kind) is not str or kind not in ARTIFACT_SCHEMAS or type(content) is not str:
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


BODY_MODELS = {"requirements": RequirementsBody, "design": DesignBody, "public-checks": PublicChecksBody,
               "review-findings": ReviewFindingsBody}


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
