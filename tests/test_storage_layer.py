"""Storage interface layer: one driver boundary and a pinned SQL portability baseline."""
import ast
from pathlib import Path

from loom import storage
from loom.store import Store

SRC = Path(__file__).resolve().parents[1] / 'src'
MODULES = sorted(p for p in list((SRC / 'loom').glob('*.py')) + list((SRC / 'monsoon').glob('*.py')) if p.name != 'storage.py')
# Known SQLite-only constructs a PostgreSQL backend must replace. New code may not add
# more; the numbers only go down as the PostgreSQL milestone lands.
BASELINE = {'rowid': 36, 'raise_abort_trigger': 32, 'pragma': 5, 'julianday': 0, 'insert_or': 0}


def imports_driver(path):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import) and any(a.name == 'sqlite3' for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == 'sqlite3':
            return True
    return False


def test_only_the_storage_layer_imports_the_database_driver():
    assert [p.name for p in MODULES if imports_driver(p)] == []


def test_sqlite_only_constructs_do_not_grow():
    totals = storage.portability_report(MODULES)['totals']
    for name, limit in BASELINE.items():
        assert totals.get(name, 0) <= limit, (name, totals.get(name, 0), limit)


def test_store_delegates_connections_clock_and_scripts_to_the_backend(tmp_path):
    store = Store(tmp_path / 'layer.db')
    assert store.backend is storage.SQLITE
    with store.connect() as db:
        assert isinstance(db, storage.Connection) and db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert abs(store.now(db) - __import__('time').time()) < 5
    with store.transaction() as db:
        storage.SQLITE.execute_script(db, 'CREATE TABLE t(x INTEGER);\nINSERT INTO t VALUES (1) ;\nINSERT INTO t VALUES (2);')
    copy = tmp_path / 'copy.db'
    storage.SQLITE.snapshot_copy(store.path, copy)
    with storage.SQLITE.connect_read_only(copy) as db:
        assert db.execute('SELECT count(*) FROM t').fetchone()[0] == 2
    # Standard upserts replace the old SQLite-only INSERT OR forms.
    with store.transaction() as db:
        db.execute('CREATE TABLE kv(k TEXT PRIMARY KEY, v INTEGER)')
        db.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT DO NOTHING', ('a', 1))
        db.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT DO NOTHING', ('a', 2))
        db.execute('INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', ('a', 3))
        assert db.execute('SELECT v FROM kv').fetchone()[0] == 3
