"""Content-addressed domain assets. References grant no capabilities."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PackName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]
PackVersion = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")]


class PackReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")
    schema_version: Literal["loom.domain-pack-ref/v1"] = "loom.domain-pack-ref/v1"
    name: PackName
    version: PackVersion
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
