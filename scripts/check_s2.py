"""Run the local execution alpha checks against explicit local infrastructure.

The integration stage rejects any skipped testcase. Credentials stay in the
private PostgreSQL config file, never in the report or command arguments.
Docker is real; model gateways use test doubles. Real model probes are separate.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from loom import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--nats-server-bin", type=Path, required=True)
    parser.add_argument("--postgres-config", type=Path, required=True)
    parser.add_argument("--sandbox-image", required=True,
                        help="explicitly approved local runner image ID: sha256:<64 lowercase hex digits>")
    parser.add_argument("--report", type=Path, default=Path("artifacts/s2-validation.json"))
    args = parser.parse_args()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", args.sandbox_image) is None:
        parser.error("--sandbox-image must be an approved immutable sha256 image ID; mutable tags are rejected")
    if urlsplit(args.nats_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("S2 checks require an explicitly disposable loopback broker")
    if not args.nats_server_bin.is_file() or not args.postgres_config.is_file():
        parser.error("native broker binary and private disposable PostgreSQL config must exist")
    root = Path(__file__).resolve().parents[1]
    artifacts = root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    env = dict(os.environ, LOOM_NATS_TEST_URL=args.nats_url,
               LOOM_NATS_SERVER_BIN=str(args.nats_server_bin.resolve()),
               LOOM_POSTGRES_TEST_CONFIG=str(args.postgres_config.resolve()),
               LOOM_SANDBOX_TEST_IMAGE=args.sandbox_image)
    stages = []
    for name, targets in (("unit-and-local", ["tests", "--ignore=tests/integration"]),
                          ("real-infrastructure", ["tests/integration"])):
        junit = artifacts / ("s2-" + name + ".xml")
        started = time.monotonic()
        try:
            # A failed launch must never reuse a passing report from an older run.
            junit.unlink(missing_ok=True)
            completed = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q",
                                        "--tb=short", "--no-showlocals", "-o", "junit_logging=no",
                                        "--junitxml=" + str(junit)], cwd=root, env=env)
        except OSError:
            stages.append({"name": name, "passed": False, "reason": "could not reset test report or start pytest"})
            break
        if not junit.exists():
            stages.append({"name": name, "passed": False, "exit_code": completed.returncode, "reason": "test report missing"})
            break
        try:
            junit_root = ET.parse(junit).getroot()
            cases = junit_root.findall(".//testcase")
        except (OSError, ET.ParseError):
            stages.append({"name": name, "passed": False, "exit_code": completed.returncode, "reason": "test report unreadable or invalid"})
            break
        skipped = sum(case.find("skipped") is not None for case in cases)
        failures = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
        success = completed.returncode == 0 and len(cases) > 0 and skipped == 0 and failures == 0
        stage = {"name": name, "passed": success, "testcases": len(cases), "skipped": skipped,
                 "failures": failures, "exit_code": completed.returncode, "seconds": round(time.monotonic() - started, 3)}
        if success:
            suites = [junit_root] if junit_root.tag == "testsuite" else junit_root.findall("./testsuite")
            try:
                # Pytest counts passing subtests in testsuite@tests but does
                # not create additional testcase nodes for them. Count only
                # an entirely passing stage; failed subtests are not passes.
                declared_tests = sum(int(suite.attrib["tests"]) for suite in suites)
                if suites and declared_tests >= len(cases):
                    stage["subtests_passed"] = declared_tests - len(cases)
            except (KeyError, ValueError):
                pass  # Do not invent a count when XML lacks valid totals.
        stages.append(stage)
        if not success:
            break
    success = len(stages) == 2 and all(stage["passed"] for stage in stages)
    report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "timezone": "Asia/Shanghai", "milestone": __version__ + "-public-preview-alpha", "passed": success,
              "stages": stages, "real_models_used": False, "production_ready": False,
              "sandbox_image": args.sandbox_image,
              "scope": "Single-host SQLite local execution alpha; real Docker Linux isolation and JetStream, separate PostgreSQL schema/RLS checks; model gateway tests use mocks, no real model calls. Real model probe reports are separate; no PostgreSQL Store adapter or production readiness claim"}
    target = args.report.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
