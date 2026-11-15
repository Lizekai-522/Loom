"""Storage interface layer: the only module that talks to the database driver.

Every service obtains connections through :class:`loom.store.Store`, which
delegates here. Business modules never import the driver, so a PostgreSQL
backend can be added by implementing the same small surface (connect,
read-only snapshot, backup, script execution, clock, error types) plus a
dialect translation for the constructs catalogued by :func:`portability_report`.

The SQLite backend below preserves the existing behaviour exactly.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re
import sqlite3

IntegrityError = sqlite3.IntegrityError
OperationalError = sqlite3.OperationalError
DatabaseError = sqlite3.DatabaseError
Row = sqlite3.Row


class Connection(sqlite3.Connection):
    """Closing context manager: ``with store.connect() as db`` always closes."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class SqliteBackend:
    name = 'sqlite'
    # Wall clock evaluated by the database, so every writer shares one time source.
    NOW_SQL = "SELECT (julianday('now')-2440587.5)*86400.0"

    def connect(self, path):
        db = sqlite3.connect(path, timeout=15, isolation_level=None, factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA busy_timeout=15000')
        return db

    def connect_read_only(self, path):
        db = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, factory=Connection, isolation_level=None, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db

    @staticmethod
    def begin_write(db):
        db.execute('BEGIN IMMEDIATE')

    @staticmethod
    def enable_wal(db):
        db.execute('PRAGMA journal_mode=WAL')

    def now(self, db):
        return db.execute(self.NOW_SQL).fetchone()[0]

    @staticmethod
    def execute_script(db, script):
        """Run a multi-statement schema inside the caller's transaction."""
        statement = ''
        for line in script.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ''

    @staticmethod
    def backup(db, destination):
        """Online copy of an open connection's database (snapshot pinned by the caller)."""
        with sqlite3.connect(destination) as target:
            db.backup(target)

    def snapshot_copy(self, source, destination):
        """Consistent read-only copy of a database file to a new private file."""
        with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as db, sqlite3.connect(destination) as target:
            db.backup(target)


SQLITE = SqliteBackend()


# ----- portability inventory ---------------------------------------------------------
# Constructs a PostgreSQL backend must translate or replace. "translatable" ones have a
# mechanical dialect mapping; "sqlite_only" ones need schema/code changes first.
TRANSLATABLE = {
    'json_extract': re.compile(r'json_extract\s*\(', re.I),       # -> jsonb ->/->> operators
    'qmark_params': re.compile(r'\?'),                             # -> %s placeholders
    'begin_immediate': re.compile(r'BEGIN\s+IMMEDIATE', re.I),     # -> BEGIN; SELECT ... FOR UPDATE / serializable
}
SQLITE_ONLY = {
    'rowid': re.compile(r'\browid\b', re.I),                       # needs explicit sequence columns
    'raise_abort_trigger': re.compile(r'RAISE\s*\(\s*ABORT', re.I),  # -> plpgsql trigger functions (see 001_core.sql)
    'pragma': re.compile(r'\bPRAGMA\b', re.I),                     # backend setup only
    'insert_or': re.compile(r'INSERT\s+OR\s+(IGNORE|REPLACE)', re.I),  # use INSERT ... ON CONFLICT instead
    'julianday': re.compile(r'\bjulianday\b', re.I),               # backend clock only
}


def _sql_literals(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if re.search(r'\b(SELECT|INSERT|UPDATE|DELETE|CREATE|PRAGMA|BEGIN)\b', text):
                yield text
        elif isinstance(node, ast.JoinedStr):
            text = ''.join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if re.search(r'\b(SELECT|INSERT|UPDATE|DELETE|CREATE)\b', text):
                yield text


def portability_report(paths):
    """Count SQLite-specific constructs per module (static scan of SQL string literals)."""
    report = {}
    for path in paths:
        counts = Counter()
        for text in _sql_literals(path):
            for name, pattern in {**TRANSLATABLE, **SQLITE_ONLY}.items():
                counts[name] += len(pattern.findall(text))
        report[str(path)] = {k: v for k, v in counts.items() if v}
    totals = Counter()
    for counts in report.values():
        totals.update(counts)
    return dict(modules=report, totals=dict(totals),
                translatable=sorted(TRANSLATABLE), sqlite_only=sorted(SQLITE_ONLY))
