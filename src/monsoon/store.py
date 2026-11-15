"""Durable assistant state inside the Loom SQLite store.

Current state lives in small mutable tables; every change is also appended to
the tenant's hash-chained platform ledger (kind ``assistant``) and to the
per-thread event log that the browser stream tails. Event logs are append-only.

Personal data and credentials are redacted on every write (database, event log,
ledger, kernel receipts that echo a result) and again on every read, so rows
written before redaction existed never reach the browser or an export unmasked.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import json
import re
import time
from uuid import uuid4

from loom.platform_ledger import Ledger
from loom.store import Conflict, InvalidInput, NotFound, canonical

from .safety import redact, redact_value


def valid_due_date(value):
    """Accept a real calendar day in the same fixed format used by the UI."""
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True

SCHEMA = '''
CREATE TABLE IF NOT EXISTS ms_threads (
 tenant TEXT NOT NULL, id TEXT NOT NULL, subject TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE UNIQUE INDEX IF NOT EXISTS ms_threads_subject ON ms_threads(tenant,subject);
CREATE TABLE IF NOT EXISTS ms_events (
 tenant TEXT NOT NULL, thread_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
 ts REAL NOT NULL, PRIMARY KEY(tenant,thread_id,seq));
CREATE TRIGGER IF NOT EXISTS ms_events_no_update BEFORE UPDATE ON ms_events
 BEGIN SELECT RAISE(ABORT,'assistant events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ms_events_no_delete BEFORE DELETE ON ms_events
 BEGIN SELECT RAISE(ABORT,'assistant events are append-only'); END;
CREATE TABLE IF NOT EXISTS ms_messages (
 tenant TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL, meta TEXT NOT NULL, author TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS ms_messages_thread ON ms_messages(tenant,thread_id,created_at);
CREATE TABLE IF NOT EXISTS ms_tasks (
 tenant TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
 brief TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS ms_todos (
 tenant TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
 source TEXT NOT NULL, action TEXT, due TEXT, task_id TEXT, ref TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(tenant,id));
CREATE UNIQUE INDEX IF NOT EXISTS ms_todos_ref ON ms_todos(tenant,ref) WHERE ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS ms_mandates (
 tenant TEXT NOT NULL, id TEXT NOT NULL, subject TEXT NOT NULL, scopes TEXT NOT NULL,
 daily_budget_microusd INTEGER NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL, revoked_at REAL,
 PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS ms_approvals (
 tenant TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL, action TEXT NOT NULL, title TEXT NOT NULL,
 detail TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, ref TEXT,
 requested_at REAL NOT NULL, decided_by TEXT, decided_at REAL, PRIMARY KEY(tenant,id));
CREATE UNIQUE INDEX IF NOT EXISTS ms_approvals_ref ON ms_approvals(tenant,ref) WHERE ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS ms_jobs (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, purpose TEXT NOT NULL, owner TEXT NOT NULL, proactive INTEGER NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(tenant,run_id));
CREATE TABLE IF NOT EXISTS ms_settings (
 tenant TEXT NOT NULL PRIMARY KEY, patrol_minutes INTEGER NOT NULL, brief_hour INTEGER NOT NULL,
 tz_offset_minutes INTEGER NOT NULL, daily_budget_microusd INTEGER NOT NULL,
 last_patrol_at REAL, last_brief_date TEXT, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ms_runtime (
 tenant TEXT NOT NULL PRIMARY KEY, executor_id TEXT NOT NULL, search_provider TEXT NOT NULL, heartbeat_at REAL NOT NULL);
'''

TASK_KINDS = ('research', 'coding')
TASK_STATES = ('queued', 'running', 'waiting_approval', 'blocked', 'awaiting_confirmation', 'completed', 'cancelled')
OPEN_TASK_STATES = ('queued', 'running', 'waiting_approval', 'blocked', 'awaiting_confirmation')
TODO_STATES = ('open', 'in_progress', 'done', 'dismissed')
MANDATE_SCOPES = ('research', 'resume')
# Pauses after which no model request of this step has an unknown outcome: a new
# attempt (with its own kernel task) is safe. Anything else waits for a person.
RETRY_SAFE_BLOCKS = frozenset({
    'SEARCH_CONFIG_INVALID', 'SEARCH_TRANSPORT_ERROR', 'SEARCH_NOT_CONFIGURED', 'SEARCH_KEY_MISSING', 'NO_READABLE_SOURCES',
    'NO_SEARCH_RESULTS', 'MODEL_HTTP_ERROR', 'MODEL_KEY_MISSING_OR_INVALID', 'MODEL_REQUEST_TOO_LARGE', 'INVALID_MODEL_INPUT',
    'MODEL_REPORT_INVALID', 'REPORT_POLICY_BLOCKED', 'CONTENT_POLICY_BLOCKED', 'DAILY_BUDGET_REACHED',
    'MANDATE_DAILY_BUDGET_REACHED', 'MANDATE_REQUIRED', 'BUDGET_EXHAUSTED'})
DEFAULTS = dict(patrol_minutes=180, brief_hour=8, tz_offset_minutes=480, daily_budget_microusd=1_000_000)


def text(value, label, limit, *, empty=False):
    if type(value) is not str or len(value) > limit or '\x00' in value or (not empty and not value.strip()):
        raise InvalidInput(f'invalid {label}')
    return value


def new_id(prefix):
    return prefix + '-' + uuid4().hex[:20]


class AssistantStore:
    def __init__(self, store, *, initialize=True):
        self.store = store
        self.ledger = Ledger(store, initialize=initialize)
        if initialize:
            with store.connect() as db:
                db.executescript(SCHEMA)

    # ----- audit + stream ---------------------------------------------------
    def _audit(self, db, tenant, record_id, record, actor):
        self.ledger.append_in(db, tenant, 'assistant', record_id, record, actor)

    def emit(self, db, tenant, thread_id, kind, payload):
        row = db.execute('SELECT COALESCE(MAX(seq),0) FROM ms_events WHERE tenant=? AND thread_id=?', (tenant, thread_id)).fetchone()
        seq = row[0] + 1
        db.execute('INSERT INTO ms_events VALUES(?,?,?,?,?,?)',
                   (tenant, thread_id, seq, kind, canonical(payload), self.store.now(db)))
        return seq

    def events(self, tenant, thread_id, *, after_seq=0, limit=200):
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidInput('invalid cursor')
        with self.store.connect() as db:
            self._thread(db, tenant, thread_id)
            rows = db.execute('SELECT seq,type,payload,ts FROM ms_events WHERE tenant=? AND thread_id=? AND seq>? ORDER BY seq LIMIT ?',
                              (tenant, thread_id, after_seq, limit)).fetchall()
            head = db.execute('SELECT COALESCE(MAX(seq),0) FROM ms_events WHERE tenant=? AND thread_id=?', (tenant, thread_id)).fetchone()[0]
        return dict(events=[dict(seq=r['seq'], type=r['type'], data=redact_value(json.loads(r['payload'])), ts=r['ts']) for r in rows],
                    head_seq=head, next_after_seq=rows[-1]['seq'] if rows else after_seq)

    # ----- threads ----------------------------------------------------------
    def _thread(self, db, tenant, thread_id):
        row = db.execute('SELECT * FROM ms_threads WHERE tenant=? AND id=?', (tenant, thread_id)).fetchone()
        if row is None:
            raise NotFound('thread not found')
        return dict(row)

    def thread_for(self, tenant, subject):
        """One long conversation per person (created on first use)."""
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_threads WHERE tenant=? AND subject=?', (tenant, subject)).fetchone()
            if row is not None:
                return dict(row)
            thread = dict(tenant=tenant, id=new_id('th'), subject=subject, title='助手', created_at=self.store.now(db))
            db.execute('INSERT INTO ms_threads VALUES(?,?,?,?,?)', tuple(thread.values()))
            self._audit(db, tenant, thread['id'], dict(op='thread.created', subject=subject), 'subject:' + subject)
            return thread

    def thread_of(self, tenant, subject):
        """The person's thread, or None (never creates one)."""
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_threads WHERE tenant=? AND subject=?', (tenant, subject)).fetchone()
        return dict(row) if row else None

    def todo(self, tenant, todo_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_todos WHERE tenant=? AND id=?', (tenant, todo_id)).fetchone()
        if row is None:
            raise NotFound('todo not found')
        return self._todo(row)

    def approval(self, tenant, approval_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_approvals WHERE tenant=? AND id=?', (tenant, approval_id)).fetchone()
        if row is None:
            raise NotFound('approval not found')
        return self._approval(row)

    def thread(self, tenant, thread_id):
        with self.store.connect() as db:
            return self._thread(db, tenant, thread_id)

    def threads(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM ms_threads ORDER BY rowid')]

    # ----- messages -----------------------------------------------------------
    @staticmethod
    def _message(row):
        item = dict(row)
        item['meta'] = redact_value(json.loads(item['meta']))
        item['text'] = redact(item['text'])
        item.pop('tenant', None)
        return item

    def add_message(self, tenant, thread_id, *, role, kind, body, status='done', meta=None, author, db=None):
        if role not in ('user', 'assistant', 'notice') or kind not in ('text', 'report', 'brief', 'task', 'approval', 'notice', 'mail'):
            raise InvalidInput('invalid message role or kind')
        counts = Counter()
        body = redact(text(body, 'message', 20000, empty=status == 'streaming'), counts)
        meta = redact_value(meta or {}, counts)
        if counts:
            meta['redacted'] = dict(sorted(counts.items()))

        def apply(db):
            self._thread(db, tenant, thread_id)
            now = self.store.now(db)
            message = dict(id=new_id('m'), thread_id=thread_id, role=role, kind=kind, text=body, status=status,
                           meta=meta or {}, author=author, created_at=now, updated_at=now)
            db.execute('INSERT INTO ms_messages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (tenant, message['id'], thread_id, role, kind, body, status, canonical(message['meta']), author, now, now))
            self.emit(db, tenant, thread_id, 'message', message)
            self._audit(db, tenant, message['id'], dict(op='message.created', role=role, kind=kind, status=status,
                                                        meta=message['meta']), author)
            return message
        if db is not None:
            return apply(db)
        with self.store.transaction() as db:
            return apply(db)

    def update_message(self, tenant, message_id, *, body=None, status=None, meta=None, delta=False):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_messages WHERE tenant=? AND id=?', (tenant, message_id)).fetchone()
            if row is None:
                raise NotFound('message not found')
            current = self._message(row)
            body = current['text'] if body is None else redact(text(body, 'message', 20000, empty=True))
            status = current['status'] if status is None else status
            merged = {**current['meta'], **redact_value(meta or {})}
            now = self.store.now(db)
            db.execute('UPDATE ms_messages SET text=?,status=?,meta=?,updated_at=? WHERE tenant=? AND id=?',
                       (body, status, canonical(merged), now, tenant, message_id))
            if delta:
                self.emit(db, tenant, current['thread_id'], 'message.delta', dict(id=message_id, text=body))
            else:
                updated = {**current, 'text': body, 'status': status, 'meta': merged, 'updated_at': now}
                self.emit(db, tenant, current['thread_id'], 'message.updated', updated)
                self._audit(db, tenant, message_id, dict(op='message.updated', status=status), current['author'])
            return {**current, 'text': body, 'status': status, 'meta': merged}

    def messages(self, tenant, thread_id, *, limit=50, before=None):
        with self.store.connect() as db:
            self._thread(db, tenant, thread_id)
            query, args = 'SELECT * FROM ms_messages WHERE tenant=? AND thread_id=?', [tenant, thread_id]
            if before is not None:
                query += ' AND created_at<?'
                args.append(before)
            rows = db.execute(query + ' ORDER BY created_at DESC, rowid DESC LIMIT ?', (*args, max(1, min(int(limit), 200)))).fetchall()
        return [self._message(r) for r in reversed(rows)]

    def pending_turns(self):
        """User messages still waiting for a reply, oldest first, across tenants."""
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM ms_messages WHERE role='user' AND status='queued' ORDER BY created_at,rowid").fetchall()
        return [dict(self._message(r), tenant=r['tenant']) for r in rows]

    # ----- tasks --------------------------------------------------------------
    @staticmethod
    def _task(row):
        item = dict(row)
        item['state'] = redact_value(json.loads(item['state']))
        item['title'], item['brief'] = redact(item['title']), redact(item['brief'])
        item.pop('tenant', None)
        return item

    def create_task(self, tenant, thread_id, *, kind, title, brief, origin, actor, status='queued', state=None, db=None):
        if kind not in TASK_KINDS or origin not in ('user', 'proactive') or status not in TASK_STATES:
            raise InvalidInput('invalid task')
        title = redact(text(title, 'task title', 200))
        brief = redact(text(brief, 'task brief', 8000))
        state = redact_value(state or {})

        def apply(db):
            self._thread(db, tenant, thread_id)
            now = self.store.now(db)
            task = dict(id=new_id('task'), thread_id=thread_id, kind=kind, title=title, brief=brief, status=status,
                        origin=origin, state=state or {}, created_at=now, updated_at=now)
            db.execute('INSERT INTO ms_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (tenant, task['id'], thread_id, kind, title, brief, status, origin, canonical(task['state']), now, now))
            self.emit(db, tenant, thread_id, 'task', task)
            self._audit(db, tenant, task['id'], dict(op='task.created', kind=kind, origin=origin, status=status), actor)
            return task
        if db is not None:
            return apply(db)
        with self.store.transaction() as db:
            return apply(db)

    def update_task(self, tenant, task_id, *, actor, status=None, state=None, expect=None):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_tasks WHERE tenant=? AND id=?', (tenant, task_id)).fetchone()
            if row is None:
                raise NotFound('task not found')
            task = self._task(row)
            if expect is not None and task['status'] not in expect:
                raise Conflict(f"task is {task['status']}")
            if status is not None and status not in TASK_STATES:
                raise InvalidInput('invalid task status')
            merged = {**task['state'], **redact_value(state or {})}
            now = self.store.now(db)
            db.execute('UPDATE ms_tasks SET status=?,state=?,updated_at=? WHERE tenant=? AND id=?',
                       (status or task['status'], canonical(merged), now, tenant, task_id))
            updated = {**task, 'status': status or task['status'], 'state': merged, 'updated_at': now}
            self.emit(db, tenant, task['thread_id'], 'task', updated)
            if status is not None and status != task['status']:
                self._audit(db, tenant, task_id, dict(op='task.status', status=status, previous=task['status']), actor)
            return updated

    def task(self, tenant, task_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_tasks WHERE tenant=? AND id=?', (tenant, task_id)).fetchone()
        if row is None:
            raise NotFound('task not found')
        return self._task(row)

    def tasks(self, tenant, *, statuses=None, thread_id=None, limit=100):
        query, args = 'SELECT * FROM ms_tasks WHERE tenant=?', [tenant]
        if statuses:
            query += f" AND status IN ({','.join('?' for _ in statuses)})"
            args += list(statuses)
        if thread_id is not None:
            query += ' AND thread_id=?'
            args.append(thread_id)
        with self.store.connect() as db:
            rows = db.execute(query + ' ORDER BY updated_at DESC LIMIT ?', (*args, limit)).fetchall()
        return [self._task(r) for r in rows]

    def runnable_tasks(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM ms_tasks WHERE kind='research' AND status IN ('queued','running') "
                              "ORDER BY created_at,rowid").fetchall()
        return [dict(self._task(r), tenant=r['tenant']) for r in rows]

    # ----- todos --------------------------------------------------------------
    @staticmethod
    def _todo(row):
        item = dict(row)
        item['action'] = redact_value(json.loads(item['action'])) if item['action'] else None
        item['text'] = redact(item['text'])
        item.pop('tenant', None)
        return item

    def add_todo(self, tenant, thread_id, *, body, source, actor, action=None, due=None, ref=None):
        body = redact(text(body, 'todo', 1000))
        action = redact_value(action) if action else action
        if source not in ('user', 'assistant', 'session', 'task'):
            raise InvalidInput('invalid todo source')
        if due is not None and not valid_due_date(due):
            raise InvalidInput('due must be a valid date in YYYY-MM-DD format')
        with self.store.transaction() as db:
            self._thread(db, tenant, thread_id)
            if ref is not None:
                existing = db.execute('SELECT * FROM ms_todos WHERE tenant=? AND ref=?', (tenant, ref)).fetchone()
                if existing is not None:
                    return self._todo(existing)
            now = self.store.now(db)
            todo = dict(id=new_id('todo'), thread_id=thread_id, text=body, status='open', source=source, action=action,
                        due=due, task_id=None, ref=ref, created_at=now, updated_at=now)
            db.execute('INSERT INTO ms_todos VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (tenant, todo['id'], thread_id, body, 'open', source, canonical(action) if action else None,
                        due, None, ref, now, now))
            self.emit(db, tenant, thread_id, 'todo', todo)
            self._audit(db, tenant, todo['id'], dict(op='todo.created', source=source, ref=ref), actor)
            return todo

    def update_todo(self, tenant, todo_id, *, status, actor, task_id=None):
        if status not in TODO_STATES:
            raise InvalidInput('invalid todo status')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_todos WHERE tenant=? AND id=?', (tenant, todo_id)).fetchone()
            if row is None:
                raise NotFound('todo not found')
            todo = self._todo(row)
            now = self.store.now(db)
            db.execute('UPDATE ms_todos SET status=?,task_id=COALESCE(?,task_id),updated_at=? WHERE tenant=? AND id=?',
                       (status, task_id, now, tenant, todo_id))
            updated = {**todo, 'status': status, 'task_id': task_id or todo['task_id'], 'updated_at': now}
            self.emit(db, tenant, todo['thread_id'], 'todo', updated)
            self._audit(db, tenant, todo_id, dict(op='todo.status', status=status), actor)
            return updated

    def todos(self, tenant, *, statuses=('open', 'in_progress'), thread_id=None):
        query, args = f"SELECT * FROM ms_todos WHERE tenant=? AND status IN ({','.join('?' for _ in statuses)})", [tenant, *statuses]
        if thread_id is not None:
            query += ' AND thread_id=?'
            args.append(thread_id)
        with self.store.connect() as db:
            return [self._todo(r) for r in db.execute(query + ' ORDER BY created_at,rowid LIMIT 500', args)]

    # ----- mandates -----------------------------------------------------------
    def grant_mandate(self, tenant, subject, *, scopes, daily_budget_microusd, days):
        if (not isinstance(scopes, list) or not scopes or len(set(scopes)) != len(scopes)
                or not set(scopes) <= set(MANDATE_SCOPES)):
            raise InvalidInput('scopes must be chosen from: ' + ', '.join(MANDATE_SCOPES))
        if type(daily_budget_microusd) is not int or not 0 < daily_budget_microusd <= 20_000_000:
            raise InvalidInput('daily budget must be between 1 and 20000000 micro-USD')
        if type(days) is not int or not 1 <= days <= 90:
            raise InvalidInput('mandate lasts 1 to 90 days')
        with self.store.transaction() as db:
            now = self.store.now(db)
            mandate = dict(id=new_id('mandate'), subject=subject, scopes=sorted(scopes), daily_budget_microusd=daily_budget_microusd,
                           expires_at=now + days * 86400, created_at=now, revoked_at=None)
            db.execute('INSERT INTO ms_mandates VALUES(?,?,?,?,?,?,?,?)',
                       (tenant, mandate['id'], subject, canonical(mandate['scopes']), daily_budget_microusd,
                        mandate['expires_at'], now, None))
            self._audit(db, tenant, mandate['id'], dict(op='mandate.granted', **{k: v for k, v in mandate.items() if k != 'id'}),
                        'subject:' + subject)
            return mandate

    def revoke_mandate(self, tenant, mandate_id, subject):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_mandates WHERE tenant=? AND id=?', (tenant, mandate_id)).fetchone()
            if row is None:
                raise NotFound('mandate not found')
            if row['revoked_at'] is not None:
                return self._mandate(row)
            now = self.store.now(db)
            db.execute('UPDATE ms_mandates SET revoked_at=? WHERE tenant=? AND id=?', (now, tenant, mandate_id))
            self._audit(db, tenant, mandate_id, dict(op='mandate.revoked'), 'subject:' + subject)
            return {**self._mandate(row), 'revoked_at': now, 'active': False}

    def _mandate(self, row, now=None):
        item = dict(row)
        item['scopes'] = json.loads(item['scopes'])
        item.pop('tenant', None)
        now = time.time() if now is None else now
        item['active'] = item['revoked_at'] is None and item['expires_at'] > now
        return item

    def mandates(self, tenant):
        with self.store.connect() as db:
            return [self._mandate(r) for r in db.execute('SELECT * FROM ms_mandates WHERE tenant=? ORDER BY created_at DESC', (tenant,))]

    def active_mandate(self, tenant, scope, subject):
        """The signer's own active mandate: it never covers another person's thread."""
        return next((m for m in self.mandates(tenant) if m['active'] and scope in m['scopes'] and m['subject'] == subject), None)

    # ----- approvals ----------------------------------------------------------
    @staticmethod
    def _approval(row):
        item = dict(row)
        item['payload'] = redact_value(json.loads(item['payload']))
        item['title'], item['detail'] = redact(item['title']), redact(item['detail'])
        item.pop('tenant', None)
        return item

    def request_approval(self, tenant, thread_id, *, action, title, detail, payload, actor, ref=None):
        if action not in ('start_research', 'start_coding', 'send_mail'):
            raise InvalidInput('unknown approval action')
        title, detail = redact(text(title, 'approval title', 200)), redact(text(detail, 'approval detail', 4000))
        payload = redact_value(payload)
        with self.store.transaction() as db:
            if ref is not None:
                existing = db.execute('SELECT * FROM ms_approvals WHERE tenant=? AND ref=?', (tenant, ref)).fetchone()
                if existing is not None:
                    return self._approval(existing)
            now = self.store.now(db)
            approval = dict(id=new_id('apv'), thread_id=thread_id, action=action, title=title, detail=detail, payload=payload, status='pending', ref=ref,
                            requested_at=now, decided_by=None, decided_at=None)
            db.execute('INSERT INTO ms_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (tenant, approval['id'], thread_id, action, title, detail, canonical(payload), 'pending', ref, now, None, None))
            self.emit(db, tenant, thread_id, 'approval', approval)
            self._audit(db, tenant, approval['id'], dict(op='approval.requested', action=action, payload=payload), actor)
            return approval

    def decide_approval(self, tenant, approval_id, *, decision, subject):
        if decision not in ('approved', 'rejected'):
            raise InvalidInput('decision must be approved or rejected')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_approvals WHERE tenant=? AND id=?', (tenant, approval_id)).fetchone()
            if row is None:
                raise NotFound('approval not found')
            if row['status'] != 'pending':
                raise Conflict('approval was already decided')
            now = self.store.now(db)
            db.execute('UPDATE ms_approvals SET status=?,decided_by=?,decided_at=? WHERE tenant=? AND id=?',
                       (decision, 'subject:' + subject, now, tenant, approval_id))
            approval = {**self._approval(row), 'status': decision, 'decided_by': 'subject:' + subject, 'decided_at': now}
            self.emit(db, tenant, row['thread_id'], 'approval', approval)
            self._audit(db, tenant, approval_id, dict(op='approval.decided', decision=decision), 'subject:' + subject)
            return approval

    def approvals(self, tenant, *, status='pending'):
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM ms_approvals WHERE tenant=? AND (? IS NULL OR status=?) ORDER BY requested_at DESC LIMIT 200',
                              (tenant, status, status)).fetchall()
        return [self._approval(r) for r in rows]

    # ----- content rules -------------------------------------------------------
    def record_safety(self, tenant, thread_id, verdict, *, ref):
        """Audit a content-rule hit: category, action, rule id and stage, never the text."""
        record = dict(verdict, ref=ref)
        with self.store.transaction() as db:
            self._thread(db, tenant, thread_id)
            self.emit(db, tenant, thread_id, 'safety', record)
            self._audit(db, tenant, 'safety:' + ref, dict(op='safety.hit', **record), 'assistant')
        return record

    # ----- budget jobs + settings ---------------------------------------------
    def record_job(self, tenant, run_id, *, purpose, owner, proactive):
        with self.store.transaction() as db:
            db.execute('INSERT INTO ms_jobs VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                       (tenant, run_id, purpose, owner, int(bool(proactive)), self.store.now(db)))

    def spend_since(self, tenant, since, *, proactive=None):
        """Spent plus still-reserved micro-USD of assistant jobs since a timestamp."""
        query = ('SELECT COALESCE(SUM(r.spent+r.reserved),0) FROM ms_jobs j JOIN runs r ON r.tenant=j.tenant AND r.id=j.run_id '
                 'WHERE j.tenant=? AND j.created_at>=?')
        args = [tenant, since]
        if proactive is not None:
            query += ' AND j.proactive=?'
            args.append(int(bool(proactive)))
        with self.store.connect() as db:
            return db.execute(query, args).fetchone()[0]

    def settings(self, tenant):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_settings WHERE tenant=?', (tenant,)).fetchone()
        if row is None:
            return dict(tenant_id=tenant, **DEFAULTS, last_patrol_at=None, last_brief_date=None)
        item = dict(row)
        item['tenant_id'] = item.pop('tenant')
        return item

    def save_settings(self, tenant, *, actor, **changes):
        bounds = dict(patrol_minutes=(15, 1440), brief_hour=(-1, 23), tz_offset_minutes=(-720, 840),
                      daily_budget_microusd=(0, 20_000_000))
        for key, value in changes.items():
            if key in bounds:
                low, high = bounds[key]
                if type(value) is not int or not low <= value <= high:
                    raise InvalidInput(f'{key} must be an integer between {low} and {high}')
            elif key not in ('last_patrol_at', 'last_brief_date'):
                raise InvalidInput('unknown setting ' + key)
        current = self.settings(tenant)
        current.update(changes)
        with self.store.transaction() as db:
            now = self.store.now(db)
            db.execute('INSERT INTO ms_settings VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tenant) DO UPDATE SET '
                       'patrol_minutes=excluded.patrol_minutes,brief_hour=excluded.brief_hour,tz_offset_minutes=excluded.tz_offset_minutes,'
                       'daily_budget_microusd=excluded.daily_budget_microusd,last_patrol_at=excluded.last_patrol_at,'
                       'last_brief_date=excluded.last_brief_date,updated_at=excluded.updated_at',
                       (tenant, current['patrol_minutes'], current['brief_hour'], current['tz_offset_minutes'],
                        current['daily_budget_microusd'], current['last_patrol_at'], current['last_brief_date'], now))
            if set(changes) - {'last_patrol_at', 'last_brief_date'}:
                self._audit(db, tenant, 'settings', dict(op='settings.updated', **{k: v for k, v in changes.items()}), actor)
        return self.settings(tenant)

    def heartbeat(self, tenant, executor_id, search_provider):
        with self.store.transaction() as db:
            db.execute('INSERT INTO ms_runtime VALUES(?,?,?,?) ON CONFLICT(tenant) DO UPDATE SET executor_id=excluded.executor_id,'
                       'search_provider=excluded.search_provider,heartbeat_at=excluded.heartbeat_at',
                       (tenant, executor_id, search_provider, self.store.now(db)))

    def runtime(self, tenant):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_runtime WHERE tenant=?', (tenant,)).fetchone()
            now = self.store.now(db)
        if row is None:
            return dict(online=False, search_provider=None, heartbeat_age_seconds=None)
        age = now - row['heartbeat_at']
        return dict(online=age < 30, search_provider=row['search_provider'], heartbeat_age_seconds=round(age, 1),
                    executor_id=row['executor_id'])
