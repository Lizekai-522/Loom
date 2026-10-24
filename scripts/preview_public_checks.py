"""Read one bounded candidate file and print a compilation preview only."""
import argparse
import json
from pathlib import Path

from loom.artifact_schemas import MAX_BODY_BYTES
from loom.public_checks import PublicChecksError, compile_public_checks_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        "Preview untrusted development checks. Does not read identities/secrets, "
        "create a database, approve a plan, call models or run Docker."))
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--expected-body-hash")
    parser.add_argument("--include-cases", action="store_true",
                        help="Explicitly include input and expected values in stdout.")
    args = parser.parse_args(argv)
    try:
        with args.body_file.open("rb") as source:
            raw = source.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise PublicChecksError()
        compiled = compile_public_checks_json(
            raw.decode("utf-8"), expected_body_hash=args.expected_body_hash)
        result = {"passed": True, **compiled.preview(include_cases=args.include_cases)}
        code = 0
    except (OSError, UnicodeError, PublicChecksError):
        result = {"passed": False, "error": "PUBLIC_CHECKS_INVALID",
                  "executed": False, "confers_authority": False}
        code = 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
