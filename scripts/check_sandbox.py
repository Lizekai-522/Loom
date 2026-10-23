"""Require a real Docker/Linux sandbox and every live isolation probe.

This runner never treats skipped integration tests as success and never runs
candidate code directly on the host. The --image argument is an operator trust
decision: it must identify the build from the reviewed sandbox/Dockerfile.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from loom.sandbox import DockerSandbox, SandboxConfig, SandboxError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="approved image@sha256:digest or local sha256:imageID; no tags")
    parser.add_argument("--report", type=Path, help="write public JSON test results; no credentials")
    args = parser.parse_args(argv)
    report = {"schema": "loom.sandbox.live-check.v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "passed": False, "skipped": 0, "scope": "local Docker isolation and host-frozen JSON tests; no live model"}
    try:
        report["preflight"] = DockerSandbox(SandboxConfig(args.image)).preflight()
        os.environ["LOOM_SANDBOX_TEST_IMAGE"] = args.image
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.integration.test_sandbox_live")
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
        report.update(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                      skipped=len(result.skipped), passed=result.wasSuccessful() and not result.skipped and result.testsRun >= 9)
        report["test_output"] = output.getvalue()
    except (SandboxError, OSError) as exc:
        report["blocked"] = str(exc)
        report["tests_run"] = 0
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
