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
