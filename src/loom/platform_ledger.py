"""Tenant-scoped, append-only, hash-chained records for platform registries.

Agent definitions, capability snapshots, knowledge, memory, plugins, pack
rollouts and adaptation decisions are versioned facts, not run events. Each
(tenant, kind) forms its own chain; a record never updates in place, so the
latest version is simply the last append for an id.
"""
from __future__ import annotations

import json
import time
from uuid import uuid4

from .store import Conflict, InvalidInput, NotFound, canonical, digest

SCHEMA = '''
CREATE TABLE IF NOT EXISTS platform_records (
 tenant TEXT NOT NULL, kind TEXT NOT NULL, seq INTEGER NOT NULL, record_id TEXT NOT NULL,
 record TEXT NOT NULL, actor TEXT NOT NULL, ts REAL NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
 PRIMARY KEY(tenant,kind,seq));
CREATE INDEX IF NOT EXISTS platform_records_id ON platform_records(tenant,kind,record_id,seq);
CREATE TRIGGER IF NOT EXISTS platform_records_no_update BEFORE UPDATE ON platform_records
 BEGIN SELECT RAISE(ABORT,'platform records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS platform_records_no_delete BEFORE DELETE ON platform_records
 BEGIN SELECT RAISE(ABORT,'platform records are append-only'); END;
'''


def text(value, label, limit, *, empty=False):
    if type(value) is not str or len(value) > limit or '\x00' in value or (not empty and not value.strip()):
        raise InvalidInput(f'invalid {label}')
    return value


class Ledger:
    def __init__(self, store, *, initialize=True):
        self.store = store
        if initialize:
            with store.connect() as db:
                db.executescript(SCHEMA)

    def append_in(self, db, tenant, kind, record_id, record, actor):
        text(kind, 'record kind', 40)
        text(record_id, 'record id', 200)
        prior = db.execute('SELECT seq,hash FROM platform_records WHERE tenant=? AND kind=? ORDER BY seq DESC LIMIT 1',
                           (tenant, kind)).fetchone()
        seq = prior['seq'] + 1 if prior else 1
        envelope = dict(tenant=tenant, kind=kind, seq=seq, record_id=record_id, record=record, actor=actor,
                        ts=time.time(), prev_hash=prior['hash'] if prior else '0' * 64)
        entry_hash = digest(envelope)
        db.execute('INSERT INTO platform_records VALUES(?,?,?,?,?,?,?,?,?)',
                   (tenant, kind, seq, record_id, canonical(record), actor, envelope['ts'], envelope['prev_hash'], entry_hash))
        return dict(record_id=record_id, seq=seq, hash=entry_hash, ts=envelope['ts'], record=record)

    def append(self, tenant, key, kind, record_id, record, actor, *, check=None):
        """Idempotent append under the Store receipt table; check(db) may refuse."""
        args = dict(kind=kind, record_id=record_id, record=record, actor=actor)

        def apply(db):
            if check is not None:
                check(db)
            return self.append_in(db, tenant, kind, record_id, record, actor)
        return self.store._command(tenant, key, 'platform.' + kind, args, apply)

    @staticmethod
    def _row(row):
        return dict(record_id=row['record_id'], seq=row['seq'], hash=row['hash'], ts=row['ts'], actor=row['actor'],
                    record=json.loads(row['record']))

    def latest_in(self, db, tenant, kind, record_id):
        row = db.execute('SELECT * FROM platform_records WHERE tenant=? AND kind=? AND record_id=? ORDER BY seq DESC LIMIT 1',
                         (tenant, kind, record_id)).fetchone()
        return None if row is None else self._row(row)

    def latest(self, tenant, kind, record_id):
        with self.store.connect() as db:
            found = self.latest_in(db, tenant, kind, record_id)
        if found is None:
            raise NotFound(f'{kind} not found')
        return found

    def history(self, tenant, kind, record_id):
        with self.store.connect() as db:
            return [self._row(r) for r in db.execute(
                'SELECT * FROM platform_records WHERE tenant=? AND kind=? AND record_id=? ORDER BY seq', (tenant, kind, record_id))]

    def current(self, tenant, kind):
        """Latest version of every id of a kind, in first-seen order."""
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM platform_records WHERE tenant=? AND kind=? ORDER BY seq', (tenant, kind)).fetchall()
        latest = {}
        for row in rows:
            latest[row['record_id']] = self._row(row)
        return list(latest.values())

    def verify(self, tenant, kind):
        """Recompute the chain; True only if every link and hash matches."""
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM platform_records WHERE tenant=? AND kind=? ORDER BY seq', (tenant, kind)).fetchall()
        prev = '0' * 64
        for index, row in enumerate(rows, 1):
            envelope = dict(tenant=tenant, kind=kind, seq=row['seq'], record_id=row['record_id'], record=json.loads(row['record']),
                            actor=row['actor'], ts=row['ts'], prev_hash=row['prev_hash'])
            if row['seq'] != index or row['prev_hash'] != prev or digest(envelope) != row['hash']:
                return False
            prev = row['hash']
        return True


def new_id():
    return str(uuid4())


def require(condition, message):
    if not condition:
        raise InvalidInput(message)


def conflict(condition, message):
    if not condition:
        raise Conflict(message)
