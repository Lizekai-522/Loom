"""Run the preview CLI as a user would, with isolated local input files."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from loom.public_checks import compile_public_checks
from loom.store import canonical


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/preview_public_checks.py"


def source():
    return {"schema_version": "loom.public-checks/v1", "visibility": "development",
            "cases": [{"name": "exact", "input": [9007199254740993, True, 1.0],
                       "expected": {"value": 9007199254740993}}]}


def run(directory, *args):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=directory,
                            capture_output=True, text=True, timeout=20)
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def test_default_preview_exact_digest_no_payload_or_side_effect_files(tmp_path):
    path = tmp_path / "checks.json"
    path.write_text(canonical(source()), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    code, result = run(tmp_path, "--body-file", path)
    expected = compile_public_checks(source()).preview()
    assert code == 0 and result == {"passed": True, **expected}
    assert "cases" not in result and result["executed"] is False
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_explicit_cases_and_matching_hash_preserve_numeric_types(tmp_path):
    path = tmp_path / "checks.json"
    body = source()
    path.write_text(canonical(body), encoding="utf-8")
    compiled = compile_public_checks(body)
    code, result = run(tmp_path, "--body-file", path,
                       "--expected-body-hash", compiled.source_body_hash, "--include-cases")
    assert code == 0 and canonical(result["cases"]) == canonical(body["cases"])
    assert type(result["cases"][0]["input"][0]) is int
    assert type(result["cases"][0]["input"][1]) is bool
    assert type(result["cases"][0]["input"][2]) is float
    assert result["confers_authority"] is False and result["verified"] is False


@pytest.mark.parametrize("raw", [
    b"\xff", b"x" * 32769,
    b'{"schema_version":"loom.public-checks/v1","cases":[],"cases":[]}',
    b'{"private-marker":"never echo this"}',
], ids=["invalid-utf8", "oversize", "duplicate-json", "invalid-schema"])
def test_bad_file_fails_without_echoing_body_or_path(tmp_path, raw):
    path = tmp_path / "private-marker.json"
    path.write_bytes(raw)
    code, result = run(tmp_path, "--body-file", path)
    assert code == 2 and result == {"passed": False, "error": "PUBLIC_CHECKS_INVALID",
                                  "executed": False, "confers_authority": False}
    assert "private-marker" not in canonical(result) and str(tmp_path) not in canonical(result)


def test_missing_file_and_hash_mismatch_are_safe_errors(tmp_path):
    code, missing = run(tmp_path, "--body-file", tmp_path / "missing.json")
    assert code == 2
    path = tmp_path / "checks.json"
    path.write_text(canonical(source()), encoding="utf-8")
    code, mismatch = run(tmp_path, "--body-file", path, "--expected-body-hash", "0" * 64)
    assert code == 2 and missing == mismatch
