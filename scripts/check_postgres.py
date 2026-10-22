"""Run opt-in real PostgreSQL checks; explicit configuration may never skip.

Usage: python scripts/check_postgres.py --config <private work JSON path>
The named disposable test database's loom schema will be reset.
"""

import argparse
import os
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description="Test the candidate migration in a disposable loopback PostgreSQL cluster")
    parser.add_argument("--config", type=Path, help="private test cluster JSON; no credentials on the command line")
    args = parser.parse_args()
    if args.config:
        os.environ["LOOM_POSTGRES_TEST_CONFIG"] = str(args.config.resolve())
    if not os.environ.get("LOOM_POSTGRES_TEST_CONFIG"):
        print("PostgreSQL check requires --config or LOOM_POSTGRES_TEST_CONFIG; no integration checks were run.", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(str(root / "tests/integration"), pattern="test_postgres_live.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("Explicit PostgreSQL integration configuration must not result in skipped tests.", file=sys.stderr)
        return 1
    if result.testsRun == 0:
        print("No PostgreSQL checks were discovered.", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
