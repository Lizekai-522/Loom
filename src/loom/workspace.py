"""User-scoped workspace state and durable scheduled conversation dispatch.

Scheduling commits an ordinary queued message and a receipt in one SQLite
transaction. It never calls a model or claims the message has been answered.
Automations default to observe: the schedule is recorded and no message is
queued until a person switches the row to auto or presses run-now.
"""
from datetime import datetime
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4

from monsoon.safety import redact
from monsoon.store import AssistantStore, new_id

from .store import Conflict, InvalidInput, NotFound, canonical
from .model_pricing import TARIFFS


SCHEMA = '''
CREATE TABLE IF NOT EXISTS ws_projects (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
 created_at REAL NOT NULL, deleted_at REAL, PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS ws_preferences (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(tenant,subject));
CREATE TABLE IF NOT EXISTS ws_automations (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
 prompt TEXT NOT NULL, project_id TEXT, model TEXT, interval_minutes INTEGER,
 due_at REAL, next_run_at REAL, enabled INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, deleted_at REAL,
 mode TEXT NOT NULL DEFAULT 'observe',
 PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS ws_automation_runs (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL,
 automation_id TEXT NOT NULL, slot TEXT NOT NULL, message_id TEXT,
 state TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,automation_id,slot));
CREATE INDEX IF NOT EXISTS ws_automation_due ON ws_automations(enabled,next_run_at);
CREATE TABLE IF NOT EXISTS ws_worktrees (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
 run_id TEXT NOT NULL, branch TEXT NOT NULL, path TEXT NOT NULL, base_commit TEXT NOT NULL,
 by_person INTEGER NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, removed_at REAL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,run_id));
'''

ADMISSION_MODES = ('off', 'observe', 'auto')

DEFAULT_PREFERENCES = dict(appearance='light', font='system', font_size=14,
    shortcuts='default', confirmation_mode='ask', preferred_model=None,
    selected_project_id=None)
CHOICES = dict(appearance={'light', 'dark', 'system'}, font={'system', 'sans', 'mono'},
    shortcuts={'default', 'vim', 'emacs'}, confirmation_mode={'ask', 'plan', 'read_only'})


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise InvalidInput('invalid ' + label)
    return redact(value.strip())


def _due(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError
            value = parsed.timestamp()
        except ValueError:
            raise InvalidInput('due_at requires an ISO timestamp with timezone') from None
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise InvalidInput('invalid due_at')
    return float(value)


def _porcelain_path(value):
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text


def _added_paths(diff_text):
    added = []
    lines = diff_text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith('diff --git '):
            continue
        new_file = False
        path = None
        for nxt in lines[index + 1:index + 8]:
            if nxt.startswith('diff --git '):
                break
            if nxt.startswith('new file mode '):
                new_file = True
            if nxt.startswith('+++ b/'):
                path = nxt[6:]
        if new_file and path and path != '/dev/null' and '..' not in Path(path).parts:
            added.append(path)
    return added


def _clip_conflict(text):
    line = (text or '').strip().splitlines()
    return (line[-1] if line else 'untracked file')[:180]


_SKIP_DIRS = frozenset({'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build'})
_SECRET_NAMES = frozenset({'.env', 'credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519'})


class WorktreeConflict(Conflict):
    code = 'WORKTREE_CONFLICT'

    def __init__(self, files):
        self.files = tuple(files)
        listed = ', '.join(self.files) or 'unknown'
        super().__init__('WORKTREE_CONFLICT ' + listed)


class Workspace:
    worker_slots = 16

    def __init__(self, store, *, executor_config=None, executor_config_path=None):
        self.store = store
        self.assistant = AssistantStore(store)
        self.executor_config = executor_config
        self.config_path = executor_config_path or getattr(executor_config, 'source_path', None)
        parent = Path(self.config_path).resolve().parent if self.config_path else Path(store.path).resolve().parent
        self.root = (parent / 'workspaces').resolve()
        with store.connect() as db:
            db.executescript(SCHEMA)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(ws_automations)')}
            if 'mode' not in columns:
                db.execute("ALTER TABLE ws_automations ADD COLUMN mode TEXT NOT NULL DEFAULT 'observe'")

    def _audit(self, db, identity, op, identifier):
        self.assistant.ledger.append_in(db, identity.tenant_id, 'workspace', identifier,
            dict(op=op, subject=identity.subject_id), 'subject:' + identity.subject_id)

    def models(self):
        """Only allowlisted aliases leave this boundary; no provider config or keys."""
        model_path = getattr(self.executor_config, 'model_config', None)
        if self.config_path:
            try:
                config = json.loads(Path(self.config_path).read_text(encoding='utf-8-sig'))
                model_path = config.get('model_config', model_path)
                if model_path and not Path(model_path).is_absolute():
                    model_path = Path(self.config_path).parent / model_path
            except (OSError, ValueError, TypeError):
                return dict(items=[], server_managed=True)
        if model_path is None:
            return dict(items=[], server_managed=True)
        try:
            raw = Path(model_path).read_bytes()
            if len(raw) > 262144:
                return dict(items=[], server_managed=True)
            data = json.loads(raw)
            aliases = {r.get('alias') for r in data.get('routes', []) if isinstance(r, dict)
                       and isinstance(r.get('alias'), str) and r['alias'] in TARIFFS
                       and (r.get('provider'), r.get('model')) ==
                       (TARIFFS[r['alias']].provider, TARIFFS[r['alias']].model)}
            return dict(items=[dict(alias=alias, server_managed=True) for alias in sorted(
                a for a in aliases if isinstance(a, str) and 0 < len(a) <= 128 and
                all(c.isalnum() or c in '-_.:/' for c in a))], server_managed=True)
        except (OSError, ValueError, TypeError, AttributeError):
            return dict(items=[], server_managed=True)

    def _model(self, alias):
        if alias is not None and alias not in {m['alias'] for m in self.models()['items']}:
            raise InvalidInput('model is not configured on the server')
        return alias

    def _project_in(self, db, identity, project_id):
        row = db.execute('SELECT * FROM ws_projects WHERE tenant=? AND subject=? AND id=? AND deleted_at IS NULL',
            (identity.tenant_id, identity.subject_id, project_id)).fetchone()
        if row is None:
            raise NotFound('project not found')
        return dict(row)

    def _path(self, identity, project_id):
        owner = sha256((identity.tenant_id + '\0' + identity.subject_id).encode()).hexdigest()
        parent = self.root / owner
        target = parent / project_id
        if self.root.resolve() != self.root or parent.resolve().parent != self.root or target.resolve().parent != parent:
            raise InvalidInput('workspace path is outside the project boundary')
        return target

    def _project_view(self, identity, row):
        return dict(id=row['id'], name=row['name'], path=str(self._path(identity, row['id'])), created_at=row['created_at'])

    def project(self, identity, project_id):
        with self.store.connect() as db:
            row = self._project_in(db, identity, project_id)
        return self._project_view(identity, row)

    def projects(self, identity):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM ws_projects WHERE tenant=? AND subject=? AND deleted_at IS NULL ORDER BY created_at',
                (identity.tenant_id, identity.subject_id)).fetchall()
        return [self._project_view(identity, row) for row in rows]

    def add_project(self, identity, name):
        name = _text(name, 'project name', 120)
        identifier = 'prj-' + uuid4().hex
        directory = self._path(identity, identifier)
        directory.mkdir(parents=True, exist_ok=False)
        with self.store.transaction() as db:
            db.execute('INSERT INTO ws_projects VALUES(?,?,?,?,?,NULL)',
                (identity.tenant_id, identity.subject_id, identifier, name, time.time()))
            self._audit(db, identity, 'project.created', identifier)
        return self.project(identity, identifier)

    def update_project(self, identity, project_id, name):
        name = _text(name, 'project name', 120)
        with self.store.transaction() as db:
            self._project_in(db, identity, project_id)
            db.execute('UPDATE ws_projects SET name=? WHERE tenant=? AND id=?', (name, identity.tenant_id, project_id))
            self._audit(db, identity, 'project.renamed', project_id)
        return self.project(identity, project_id)

    def delete_project(self, identity, project_id):
        with self.store.transaction() as db:
            self._project_in(db, identity, project_id)
            db.execute('UPDATE ws_projects SET deleted_at=? WHERE tenant=? AND id=?', (time.time(), identity.tenant_id, project_id))
            db.execute('UPDATE ws_automations SET enabled=0 WHERE tenant=? AND subject=? AND project_id=?',
                (identity.tenant_id, identity.subject_id, project_id))
            prefs = self._preferences_in(db, identity)
            if prefs['selected_project_id'] == project_id:
                self._save_preferences(db, identity, {**prefs, 'selected_project_id': None})
            self._audit(db, identity, 'project.deleted', project_id)
        # Files are retained for deliberate recovery; deleting a project is not
        # permission to recursively remove user-created files.
        return dict(id=project_id, deleted=True, files_retained=True)

    def _preferences_in(self, db, identity):
        row = db.execute('SELECT value FROM ws_preferences WHERE tenant=? AND subject=?',
            (identity.tenant_id, identity.subject_id)).fetchone()
        return {**DEFAULT_PREFERENCES, **(json.loads(row['value']) if row else {})}

    def preferences(self, identity):
        with self.store.connect() as db:
            return self._preferences_in(db, identity)

    def _save_preferences(self, db, identity, prefs):
        db.execute('INSERT INTO ws_preferences VALUES(?,?,?) ON CONFLICT(tenant,subject) DO UPDATE SET value=excluded.value',
            (identity.tenant_id, identity.subject_id, canonical(prefs)))

    def update_preferences(self, identity, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULT_PREFERENCES):
            raise InvalidInput('unsupported preference')
        for key, value in changes.items():
            if key in CHOICES and (type(value) is not str or value not in CHOICES[key]):
                raise InvalidInput('invalid preference ' + key)
            if key == 'font_size' and (type(value) is not int or not 12 <= value <= 24):
                raise InvalidInput('invalid font size')
            if key == 'preferred_model':
                self._model(value)
        with self.store.transaction() as db:
            if changes.get('selected_project_id') is not None:
                self._project_in(db, identity, changes['selected_project_id'])
            prefs = {**self._preferences_in(db, identity), **changes}
            self._save_preferences(db, identity, prefs)
            self._audit(db, identity, 'preferences.updated', identity.subject_id)
            return prefs

    def _automation_in(self, db, identity, automation_id):
        row = db.execute('SELECT * FROM ws_automations WHERE tenant=? AND subject=? AND id=? AND deleted_at IS NULL',
            (identity.tenant_id, identity.subject_id, automation_id)).fetchone()
        if row is None:
            raise NotFound('automation not found')
        return dict(row)

    @staticmethod
    def _automation_view(row):
        return {k: (bool(v) if k == 'enabled' else v) for k, v in dict(row).items()
                if k not in ('tenant', 'subject', 'deleted_at')}

    def automations(self, identity):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM ws_automations WHERE tenant=? AND subject=? AND deleted_at IS NULL ORDER BY created_at',
                (identity.tenant_id, identity.subject_id)).fetchall()
        return [self._automation_view(row) for row in rows]

    def automation(self, identity, identifier):
        with self.store.connect() as db:
            return self._automation_view(self._automation_in(db, identity, identifier))

    def add_automation(self, identity, data):
        name = _text(data.get('name'), 'automation name', 120)
        prompt = _text(data.get('prompt'), 'prompt', 4000)
        interval = data.get('interval_minutes')
        if interval is not None and (type(interval) is not int or not 1 <= interval <= 525600):
            raise InvalidInput('invalid interval')
        due = _due(data.get('due_at'))
        mode = data.get('mode') or 'observe'
        if mode not in ADMISSION_MODES:
            raise InvalidInput('invalid automation mode')
        identifier, now = 'auto-' + uuid4().hex, time.time()
        with self.store.transaction() as db:
            project = data.get('project_id')
            if project is not None:
                self._project_in(db, identity, project)
            model = self._model(data.get('model'))
            db.execute('''INSERT INTO ws_automations
                (tenant,subject,id,name,prompt,project_id,model,interval_minutes,due_at,next_run_at,enabled,created_at,updated_at,mode)
                VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?)''',
                (identity.tenant_id, identity.subject_id, identifier, name, prompt, project, model, interval,
                 due, due if due else (now + interval * 60 if interval else None), now, now, mode))
            self._audit(db, identity, 'automation.created.disabled', identifier)
        return self.automation(identity, identifier)

    def update_automation(self, identity, identifier, changes):
        allowed = {'name', 'prompt', 'project_id', 'model', 'interval_minutes', 'due_at', 'mode'}
        if set(changes) - allowed:
            raise InvalidInput('unsupported automation field')
        with self.store.transaction() as db:
            row = self._automation_in(db, identity, identifier)
            data = {**row, **changes}
            data['name'] = _text(data['name'], 'automation name', 120)
            data['prompt'] = _text(data['prompt'], 'prompt', 4000)
            self._model(data['model'])
            if data['project_id'] is not None:
                self._project_in(db, identity, data['project_id'])
            interval = data['interval_minutes']
            if interval is not None and (type(interval) is not int or not 1 <= interval <= 525600):
                raise InvalidInput('invalid interval')
            data['due_at'] = _due(data['due_at'])
            next_run = row['next_run_at']
            if 'due_at' in changes or 'interval_minutes' in changes:
                next_run = data['due_at'] or (time.time() + interval * 60 if interval else None)
            if row['enabled'] and next_run is None:
                raise InvalidInput('enabled automation requires a schedule')
            mode = data.get('mode') or row.get('mode') or 'observe'
            if mode not in ADMISSION_MODES:
                raise InvalidInput('invalid automation mode')
            enabled = 0 if mode == 'off' else row['enabled']
            db.execute('''UPDATE ws_automations SET name=?,prompt=?,project_id=?,model=?,interval_minutes=?,due_at=?,next_run_at=?,updated_at=?,enabled=?,mode=?
                WHERE tenant=? AND id=?''',
                (data['name'], data['prompt'], data['project_id'], data['model'], interval, data['due_at'], next_run,
                 time.time(), enabled, mode, identity.tenant_id, identifier))
            self._audit(db, identity, 'automation.updated', identifier)
        return self.automation(identity, identifier)

    def enable_automation(self, identity, identifier, enabled):
        with self.store.transaction() as db:
            row = self._automation_in(db, identity, identifier)
            if enabled and (row.get('mode') or 'observe') == 'off':
                raise InvalidInput('turn the automation to observe or auto before enabling')
            if enabled and row['next_run_at'] is None:
                raise InvalidInput('automation requires due_at or interval_minutes')
            if enabled and row['project_id']:
                self._project_in(db, identity, row['project_id'])
            if enabled:
                self._model(row['model'])
            db.execute('UPDATE ws_automations SET enabled=?,updated_at=? WHERE tenant=? AND id=?',
                (int(enabled), time.time(), identity.tenant_id, identifier))
            self._audit(db, identity, 'automation.enabled' if enabled else 'automation.disabled', identifier)
        return self.automation(identity, identifier)

    def delete_automation(self, identity, identifier):
        with self.store.transaction() as db:
            self._automation_in(db, identity, identifier)
            db.execute('UPDATE ws_automations SET enabled=0,deleted_at=? WHERE tenant=? AND id=?', (time.time(), identity.tenant_id, identifier))
            self._audit(db, identity, 'automation.deleted', identifier)
        return dict(id=identifier, deleted=True)

    def _enqueue(self, db, identity, row, slot, now):
        prior = db.execute('SELECT * FROM ws_automation_runs WHERE tenant=? AND automation_id=? AND slot=?',
            (identity.tenant_id, row['id'], slot)).fetchone()
        if prior:
            return self._run_view(prior)
        if row['project_id']:
            self._project_in(db, identity, row['project_id'])
        preferences = self._preferences_in(db, identity)
        model_alias = self._model(row['model'] or preferences['preferred_model'])
        thread = db.execute('SELECT * FROM ms_threads WHERE tenant=? AND subject=?',
            (identity.tenant_id, identity.subject_id)).fetchone()
        if thread is None:
            thread_id = new_id('th')
            db.execute('INSERT INTO ms_threads VALUES(?,?,?,?,?)',
                (identity.tenant_id, thread_id, identity.subject_id, '助手', now))
            self.assistant._audit(db, identity.tenant_id, thread_id,
                dict(op='thread.created', subject=identity.subject_id), 'subject:' + identity.subject_id)
        else:
            thread_id = thread['id']
        receipt_id = 'arun-' + uuid4().hex
        message = self.assistant.add_message(identity.tenant_id, thread_id, role='user', kind='text',
            body=row['prompt'], status='queued', author='subject:' + identity.subject_id,
            meta=dict(automation_id=row['id'], automation_run_id=receipt_id,
                      project_id=row['project_id'], model_alias=model_alias,
                      confirmation_mode=preferences['confirmation_mode'],
                      admission=row.get('mode') or 'auto', route=self._route_badge()), db=db)
        db.execute('INSERT INTO ws_automation_runs VALUES(?,?,?,?,?,?,?,NULL,?)',
            (identity.tenant_id, identity.subject_id, receipt_id, row['id'], slot, message['id'], 'queued', now))
        self._audit(db, identity, 'automation.message.queued', receipt_id)
        return dict(id=receipt_id, automation_id=row['id'], slot=slot, message_id=message['id'], state='queued',
                    reason=None, created_at=now, model_called=False)

    @staticmethod
    def _run_view(row):
        result = {k: v for k, v in dict(row).items() if k not in ('tenant', 'subject')}
        result['dispatch_state'] = result['state']
        status = result.get('message_status')
        if status in ('answered', 'failed'):
            result['state'] = status
        # Only the worker's model receipts can determine whether a provider was
        # called. An answered message can also be a local policy response.
        result['model_called'] = False if status in (None, 'queued') else None
        return result

    def run_automation(self, identity, identifier, key):
        key = _text(key, 'idempotency key', 200)
        with self.store.transaction() as db:
            row = self._automation_in(db, identity, identifier)
            # A person pressing run-now is an explicit dispatch, including from observe/off.
            return self._enqueue(db, identity, {**row, 'mode': 'manual'}, 'manual:' + key, time.time())

    def automation_runs(self, identity, identifier):
        with self.store.connect() as db:
            self._automation_in(db, identity, identifier)
            rows = db.execute('SELECT r.*, m.status AS message_status FROM ws_automation_runs r LEFT JOIN ms_messages m ON m.tenant=r.tenant AND m.id=r.message_id WHERE r.tenant=? AND r.subject=? AND r.automation_id=? ORDER BY r.created_at DESC LIMIT 100',
                (identity.tenant_id, identity.subject_id, identifier)).fetchall()
        return [self._run_view(row) for row in rows]

    def dispatch_due(self, *, now=None):
        """One transactional due-slot dispatch; safe across restarts/API processes."""
        now = time.time() if now is None else now
        with self.store.connect() as db:
            due = db.execute('SELECT tenant,subject,id FROM ws_automations WHERE enabled=1 AND deleted_at IS NULL AND next_run_at<=? ORDER BY next_run_at LIMIT 20', (now,)).fetchall()
        receipts = []
        for candidate in due:
            identity = SimpleNamespace(tenant_id=candidate['tenant'], subject_id=candidate['subject'])
            with self.store.transaction() as db:
                row = self._automation_in(db, identity, candidate['id'])
                if not row['enabled'] or row['next_run_at'] is None or row['next_run_at'] > now:
                    continue
                slot = 'schedule:' + repr(row['next_run_at'])
                mode = row.get('mode') or 'auto'
                if mode == 'off':
                    db.execute('UPDATE ws_automations SET enabled=0,updated_at=? WHERE tenant=? AND id=?',
                               (now, identity.tenant_id, row['id']))
                    continue
                try:
                    if mode == 'observe':
                        receipt = self._observe(db, identity, row, slot, now)
                    else:
                        receipt = self._enqueue(db, identity, row, slot, now)
                except (InvalidInput, NotFound):
                    # A removed model/project pauses the schedule and leaves an
                    # explicit durable receipt instead of spinning indefinitely.
                    identifier = 'arun-' + uuid4().hex
                    db.execute('INSERT OR IGNORE INTO ws_automation_runs VALUES(?,?,?,?,?,NULL,?,?,?)',
                        (identity.tenant_id, identity.subject_id, identifier, row['id'], slot, 'blocked', 'configuration_unavailable', now))
                    db.execute('UPDATE ws_automations SET enabled=0 WHERE tenant=? AND id=?', (identity.tenant_id, row['id']))
                    continue
                interval = row['interval_minutes']
                next_run = (row['next_run_at'] + (math.floor((now - row['next_run_at']) / (interval * 60)) + 1) * interval * 60) if interval else None
                db.execute('UPDATE ws_automations SET next_run_at=?,enabled=?,updated_at=? WHERE tenant=? AND id=?',
                    (next_run, int(bool(interval)), now, identity.tenant_id, row['id']))
                receipts.append(receipt)
        return receipts

    @staticmethod
    def _route_badge():
        from .lightpath_shadow import assess
        result = assess(trusted_policy=True, frozen_suite=False, complete_spec=True,
                        side_effect_free=False, semantic_review_required=True,
                        budget_ok=True, unknown_state=False)
        return dict(recommendation=result.recommendation, reason=result.reason, enforcement=False)

    def _observe(self, db, identity, row, slot, now):
        """Record the schedule without queueing a model message."""
        prior = db.execute('SELECT * FROM ws_automation_runs WHERE tenant=? AND automation_id=? AND slot=?',
                           (identity.tenant_id, row['id'], slot)).fetchone()
        if prior:
            return self._run_view(prior)
        receipt_id = 'arun-' + uuid4().hex
        badge = self._route_badge()
        reason = badge['recommendation'] + ':' + badge['reason']
        db.execute('INSERT INTO ws_automation_runs VALUES(?,?,?,?,?,NULL,?,?,?)',
                   (identity.tenant_id, identity.subject_id, receipt_id, row['id'], slot, 'would_dispatch', reason[:500], now))
        self._audit(db, identity, 'automation.would_dispatch', receipt_id)
        return dict(id=receipt_id, automation_id=row['id'], slot=slot, message_id=None, state='would_dispatch',
                    reason=reason[:500], created_at=now, model_called=False)

    def _git(self, cwd, *args, check=True):
        try:
            proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False)
        except FileNotFoundError:
            raise InvalidInput('git is not available') from None
        except subprocess.TimeoutExpired:
            raise InvalidInput('git timed out') from None
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or 'git failed').strip().splitlines()
            raise InvalidInput(detail[-1][:200] if detail else 'git failed')
        return proc

    def _ensure_repository(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / '.git').exists():
            self._git(directory, 'init')
            self._git(directory, 'config', 'user.email', 'verilo@localhost')
            self._git(directory, 'config', 'user.name', 'Verilo')
        if self._git(directory, 'rev-parse', '--verify', 'HEAD', check=False).returncode != 0:
            if self._git(directory, 'config', 'user.email', check=False).returncode != 0:
                self._git(directory, 'config', 'user.email', 'verilo@localhost')
                self._git(directory, 'config', 'user.name', 'Verilo')
            self._git(directory, 'commit', '--allow-empty', '-m', 'verilo project root')
        return self._git(directory, 'rev-parse', 'HEAD').stdout.strip()

    def _worktree_root(self, identity):
        owner = sha256((identity.tenant_id + '\0' + identity.subject_id).encode()).hexdigest()
        root = (self.root / 'worktrees' / owner).resolve()
        base = (self.root / 'worktrees').resolve()
        if base not in root.parents:
            raise InvalidInput('worktree path is outside the workspace boundary')
        return root

    @staticmethod
    def _run_token(run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,80}', run_id):
            raise InvalidInput('invalid run id')
        return run_id

    def _worktree_in(self, db, identity, worktree_id):
        row = db.execute('SELECT * FROM ws_worktrees WHERE tenant=? AND subject=? AND id=? AND status=?',
                         (identity.tenant_id, identity.subject_id, worktree_id, 'active')).fetchone()
        if row is None:
            raise NotFound('worktree not found')
        return dict(row)

    def _worktree_view(self, row):
        path = Path(row['path']).resolve()
        root = (self.root / 'worktrees').resolve()
        if root not in path.parents:
            raise InvalidInput('worktree path is outside the workspace boundary')
        return dict(id=row['id'], project_id=row['project_id'], run_id=row['run_id'], branch=row['branch'],
                    path=str(path), base_commit=row['base_commit'], by_person=bool(row['by_person']),
                    status=row['status'], created_at=row['created_at'])

    def worktrees(self, identity, project_id=None):
        with self.store.connect() as db:
            query = 'SELECT * FROM ws_worktrees WHERE tenant=? AND subject=? AND status=? ORDER BY created_at'
            args = [identity.tenant_id, identity.subject_id, 'active']
            if project_id is not None:
                query += ' AND project_id=?'
                args.append(project_id)
            rows = db.execute(query, args).fetchall()
        return [self._worktree_view(row) for row in rows]

    def worktree(self, identity, worktree_id):
        with self.store.connect() as db:
            return self._worktree_view(self._worktree_in(db, identity, worktree_id))

    def ensure_worktree(self, identity, project_id, run_id, *, title='', is_human_request=False):
        """One git worktree per run, off the person's project checkout."""
        run_id = self._run_token(run_id)
        _text(title or 'task', 'worktree title', 200)
        with self.store.transaction() as db:
            self._project_in(db, identity, project_id)
            existing = db.execute('SELECT * FROM ws_worktrees WHERE tenant=? AND run_id=? AND status=?',
                                  (identity.tenant_id, run_id, 'active')).fetchone()
            if existing is not None:
                if existing['subject'] != identity.subject_id:
                    raise NotFound('worktree not found')
                return self._worktree_view(existing)
            active = db.execute('SELECT by_person FROM ws_worktrees WHERE tenant=? AND subject=? AND status=?',
                                (identity.tenant_id, identity.subject_id, 'active')).fetchall()
            limit = self.worker_slots + (1 if is_human_request else 0)
            if len(active) >= limit:
                raise Conflict('worktree slot limit reached')
            if not is_human_request and sum(not row['by_person'] for row in active) >= self.worker_slots:
                raise Conflict('worktree slot limit reached')
        directory = self._path(identity, project_id)
        base = self._ensure_repository(directory)
        branch = 'wt/' + run_id.replace(':', '-')
        path = self._worktree_root(identity) / run_id.replace(':', '-')
        if path.exists():
            raise Conflict('worktree directory already exists')
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(directory, 'worktree', 'add', '-b', branch, str(path), 'HEAD')
        identifier = 'wt-' + uuid4().hex
        with self.store.transaction() as db:
            self._project_in(db, identity, project_id)
            db.execute('''INSERT INTO ws_worktrees
                (tenant,subject,id,project_id,run_id,branch,path,base_commit,by_person,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,'active',?)''',
                (identity.tenant_id, identity.subject_id, identifier, project_id, run_id, branch, str(path),
                 base, int(is_human_request), time.time()))
            self._audit(db, identity, 'worktree.created', identifier)
        return self.worktree(identity, identifier)

    def worktree_diff(self, identity, worktree_id):
        item = self.worktree(identity, worktree_id)
        path = Path(item['path'])
        self._git(path, 'add', '-A')
        try:
            proc = self._git(path, 'diff', '--cached', '--no-ext-diff', item['base_commit'])
            text = proc.stdout
            if len(text) > 100_000:
                text = text[:100_000] + '\n… diff truncated\n'
            stat = self._git(path, 'diff', '--cached', '--stat', item['base_commit']).stdout[-2000:]
        finally:
            # A read of the diff must not leave the checkout staged.
            self._git(path, 'reset', '-q', check=False)
        return dict(id=item['id'], branch=item['branch'], base_commit=item['base_commit'], stat=stat, diff=text)

    def apply_worktree(self, identity, worktree_id):
        """Copy the worktree diff onto the project checkout. Does not commit or push."""
        item = self.worktree(identity, worktree_id)
        project = self.project(identity, item['project_id'])
        directory = Path(project['path'])
        status = self._git(directory, 'status', '--porcelain', '-uall').stdout.splitlines()
        if any(not line.startswith('??') for line in status):
            raise Conflict('project working tree has uncommitted changes')
        diff = self.worktree_diff(identity, worktree_id)
        if not diff['diff'].strip():
            return dict(id=item['id'], applied=False, reason='empty')
        untracked = {_porcelain_path(line[3:]) for line in status if line.startswith('??')}
        overlap = sorted(untracked & set(_added_paths(diff['diff'])))
        if overlap:
            raise WorktreeConflict(overlap)
        proc = subprocess.run(['git', 'apply', '--index'], cwd=directory, input=diff['diff'],
                              capture_output=True, text=True, timeout=30, check=False)
        if proc.returncode != 0:
            if 'already exists' in (proc.stderr or '').lower():
                raise WorktreeConflict(overlap or [_clip_conflict(proc.stderr)])
            raise Conflict('worktree diff did not apply')
        with self.store.transaction() as db:
            self._audit(db, identity, 'worktree.applied', item['id'])
        return dict(id=item['id'], applied=True, branch=item['branch'])

    def remove_worktree(self, identity, worktree_id):
        with self.store.connect() as db:
            row = self._worktree_in(db, identity, worktree_id)
        project = self._path(identity, row['project_id'])
        removal = self._git(project, 'worktree', 'remove', '--force', row['path'], check=False)
        with self.store.transaction() as db:
            db.execute('UPDATE ws_worktrees SET status=?,removed_at=? WHERE tenant=? AND id=?',
                       ('removed', time.time(), identity.tenant_id, worktree_id))
            self._audit(db, identity, 'worktree.removed', worktree_id)
        return dict(id=worktree_id, removed=True, git_removed=removal.returncode == 0)

    def list_files(self, identity, project_id, query=''):
        root = self._path(identity, project_id)
        if not root.is_dir():
            return []
        needle = (query or '').strip().lower()[:80]
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith('.')]
            for name in filenames:
                if name.startswith('.env') or name in _SECRET_NAMES:
                    continue
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                if needle and needle not in rel.lower():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                found.append(dict(path=rel, bytes=size))
                if len(found) >= 200:
                    found.sort(key=lambda item: item['path'])
                    return found
        found.sort(key=lambda item: item['path'])
        return found

    def read_project_file(self, identity, project_id, relative):
        root = self._path(identity, project_id).resolve()
        if (not isinstance(relative, str) or not relative or len(relative) > 240 or relative.startswith('/')
                or '\\' in relative or '\x00' in relative or '..' in Path(relative).parts):
            raise InvalidInput('invalid project file')
        name = Path(relative).name
        if name.startswith('.env') or name in _SECRET_NAMES:
            raise InvalidInput('project file is not available')
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise InvalidInput('project file is outside the project')
        if not target.is_file():
            raise NotFound('project file not found')
        try:
            if target.stat().st_size > 262144:
                raise InvalidInput('project file is too large to attach')
            raw = target.read_bytes()
        except OSError:
            raise InvalidInput('project file is not readable') from None
        if b'\x00' in raw[:4096]:
            raise InvalidInput('project file is not text')
        text = redact(raw.decode('utf-8', errors='replace'))
        return dict(path=Path(relative).as_posix(), text=text[:8000], truncated=len(text) > 8000)

    def read_mentions(self, identity, project_id, paths):
        if not isinstance(paths, list) or len(paths) > 8:
            raise InvalidInput('too many file mentions')
        items, seen = [], set()
        for rel in paths:
            if not isinstance(rel, str) or rel in seen:
                continue
            seen.add(rel)
            items.append(self.read_project_file(identity, project_id, rel))
        return items

    def _trusted_checkout(self, path):
        path = Path(path).resolve()
        if not path.is_dir():
            raise InvalidInput('worktree checkout is missing')
        if path.parent.parent.name != 'worktrees' or path.parent.parent.parent.name != 'workspaces':
            raise InvalidInput('worktree path is outside the workspace boundary')
        return path

    def write_program(self, tenant, run_id, source):
        """Materialize one main.py into the worktree provisioned for this session."""
        if not isinstance(source, str) or '\x00' in source or len(source.encode('utf-8')) > 131072:
            raise InvalidInput('program source is not allowed in the worktree')
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ws_worktrees'").fetchone() is None:
                return None
            row = db.execute('SELECT * FROM ws_worktrees WHERE tenant=? AND run_id=? AND status=?',
                             (tenant, run_id, 'active')).fetchone()
        if row is None:
            return None
        checkout = self._trusted_checkout(row['path'])
        target = (checkout / 'main.py').resolve()
        if target.parent != checkout:
            raise InvalidInput('worktree path is outside the workspace boundary')
        current = target.read_text(encoding='utf-8') if target.is_file() else None
        if current == source:
            return dict(written=False, path=str(target), worktree_id=row['id'])
        target.write_text(source, encoding='utf-8')
        return dict(written=True, path=str(target), worktree_id=row['id'])
