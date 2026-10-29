"""在私有新副本演练 SQLite 冷启动；不覆盖来源、不启动执行器。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys

from loom.recovery_check import RecoveryCheckError, inspect_copy, make_copy
from loom.store import LoomError


def write_report(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--work-root', type=Path, default=Path(__file__).resolve().parents[3] / 'work')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--inspect-copy', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    report_allowed = False
    try:
        if args.report:
            if args.report.suffix.lower() != '.json' or args.report.exists() or args.report.resolve() == args.db.resolve():
                raise RecoveryCheckError('NEW_JSON_REPORT_REQUIRED')
            report_allowed = True
        if args.inspect_copy:
            result = inspect_copy(args.db)
        else:
            repo = Path(__file__).resolve().parents[1]
            copy = make_copy(args.db, args.work_root, excluded_roots=(repo.parent,))
            log = copy.parent / 'inspection-private.log'
            with log.open('xb') as output:
                log.chmod(0o600)
                process = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                    '--db', str(copy), '--inspect-copy'], stdout=output, stderr=output)
                try: code = process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt':
                        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
                    else: process.kill()
                    process.wait(timeout=10)
                    raise RecoveryCheckError('RECOVERY_CHILD_TIMEOUT') from None
            if code:
                raise RecoveryCheckError('RECOVERY_CHILD_FAILED')
            with log.open('rb') as output: raw = output.read(65537)
            if len(raw) > 65536: raise RecoveryCheckError('RECOVERY_CHILD_FAILED')
            result = json.loads(raw)
            if result.get('schema_version') != 'loom.recovery-check/v1' or result.get('passed') is not True or result.get('model_dispatched') is not False:
                raise RecoveryCheckError('RECOVERY_CHILD_FAILED')
            result['cold_process_open_verified'] = True
        if args.report:
            write_report(args.report, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, LoomError, subprocess.SubprocessError) as exc:
        result = dict(passed=False, code=exc.code if isinstance(exc,RecoveryCheckError) else 'RECOVERY_CHECK_FAILED',
                      message='恢复演练未通过；保留私有副本供排查，来源数据库未被覆盖。', model_dispatched=False)
        if report_allowed and not args.report.exists():
            try: write_report(args.report, result)
            except OSError: pass
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
