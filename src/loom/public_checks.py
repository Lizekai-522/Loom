"""Pure compilation of untrusted public checks; no approval or execution."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .artifact_schemas import decode_role_output, validate_body
from .execution import freeze_tests
from .sandbox import FrozenTest
from .store import InvalidInput, canonical, digest


COMPILER_VERSION = "loom.public-checks-compiler/v1"


class PublicChecksError(InvalidInput):
    code = "PUBLIC_CHECKS_INVALID"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True)
class CompiledPublicChecks:
    # Keep only immutable canonical text. Each consumer receives fresh JSON
    # trees; changing a returned test cannot detach the stored data from its hash.
    _tests_json: str
    source_body_hash: str
    development_digest: str
    compilation_digest: str

    @property
    def compiler_version(self):
        return COMPILER_VERSION

    @property
    def tests(self) -> tuple[FrozenTest, ...]:
        return tuple(FrozenTest(**case) for case in json.loads(self._tests_json))

    def preview(self, *, include_cases=False):
        if type(include_cases) is not bool:
            raise PublicChecksError()
        cases = json.loads(self._tests_json)
        result = {
            "schema_version": "loom.public-checks-preview/v1",
            "compiler_version": self.compiler_version,
            "source_body_hash": self.source_body_hash,
            "development_digest": self.development_digest,
            "compilation_digest": self.compilation_digest,
            "case_count": len(cases), "case_names": [case["name"] for case in cases],
            "visibility": "development", "purpose": "compilation-preview",
            "approval_required": True, "confers_authority": False,
            "executed": False, "verified": False, "counts_as_success": False,
        }
        if include_cases:
            result["cases"] = cases
        return result


def compile_public_checks(body, *, expected_body_hash=None) -> CompiledPublicChecks:
    """Validate artifact and executor limits, copying JSON without I/O.

    source_body_hash matches the canonical typed candidate body hash. A future
    authenticated freeze protocol must separately verify task/epoch/model origin.
    Input grants do not authorize these tests.
    """
    try:
        checked = validate_body("public-checks", body)
        body_hash = digest(checked)
        if expected_body_hash is not None and (
                type(expected_body_hash) is not str
                or re.fullmatch(r"[0-9a-f]{64}", expected_body_hash) is None
                or expected_body_hash != body_hash):
            raise ValueError
        tests, development_digest = freeze_tests([
            FrozenTest(case["name"], case["input"], case["expected"])
            for case in checked["cases"]
        ])
        # Exactly reuse the executor's representation and digest.
        tests_json = canonical([{"name": t.name, "input": t.input, "expected": t.expected} for t in tests])
        compilation_digest = digest({"compiler_version": COMPILER_VERSION,
                                     "source_body_hash": body_hash,
                                     "development_digest": development_digest})
        return CompiledPublicChecks(tests_json, body_hash, development_digest, compilation_digest)
    except (InvalidInput, TypeError, ValueError, RecursionError, OverflowError, UnicodeError):
        raise PublicChecksError() from None


def compile_public_checks_json(content, *, expected_body_hash=None) -> CompiledPublicChecks:
    """Strict bounded JSON decoding, including duplicate-field rejection."""
    try:
        body = decode_role_output("public-checks", content)
        return compile_public_checks(body, expected_body_hash=expected_body_hash)
    except InvalidInput:
        raise PublicChecksError() from None
