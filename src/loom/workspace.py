"""User-scoped workspace state and durable scheduled conversation dispatch.

Scheduling commits an ordinary queued message and a receipt in one SQLite
transaction. It never calls a model or claims the message has been answered.
"""
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
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
 PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS ws_automation_runs (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL,
 automation_id TEXT NOT NULL, slot TEXT NOT NULL, message_id TEXT,
 state TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,automation_id,slot));
CREATE INDEX IF NOT EXISTS ws_automation_due ON ws_automations(enabled,next_run_at);
'''

DEFAULT_PREFERENCES = dict(appearance='light', font='system', font_size=14,
    shortcuts='default', confirmation_mode='ask', preferred_model=None,
    selected_project_id=None)
CHOICES = dict(appearance={'light', 'dark', 'system'}, font={'system', 'sans', 'mono'},
    shortcuts={'default', 'vim', 'emacs'}, confirmation_mode={'ask', 'read_only'})


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


class Workspace:
    def __init__(self, store, *, executor_config=None, executor_config_path=None):
        self.store = store
        self.assistant = AssistantStore(store)
        self.executor_config = executor_config
        self.config_path = executor_config_path or getattr(executor_config, 'source_path', None)
        parent = Path(self.config_path).resolve().parent if self.config_path else Path(store.path).resolve().parent
        self.root = (parent / 'workspaces').resolve()
        with store.connect() as db:
            db.executescript(SCHEMA)

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
        identifier, now = 'auto-' + uuid4().hex, time.time()
        with self.store.transaction() as db:
            project = data.get('project_id')
            if project is not None:
                self._project_in(db, identity, project)
            model = self._model(data.get('model'))
            db.execute('INSERT INTO ws_automations VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,NULL)',
                (identity.tenant_id, identity.subject_id, identifier, name, prompt, project, model, interval,
                 due, due if due else (now + interval * 60 if interval else None), now, now))
            self._audit(db, identity, 'automation.created.disabled', identifier)
        return self.automation(identity, identifier)

    def update_automation(self, identity, identifier, changes):
        allowed = {'name', 'prompt', 'project_id', 'model', 'interval_minutes', 'due_at'}
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
            db.execute('UPDATE ws_automations SET name=?,prompt=?,project_id=?,model=?,interval_minutes=?,due_at=?,next_run_at=?,updated_at=? WHERE tenant=? AND id=?',
                (data['name'], data['prompt'], data['project_id'], data['model'], interval, data['due_at'], next_run, time.time(), identity.tenant_id, identifier))
            self._audit(db, identity, 'automation.updated', identifier)
        return self.automation(identity, identifier)

    def enable_automation(self, identity, identifier, enabled):
        with self.store.transaction() as db:
            row = self._automation_in(db, identity, identifier)
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
                      confirmation_mode=preferences['confirmation_mode']), db=db)
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
            return self._enqueue(db, identity, row, 'manual:' + key, time.time())

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
                try:
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
