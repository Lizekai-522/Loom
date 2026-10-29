"""Isolated SQLite recovery checks; never dispatch work or modify the source."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .approved_execution import ReadOnlyStore
from .dispatch import DispatchService
from .store import Store


class RecoveryCheckError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fingerprint(db):
    """Hash all schema and table rows without including their values in reports."""
    schema = sorted(tuple(r) for r in db.execute(
        "SELECT type,name,tbl_name,coalesce(sql,'') FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    tables = {}
    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        name = row[0]
        quoted = '"' + name.replace('"', '""') + '"'
        values = sorted(repr(tuple(r)) for r in db.execute('SELECT * FROM ' + quoted))
        tables[name] = sha256('\n'.join(values).encode()).hexdigest()
    return dict(schema=sha256(repr(schema).encode()).hexdigest(), tables=tables)


def audit_summary(db):
    if db.execute('PRAGMA quick_check').fetchall()[0][0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
        raise RecoveryCheckError('DATABASE_INTEGRITY_FAILED')
    runs = db.execute('SELECT tenant,id FROM runs ORDER BY tenant,id').fetchall()
    event_count = 0
    for run in runs:
        rows = db.execute('SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? ORDER BY seq', tuple(run)).fetchall()
        events = [{**json.loads(r['envelope']), 'hash': r['hash']} for r in rows]
        if (not events or any((e['tenant_id'], e['run_id'], e['chain_id']) != (run[0], run[1], run[1]) for e in events)
                or not Store.verify_events(events, expected_count=len(rows))):
            raise RecoveryCheckError('AUDIT_CHAIN_INVALID')
        event_count += len(events)
    return dict(runs=len(runs), events=event_count,
                tasks=db.execute('SELECT count(*) FROM tasks').fetchone()[0],
                effects=db.execute('SELECT count(*) FROM effects').fetchone()[0],
                held_reservations=db.execute("SELECT count(*) FROM reservations WHERE state='reserved'").fetchone()[0],
                unresolved_effects=db.execute("SELECT count(*) FROM effects WHERE state IN ('intent','partial','indeterminate')").fetchone()[0])


def make_copy(source, work_root, *, excluded_roots=()):
    source = Path(source).resolve()
    root = Path(work_root).resolve()
    if not source.is_file() or any(root.is_relative_to(Path(p).resolve()) for p in excluded_roots):
        raise RecoveryCheckError('PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    parent = root / 'loom-recovery-checks'
    if parent.is_symlink() or parent.resolve() != parent:
        raise RecoveryCheckError('PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = parent / uuid4().hex
    directory.mkdir(mode=0o700)
    destination = directory / 'restored.db'
    # One source read transaction pins the checked snapshot during online backup.
    with ReadOnlyStore(source).connect() as db:
        db.execute('BEGIN')
        summary = audit_summary(db)
        expected = fingerprint(db)
        with sqlite3.connect(destination) as target:
            db.backup(target)
    with ReadOnlyStore(destination).connect() as db:
        if fingerprint(db) != expected or audit_summary(db) != summary:
            raise RecoveryCheckError('BACKUP_SNAPSHOT_DIFFERS')
    metadata = dict(schema_version='loom.recovery-snapshot/v1', fingerprint=expected, summary=summary)
    path = directory / 'expected.json'
    path.write_text(json.dumps(metadata, sort_keys=True) + '\n', encoding='utf-8')
    path.chmod(0o600)
    destination.chmod(0o600)
    return destination


def inspect_copy(destination):
    """Cold-open a new private copy, then read request history without a worker."""
    destination = Path(destination).resolve()
    if destination.name != 'restored.db' or destination.parent.parent.name != 'loom-recovery-checks':
        raise RecoveryCheckError('GENERATED_RECOVERY_COPY_REQUIRED')
    metadata = json.loads((destination.parent / 'expected.json').read_bytes())
    readonly = ReadOnlyStore(destination)
    with readonly.connect() as db:
        if fingerprint(db) != metadata['fingerprint'] or audit_summary(db) != metadata['summary']:
            raise RecoveryCheckError('RESTORED_COPY_DIFFERS')
        settings = dict(db.execute('SELECT key,value FROM settings'))
        has_requests = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_requests'").fetchone())
    store = Store(destination, max_workers=settings['max_workers'], verify_reserve=settings['verify_reserve'])
    # Schema initialization and the request reader run in this fresh process.
    dispatch = DispatchService(store) if has_requests else None
    states = Counter()
    with readonly.connect() as db:
        if dispatch is not None:
            targets = db.execute('SELECT DISTINCT tenant,run_id,task_id FROM execution_requests').fetchall()
            for row in targets:
                states.update(item['state'] for item in dispatch.history(*tuple(row))['items'])
        if fingerprint(db) != metadata['fingerprint'] or audit_summary(db) != metadata['summary']:
            raise RecoveryCheckError('COLD_OPEN_CHANGED_DATA')
        running_tasks = db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
    summary = metadata['summary']
    return dict(schema_version='loom.recovery-check/v1', passed=True,
                sqlite_copy_verified=True, store_reopen_verified=True, cold_process_open_verified=False,
                audit_chains_verified=True, ledger_and_all_table_rows_preserved=True,
                independent_checkpoint_verified=False, projection_rebuild_performed=False,
                requests_observed=sum(states.values()), queued_requests=states['queued'], running_requests=states['running'],
                running_tasks=running_tasks,
                manual_review_required=bool(running_tasks or summary['held_reservations'] or summary['unresolved_effects']
                                            or any(states[s] for s in ('running','indeterminate','awaiting_human','blocked'))),
                **summary, model_dispatched=False, counts_as_success=False, production_ready=False)
