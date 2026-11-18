"""Personal product settings, separate from the operator's private configuration.

Saved templates are explicit assistant-message actions, never shell hooks.
Memory is subject scoped; imports and exports are redacted like conversation data.
"""
import json
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import is_human
from .store import Conflict, InvalidInput, NotFound, canonical, digest
from .workflow import ROLE_SKILLS
from monsoon.safety import redact, redact_value
from monsoon.store import AssistantStore

KINDS = ('memory', 'skills', 'commands', 'hooks')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS product_entries (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL,
 title TEXT NOT NULL, content TEXT NOT NULL, enabled INTEGER NOT NULL,
 config TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(tenant,subject,id));
CREATE TABLE IF NOT EXISTS product_archives (
 tenant TEXT NOT NULL, subject TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
 body TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(tenant,subject,id));
'''


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class EntryIn(Body):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(default='', max_length=4000)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class EntryUpdate(Body):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class RunIn(Body):
    input: str = Field(default='', max_length=3000)


class ArchiveIn(Body):
    title: str = Field(min_length=1, max_length=120)


class UserSettings:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript(SCHEMA)

    @staticmethod
    def _entry(row):
        return dict(id=row['id'], kind=row['kind'], title=redact(row['title']), content=redact(row['content']),
                    enabled=bool(row['enabled']), config=redact_value(json.loads(row['config'])), revision=row['revision'],
                    created_at=row['created_at'], updated_at=row['updated_at'])

    def entries(self, tenant, subject, kind):
        if kind not in KINDS:
            raise NotFound('setting kind not found')
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM product_entries WHERE tenant=? AND subject=? AND kind=? ORDER BY created_at,id',
                              (tenant, subject, kind)).fetchall()
        return [self._entry(row) for row in rows]

    def entry(self, db, tenant, subject, kind, entry_id):
        row = db.execute('SELECT * FROM product_entries WHERE tenant=? AND subject=? AND kind=? AND id=?',
                         (tenant, subject, kind, entry_id)).fetchone()
        if row is None:
            raise NotFound('setting not found')
        return self._entry(row)

    def memory(self, tenant, subject):
        """The runtime may include only these enabled personal memories as data."""
        return [entry['content'] for entry in self.entries(tenant, subject, 'memory') if entry['enabled']][:20]

    @staticmethod
    def validate(kind, title, content, config):
        if kind not in KINDS:
            raise NotFound('setting kind not found')
        if not title.strip() or '\x00' in title + content:
            raise InvalidInput('title and content must be readable text')
        if kind != 'hooks' and not content.strip():
            raise InvalidInput('content required')
        if kind == 'hooks':
            if set(config) - {'event', 'action'} or config.get('event') not in {'reply.completed', 'task.completed'} or config.get('action') != 'browser.notification':
                raise InvalidInput('only browser notification hooks are supported')
        elif config:
            raise InvalidInput('unsupported template configuration')

    def write(self, identity, key, kind, body, *, entry_id=None):
        tenant, subject = identity.tenant_id, identity.subject_id
        args = dict(subject=subject, kind=kind, entry_id=entry_id, body=body)

        def apply(db):
            now = self.store.now(db)
            if entry_id is None:
                count = db.execute('SELECT COUNT(*) FROM product_entries WHERE tenant=? AND subject=? AND kind=?',
                                   (tenant, subject, kind)).fetchone()[0]
                if count >= 100:
                    raise Conflict('personal setting limit reached')
                item = dict(id='pe-' + uuid4().hex[:20], kind=kind, **body, revision=1, created_at=now, updated_at=now)
                self.validate(kind, item['title'], item['content'], item['config'])
                item['title'], item['content'] = redact(item['title']), redact(item['content'])
                db.execute('INSERT INTO product_entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (tenant, subject, item['id'], kind, item['title'], item['content'], int(item['enabled']),
                            canonical(item['config']), 1, now, now))
            else:
                item = self.entry(db, tenant, subject, kind, entry_id)
                if body['expected_revision'] != item['revision']:
                    raise Conflict('setting changed; reload before saving')
                item.update({k: v for k, v in body.items() if k != 'expected_revision' and v is not None})
                self.validate(kind, item['title'], item['content'], item['config'])
                item['revision'] += 1
                item['updated_at'] = now
                item['title'], item['content'] = redact(item['title']), redact(item['content'])
                db.execute('UPDATE product_entries SET title=?,content=?,enabled=?,config=?,revision=?,updated_at=? WHERE tenant=? AND subject=? AND id=?',
                           (item['title'], item['content'], int(item['enabled']), canonical(item['config']), item['revision'], now,
                            tenant, subject, entry_id))
            return item
        return self.store._command(tenant, 'product:' + digest(dict(subject=subject, key=key)), 'product.entry.write', args, apply)

    def delete(self, identity, key, kind, entry_id, expected_revision):
        tenant, subject = identity.tenant_id, identity.subject_id

        def apply(db):
            item = self.entry(db, tenant, subject, kind, entry_id)
            if item['revision'] != expected_revision:
                raise Conflict('setting changed; reload before deleting')
            db.execute('DELETE FROM product_entries WHERE tenant=? AND subject=? AND id=?', (tenant, subject, entry_id))
            return dict(deleted=True)
        return self.store._command(tenant, 'product:' + digest(dict(subject=subject, key=key)), 'product.entry.delete',
                                   dict(subject=subject, kind=kind, id=entry_id, revision=expected_revision), apply)


def install_product_settings_api(app, store, *, Reader, Writer, Key, executor_config=None):
    data = UserSettings(store)
    assistant = AssistantStore(store)
    app.state.product_settings = data

    def human(identity):
        if not is_human(identity):
            raise HTTPException(403, 'personal settings require a named human user')

    @app.get('/v1/product/capabilities')
    def capabilities(identity: Reader):
        human(identity)
        import os
        browser_ready = getattr(app.state, 'browser_inspector', None) is not None
        computer = getattr(app.state, 'computer_control', None)
        computer_ready = computer is not None and os.name == 'nt'
        return dict(
            browser=dict(available=browser_ready, mode='guarded-html-pick' if browser_ready else None,
                         reason=None if browser_ready else '浏览器点选只在本机控制台启用。'),
            computer=dict(available=computer_ready, mounted=computer is not None,
                          reason='本机屏幕控制已挂载。' if computer_ready else (
                              '屏幕采集目前只在 Windows 上可用；本机接口已挂载。' if computer is not None
                              else '尚未挂载屏幕与输入控制。')),
            remote_devices=dict(available=False, reason='尚未配置远程设备配对服务。'),
            subagents=dict(available=True, mode='pipeline_roles', roles=sorted(ROLE_SKILLS),
                           spawning_available=False, reason='编程流水线使用这些角色；自由派生子会话尚未接入。'),
            hooks=dict(available=True, actions=['browser.notification'], events=['reply.completed', 'task.completed']),
            models=dict(server_managed=True),
        )

    @app.get('/v1/product/entries/{kind}')
    def entries(kind: str, identity: Reader):
        human(identity)
        return dict(items=data.entries(identity.tenant_id, identity.subject_id, kind))

    @app.post('/v1/product/entries/{kind}', status_code=201)
    def add_entry(kind: str, body: EntryIn, identity: Writer, idempotency_key: Key):
        human(identity)
        return data.write(identity, idempotency_key, kind, body.model_dump())

    @app.patch('/v1/product/entries/{kind}/{entry_id}')
    def update_entry(kind: str, entry_id: str, body: EntryUpdate, identity: Writer, idempotency_key: Key):
        human(identity)
        return data.write(identity, idempotency_key, kind, body.model_dump(), entry_id=entry_id)

    @app.delete('/v1/product/entries/{kind}/{entry_id}')
    def delete_entry(kind: str, entry_id: str, identity: Writer, idempotency_key: Key,
                     expected_revision: int = Query(ge=1)):
        human(identity)
        return data.delete(identity, idempotency_key, kind, entry_id, expected_revision)

    @app.post('/v1/product/entries/{kind}/{entry_id}/run', status_code=201)
    def run_template(kind: str, entry_id: str, body: RunIn, identity: Writer, idempotency_key: Key):
        human(identity)
        if kind not in {'skills', 'commands'}:
            raise InvalidInput('only skill and command templates can be sent')
        with store.connect() as db:
            item = data.entry(db, identity.tenant_id, identity.subject_id, kind, entry_id)
        if not item['enabled']:
            raise Conflict('template is disabled')
        text = item['content'] + ('\n\n' + body.input if body.input else '')
        if len(text) > 4000:
            raise InvalidInput('template and input exceed 4000 characters')
        thread = assistant.thread_for(identity.tenant_id, identity.subject_id)
        meta = dict(template_id=entry_id, template_kind=kind)
        workspace = getattr(app.state, 'workspace', None)
        if workspace is not None:
            preferences = workspace.preferences(identity)
            meta['confirmation_mode'] = preferences.get('confirmation_mode', 'ask')
            alias = preferences.get('preferred_model')
            if alias:
                if alias not in {model['alias'] for model in workspace.models()['items']}:
                    raise InvalidInput('selected model is not configured')
                meta['model_alias'] = alias
            project_id = preferences.get('selected_project_id')
            if project_id:
                project = workspace.project(identity, project_id)
                meta.update(project_id=project_id, project_name=project['name'], project_path=project.get('path'))

        def apply(db):
            return assistant.add_message(identity.tenant_id, thread['id'], role='user', kind='text', body=text,
                                         status='queued', meta=meta,
                                         author='subject:' + identity.subject_id, db=db)
        return store._command(identity.tenant_id, 'product:' + digest(dict(subject=identity.subject_id, key=idempotency_key)),
                              'product.template.run', dict(subject=identity.subject_id, id=entry_id, input=body.input), apply)

    @app.get('/v1/product/usage')
    def usage(identity: Reader):
        human(identity)
        tenant, subject = identity.tenant_id, identity.subject_id
        thread = assistant.thread_of(tenant, subject)
        run_ids = set()
        message_count = task_count = 0
        with store.connect() as db:
            if thread:
                tid = thread['id']
                message_count = db.execute('SELECT COUNT(*) FROM ms_messages WHERE tenant=? AND thread_id=?', (tenant, tid)).fetchone()[0]
                tasks = db.execute('SELECT id FROM ms_tasks WHERE tenant=? AND thread_id=?', (tenant, tid)).fetchall()
                task_count = len(tasks)
                owners = [tid] + [row['id'] for row in tasks]
                for owner in owners:
                    run_ids.update(row['run_id'] for row in db.execute('SELECT run_id FROM ms_jobs WHERE tenant=? AND owner=?', (tenant, owner)))
            session_ids = [row['id'] for row in db.execute("SELECT id FROM autopilot_sessions WHERE tenant=? AND json_extract(record,'$.operator_id')=?", (tenant, subject))]
            for session_id in session_ids:
                run_ids.update(row['run_id'] for row in db.execute('SELECT run_id FROM autopilot_links WHERE tenant=? AND session_id=?', (tenant, session_id)))
            totals = dict(spent_microusd=0, reserved_microusd=0, tokens=0)
            for run_id in run_ids:
                row = db.execute('SELECT spent,reserved,tokens_spent FROM runs WHERE tenant=? AND id=?', (tenant, run_id)).fetchone()
                if row:
                    totals['spent_microusd'] += row['spent']; totals['reserved_microusd'] += row['reserved']; totals['tokens'] += row['tokens_spent']
        return dict(**totals, messages=message_count, tasks=task_count, coding_sessions=len(session_ids), runs=len(run_ids),
                    scope='current_user', source='persisted_execution_ledger')

    @app.get('/v1/product/archives')
    def archives(identity: Reader):
        human(identity)
        with store.connect() as db:
            rows = db.execute('SELECT id,title,created_at FROM product_archives WHERE tenant=? AND subject=? ORDER BY created_at DESC',
                              (identity.tenant_id, identity.subject_id)).fetchall()
        return dict(items=[dict(row) for row in rows])

    @app.post('/v1/product/archives', status_code=201)
    def create_archive(body: ArchiveIn, identity: Writer, idempotency_key: Key):
        human(identity)
        if not body.title.strip() or '\x00' in body.title:
            raise InvalidInput('archive title required')
        tenant, subject = identity.tenant_id, identity.subject_id

        def apply(db):
            count = db.execute('SELECT COUNT(*) FROM product_archives WHERE tenant=? AND subject=?', (tenant, subject)).fetchone()[0]
            if count >= 20:
                raise Conflict('archive limit reached')
            thread = db.execute('SELECT id FROM ms_threads WHERE tenant=? AND subject=?', (tenant, subject)).fetchone()
            messages = []
            if thread:
                rows = db.execute('SELECT role,kind,text,status,created_at FROM ms_messages WHERE tenant=? AND thread_id=? ORDER BY created_at,rowid',
                                  (tenant, thread['id'])).fetchall()
                messages = [redact_value(dict(row)) for row in rows]
            export = dict(schema_version='verilo.personal-archive/v1', created_at=store.now(db), messages=messages)
            payload = canonical(export)
            if len(payload.encode()) > 2_000_000:
                raise Conflict('conversation too large for a local snapshot')
            item = dict(id='ar-' + uuid4().hex[:20], title=redact(body.title), created_at=export['created_at'])
            db.execute('INSERT INTO product_archives VALUES(?,?,?,?,?,?)', (tenant, subject, item['id'], item['title'], payload, item['created_at']))
            return item
        return store._command(tenant, 'product:' + digest(dict(subject=subject, key=idempotency_key)), 'product.archive.create',
                              dict(subject=subject, title=body.title), apply)

    @app.get('/v1/product/archives/{archive_id}')
    def export_archive(archive_id: str, identity: Reader):
        human(identity)
        with store.connect() as db:
            row = db.execute('SELECT body FROM product_archives WHERE tenant=? AND subject=? AND id=?',
                             (identity.tenant_id, identity.subject_id, archive_id)).fetchone()
        if row is None:
            raise NotFound('archive not found')
        return JSONResponse(redact_value(json.loads(row['body'])), headers={'Content-Disposition': 'attachment; filename="verilo-conversation.json"', 'Cache-Control': 'no-store'})

    @app.delete('/v1/product/archives/{archive_id}')
    def delete_archive(archive_id: str, identity: Writer, idempotency_key: Key):
        human(identity)
        def apply(db):
            found = db.execute('DELETE FROM product_archives WHERE tenant=? AND subject=? AND id=?', (identity.tenant_id, identity.subject_id, archive_id))
            if not found.rowcount:
                raise NotFound('archive not found')
            return dict(deleted=True)
        return store._command(identity.tenant_id, 'product:' + digest(dict(subject=identity.subject_id, key=idempotency_key)),
                              'product.archive.delete', dict(subject=identity.subject_id, id=archive_id), apply)

    return data
