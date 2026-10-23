"""Require real Docker repair-cycle checks, simulated HTTP and zero skips.

The image must be the operator-approved immutable Loom sandbox build. This
script reads no real model credentials, identity settings or PostgreSQL config.
Reports contain only test summaries and counters, never candidate source,
frozen expectations, provider request bodies or exception tracebacks.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from loom.sandbox import DockerSandbox, SandboxConfig, SandboxError


class SummaryResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.successes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.successes.append(test.id())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="approved image@sha256:digest or local sha256:imageID; no tags")
    parser.add_argument("--report", type=Path, help="write a source-free and expectation-free JSON summary")
    args = parser.parse_args(argv)
    report = {"schema": "loom.repair.live-check.v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "passed": False, "tests_run": 0, "skipped": 0, "real_models_used": False,
              "model_http_transport": "httpx.MockTransport", "real_docker_used": False,
              "model_responses": {"first_round": "mock", "repair_round": "mock"},
              "candidate_execution": "real DockerSandbox containers",
              "scope": "real Docker Python repair cycles; simulated model HTTP; independent verification still required"}
    try:
        ready = DockerSandbox(SandboxConfig(args.image)).preflight()
        report["image_id"] = ready["image_id"]
        os.environ["LOOM_SANDBOX_TEST_IMAGE"] = args.image
        from tests.integration import test_repair_live
        suite = unittest.defaultTestLoader.loadTestsFromModule(test_repair_live)
        result = SummaryResult()
        suite.run(result)
        observations = test_repair_live.LiveRepairTests.observations
        report.update(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                      skipped=len(result.skipped), successful_tests=result.successes,
                      failed_tests=[test.id() for test, _ in result.failures],
                      errored_tests=[test.id() for test, _ in result.errors], observations=observations,
                      mock_http_requests=sum(row["mock_http_requests"] for row in observations),
                      real_sandbox_runs=sum(row["real_sandbox_runs"] for row in observations),
                      real_container_cases=sum(row["real_container_cases"] for row in observations),
                      passed=result.wasSuccessful() and not result.skipped and result.testsRun == 4)
        report["real_docker_used"] = report["real_container_cases"] > 0
    except (SandboxError, OSError) as error:
        report["blocked"] = type(error).__name__
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
