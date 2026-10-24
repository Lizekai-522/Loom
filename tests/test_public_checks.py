"""Compiler invariants and hostile-input boundaries, independent of models."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest

from loom.artifact_schemas import validate_body
from loom.execution import freeze_tests
from loom.public_checks import (COMPILER_VERSION, PublicChecksError,
                                compile_public_checks, compile_public_checks_json)
from loom.store import canonical, digest


def body(value=None):
    return {"schema_version": "loom.public-checks/v1", "visibility": "development",
            "cases": [{"name": "case", "input": {"items": [value]}, "expected": value}]}


def test_digest_is_the_existing_executor_digest_and_candidate_body_hash():
    source = body({"value": [False, 1, 1.0, None, "中文", 9007199254740993]})
    compiled = compile_public_checks(source)
    assert compiled.development_digest == freeze_tests(compiled.tests)[1]
    assert compiled.source_body_hash == digest(validate_body("public-checks", source))
    assert compiled.compiler_version == COMPILER_VERSION
    assert compiled.compilation_digest == digest({
        "compiler_version": COMPILER_VERSION, "source_body_hash": compiled.source_body_hash,
        "development_digest": compiled.development_digest})


def test_mutating_source_returned_tests_and_preview_never_changes_compilation():
    source = body({"nested": [1]})
    compiled = compile_public_checks(source)
    before = compiled.preview(include_cases=True)
    source["cases"][0]["expected"]["nested"].append(2)
    tests = compiled.tests
    tests[0].input["items"][0]["nested"].append(3)
    tests[0].expected["nested"].append(4)
    preview = compiled.preview(include_cases=True)
    preview["cases"][0]["expected"]["nested"].append(5)
    assert compiled.preview(include_cases=True) == before
    assert compiled.tests is not tests and compiled.tests[0] is not tests[0]
    with pytest.raises(FrozenInstanceError):
        compiled.tests[0].name = "changed"
    with pytest.raises(FrozenInstanceError):
        compiled.development_digest = "0" * 64


def test_no_file_environment_database_network_process_or_secret_access():
    with patch("builtins.open", side_effect=AssertionError("file access")), \
         patch("sqlite3.connect", side_effect=AssertionError("database")), \
         patch("socket.create_connection", side_effect=AssertionError("network")), \
         patch("subprocess.Popen", side_effect=AssertionError("process")), \
         patch("os.getenv", side_effect=AssertionError("environment")), \
         patch("loom.local_config.load_project_secrets", side_effect=AssertionError("secrets")):
        assert compile_public_checks_json(canonical(body())).preview()["executed"] is False


def test_preview_has_no_execution_or_model_provenance_claim():
    compiled = compile_public_checks(body("private-value"))
    preview = compiled.preview()
    assert "cases" not in preview and "private-value" not in canonical(preview)
    assert preview["approval_required"] is True
    assert all(preview[key] is False for key in ("confers_authority", "executed", "verified", "counts_as_success"))
    assert "model_source_recorded" not in preview and "origin" not in preview
    assert preview["case_names"] == ["case"]
    with pytest.raises(PublicChecksError):
        compiled.preview(include_cases=1)


def test_all_json_types_and_large_integers_survive_exactly():
    values = [True, False, 1, 1.0, -0.0, None, "1", 9007199254740993, 10**70,
              {"items": [1, "中文", False, None]}]
    for value in values:
        compiled = compile_public_checks_json(canonical(body(value)))
        actual = compiled.tests[0].expected
        assert type(actual) is type(value) and canonical(actual) == canonical(value)
    assert len({compile_public_checks(body(v)).development_digest for v in [True, 1, 1.0, "1", None]}) == 5


def test_key_order_is_irrelevant_but_case_order_name_input_and_expected_are_bound():
    source = body({"b": 2, "a": 1})
    reordered = {"cases": source["cases"], "visibility": "development", "schema_version": source["schema_version"]}
    assert compile_public_checks(source) == compile_public_checks(reordered)
    source["cases"].append({"name": "other", "input": 0, "expected": 2})
    base = compile_public_checks(source)
    variants = []
    for field in ("name", "input", "expected"):
        changed = deepcopy(source)
        changed["cases"][0][field] = "different"
        variants.append(changed)
    changed = deepcopy(source)
    changed["cases"].reverse()
    variants.append(changed)
    for changed in variants:
        other = compile_public_checks(changed)
        assert other.source_body_hash != base.source_body_hash
        assert other.development_digest != base.development_digest
        assert other.compilation_digest != base.compilation_digest


def test_expected_body_hash_is_strict_and_bound():
    source = body(1)
    expected = digest(source)
    assert compile_public_checks(source, expected_body_hash=expected).source_body_hash == expected
    for invalid in ("0" * 64, expected.upper(), "secret", 1, True, [], {}):
        with pytest.raises(PublicChecksError, match="^PUBLIC_CHECKS_INVALID$"):
            compile_public_checks(source, expected_body_hash=invalid)


@pytest.mark.parametrize("change", [
    lambda b: b.update(schema_version="other"),
    lambda b: b.update(visibility="acceptance"),
    lambda b: b.update(visibility="hidden"),
    lambda b: b.update(approved=True),
    lambda b: b.update(cases=[]),
    lambda b: b.update(cases=b["cases"] * 2),
    lambda b: b["cases"][0].update(name="../escape"),
    lambda b: b["cases"][0].update(executed=True),
    lambda b: b["cases"][0].update(expected=float("nan")),
    lambda b: b["cases"][0].update(input=float("inf")),
    lambda b: b["cases"][0].update(input={1: "coercion"}),
    lambda b: b["cases"][0].update(input=(1, 2)),
    lambda b: b["cases"][0].update(expected={"blob": "x" * 8193}),
    lambda b: b["cases"][0].update(expected=object()),
])
def test_rejects_wrong_schema_authority_claims_and_non_json_data(change):
    source = body()
    change(source)
    with pytest.raises(PublicChecksError, match="^PUBLIC_CHECKS_INVALID$"):
        compile_public_checks(source)


def test_executor_per_value_size_is_stricter_than_artifact_body_limit():
    source = body()
    source["cases"][0]["input"] = ["x" * 6000] * 3
    assert validate_body("public-checks", source)
    with pytest.raises(PublicChecksError):
        compile_public_checks(source)
    source["cases"][0]["input"] = ["x" * 6000] * 2
    assert compile_public_checks(source).preview()["case_count"] == 1


def test_body_size_case_count_depth_and_node_limits():
    oversized = body()
    oversized["cases"] = [{"name": f"c{i}", "input": "x" * 8000, "expected": None} for i in range(5)]
    too_many = body()
    too_many["cases"] = [{"name": f"c{i}", "input": None, "expected": None} for i in range(33)]
    deep = 0
    for _ in range(14):
        deep = [deep]
    for invalid in (oversized, too_many, body(deep), body([None] * 4096), None, [], "untrusted"):
        with pytest.raises(PublicChecksError):
            compile_public_checks(invalid)
    too_many["cases"].pop()
    assert len(compile_public_checks(too_many).tests) == 32


@pytest.mark.parametrize("raw", [
    '{"schema_version":"loom.public-checks/v1","visibility":"development","cases":[],"cases":[]}',
    '{"schema_version":"loom.public-checks/v1","visibility":"development","cases":[{"name":"c","input":{"x":1,"x":2},"expected":0}]}',
    '{"schema_version":"loom.public-checks/v1","visibility":"development","cases":[{"name":"c","input":NaN,"expected":0}]}',
    chr(96) * 3 + "json\n{}\n" + chr(96) * 3, "{} {}", "\ufeff{}", "", "null", "[]", 1, b"{}",
])
def test_raw_json_never_guesses_repairs_or_silently_accepts_duplicates(raw):
    with pytest.raises(PublicChecksError, match="^PUBLIC_CHECKS_INVALID$"):
        compile_public_checks_json(raw)


def test_errors_never_echo_untrusted_body():
    marker = "private-candidate-marker"
    with pytest.raises(PublicChecksError) as error:
        compile_public_checks({"arbitrary": marker})
    assert str(error.value) == "PUBLIC_CHECKS_INVALID" and marker not in str(error.value)


def test_code_html_and_urls_remain_inert_json_data():
    source = body({"source": "__import__('os').system('bad')",
                   "url": "https://example.invalid/check", "html": "<script>bad()</script>"})
    assert compile_public_checks(source).tests[0].expected == source["cases"][0]["expected"]
