"""Run staged checks against explicitly configured disposable local infrastructure.

The integration stage rejects any skipped testcase. Credentials stay in the
private PostgreSQL config file, never in the report or command arguments.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--nats-server-bin", type=Path, required=True)
    parser.add_argument("--postgres-config", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("artifacts/s2-validation.json"))
    args = parser.parse_args()
    if urlsplit(args.nats_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("S2 checks require an explicitly disposable loopback broker")
    if not args.nats_server_bin.is_file() or not args.postgres_config.is_file():
        parser.error("native broker binary and private disposable PostgreSQL config must exist")
    root = Path(__file__).resolve().parents[1]
    artifacts = root / "artifacts"
    artifacts.mkdir(exist_ok=True)
    env = dict(os.environ, LOOM_NATS_TEST_URL=args.nats_url,
               LOOM_NATS_SERVER_BIN=str(args.nats_server_bin.resolve()),
               LOOM_POSTGRES_TEST_CONFIG=str(args.postgres_config.resolve()))
    stages = []
    for name, targets in (("unit-and-local", ["tests", "--ignore=tests/integration"]),
                          ("real-infrastructure", ["tests/integration"])):
        junit = artifacts / ("s2-" + name + ".xml")
        started = time.monotonic()
        completed = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q", "--junitxml=" + str(junit)], cwd=root, env=env)
        if not junit.exists():
            stages.append({"name": name, "passed": False, "exit_code": completed.returncode, "reason": "test report missing"})
            break
        cases = ET.parse(junit).getroot().findall(".//testcase")
        skipped = sum(case.find("skipped") is not None for case in cases)
        failures = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
        success = completed.returncode == 0 and len(cases) > 0 and skipped == 0 and failures == 0
        stages.append({"name": name, "passed": success, "testcases": len(cases), "skipped": skipped,
                       "failures": failures, "exit_code": completed.returncode, "seconds": round(time.monotonic() - started, 3)})
        if not success:
            break
    success = len(stages) == 2 and all(stage["passed"] for stage in stages)
    report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "timezone": "Asia/Shanghai", "milestone": "S2-infrastructure-pilot", "passed": success,
              "stages": stages, "real_models_used": False, "production_ready": False,
              "scope": "Single-host SQLite origin + real JetStream and separate PostgreSQL schema/RLS checks; no PostgreSQL Store adapter"}
    target = args.report.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
