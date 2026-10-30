"""Versioned data-only domain assets; adapters and permissions remain code-owned.

Publication is a named operator action anchored to an existing Goal event chain.
It never creates runs, executes a template, changes defaults or approves a plan.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .artifacts import _subject, _text
from .builtin_resources import software_workflow
from .pack_refs import PackName, PackReference, PackVersion
from .store import Conflict, InvalidInput, NotFound, Store, canonical, digest
from .workflow import AgentRole, Identifier, StrictModel, TaskNode, WorkflowDefinition


class PackTemplate(StrictModel):
    id: Identifier
    title: str = Field(min_length=1, max_length=200)
    nodes: list[TaskNode] = Field(min_length=1, max_length=100)


class DomainPack(StrictModel):
    schema_version: Literal['loom.domain-pack/v1']
    name: PackName
    version: PackVersion
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    scope: Literal['local-workspace']
    roles: list[AgentRole] = Field(min_length=7, max_length=7)
    templates: list[PackTemplate] = Field(min_length=1, max_length=10)

    @model_validator(mode='after')
    def portable_templates(self):
        if len({template.id for template in self.templates}) != len(self.templates):
            raise ValueError('duplicate template identifiers')
        if not self.title.strip() or not self.description.strip():
            raise ValueError('pack text cannot be blank')
        for template in self.templates:
            WorkflowDefinition(schema_version='loom.workflow/v1', pack='software-dev',
                scope='local-workspace', roles=self.roles, nodes=template.nodes)
            if any(node.review_subject is not None or node.development_suite is not None
                   or node.evaluation_digest is not None or node.repair is not None for node in template.nodes):
                raise ValueError('portable templates cannot contain run-specific review or evaluation references')
        return self


SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_packs (
 tenant TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
 content_digest TEXT NOT NULL, bundle TEXT NOT NULL,
 run_id TEXT NOT NULL, actor TEXT NOT NULL, event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,name,version),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TRIGGER IF NOT EXISTS domain_packs_no_update BEFORE UPDATE ON domain_packs
 BEGIN SELECT RAISE(ABORT,'domain packs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS domain_packs_no_delete BEFORE DELETE ON domain_packs
 BEGIN SELECT RAISE(ABORT,'domain packs are append-only'); END;
"""


def _checked(source):
    try:
        raw = source.model_dump(mode='json') if isinstance(source, DomainPack) else source
        pack = DomainPack.model_validate(raw)
        if len(canonical(pack.model_dump(mode='json')).encode('utf-8')) > 262144:
            raise ValueError('domain pack exceeds 256 KiB')
        return pack
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise InvalidInput('invalid or unsupported domain pack') from None


def builtin_pack():
    workflow = software_workflow()
    return _checked(dict(schema_version='loom.domain-pack/v1', name='software-dev', version='0.1.0',
        title='软件开发', description='七角色本机工作流模板；运行前必须单独审批与绑定终验。',
        scope='local-workspace', roles=workflow['roles'],
        templates=[dict(id='seven-roles', title='七角色开发流程', nodes=workflow['nodes'])]))


def _view(pack, *, origin, audit_run_id=None, actor=None, event_hash=None):
    body = pack.model_dump(mode='json')
    reference = PackReference(name=pack.name, version=pack.version, content_digest=digest(body)).model_dump(mode='json')
    return dict(reference=reference, bundle=body, origin=origin, audit_run_id=audit_run_id,
                actor=actor, event_hash=event_hash, grants_authority=False, plan_approved=False)


class DomainPackService:
    def __init__(self, store: Store, *, initialize=True):
        self.store = store
        if initialize:
            with store.connect() as db:
                db.executescript(SCHEMA)

    def publish(self, tenant, key, audit_run_id, bundle, *, actor):
        _text(tenant, 'tenant'); _text(key, 'idempotency key'); _text(audit_run_id, 'audit run ID')
        _subject(actor)
        pack = _checked(bundle)
        if (pack.name, pack.version) == ('software-dev', '0.1.0'):
            raise Conflict('built-in domain pack version is read-only')
        view = _view(pack, origin='tenant', audit_run_id=audit_run_id, actor=actor)
        args = dict(audit_run_id=audit_run_id, bundle=view['bundle'], actor=actor)

        def apply(db):
            # Attach to an existing Goal without changing its tasks, plan or budget.
            if db.execute('SELECT 1 FROM workflow_goals WHERE tenant=? AND run_id=?', (tenant, audit_run_id)).fetchone() is None:
                raise NotFound('audit Goal not found in tenant')
            if self.store._run(db, tenant, audit_run_id)['status'] not in {'draft', 'open', 'paused', 'awaiting_human'}:
                raise Conflict('terminal Goal cannot receive new asset publications')
            if db.execute('SELECT 1 FROM domain_packs WHERE tenant=? AND name=? AND version=?',
                          (tenant, pack.name, pack.version)).fetchone():
                raise Conflict('domain pack version already exists; publish a new version')
            event = self.store._event(db, tenant, audit_run_id, 'domain_pack.published', view, actor, key)
            db.execute('INSERT INTO domain_packs VALUES (?,?,?,?,?,?,?,?)',
                (tenant, pack.name, pack.version, view['reference']['content_digest'], canonical(view['bundle']),
                 audit_run_id, actor, event['hash']))
            return {**view, 'event_hash': event['hash']}
        return self.store._command(tenant, key, 'domain_pack.publish', args, apply)

    def _get_in(self, db, tenant, name, version):
        try:
            # Validate path identifiers without accepting filesystem paths or URLs.
            PackReference(name=name, version=version, content_digest='0'*64)
        except (ValidationError, TypeError):
            raise InvalidInput('invalid domain pack identity') from None
        if (name, version) == ('software-dev', '0.1.0'):
            return _view(builtin_pack(), origin='builtin')
        row = db.execute('SELECT * FROM domain_packs WHERE tenant=? AND name=? AND version=?',
                         (tenant, name, version)).fetchone()
        if row is None:
            raise NotFound('domain pack version not found in tenant')
        try:
            pack = _checked(json.loads(row['bundle']))
            if (pack.name, pack.version) != (name, version):
                raise ValueError
            view = _view(pack, origin='tenant', audit_run_id=row['run_id'], actor=row['actor'])
            if view['reference']['content_digest'] != row['content_digest']:
                raise ValueError
            event = db.execute('SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND hash=?',
                                (tenant, row['run_id'], row['event_hash'])).fetchone()
            if event is None:
                raise ValueError
            envelope = json.loads(event['envelope'])
            if (digest(envelope) != event['hash'] or envelope['type'] != 'domain_pack.published'
                    or envelope['tenant_id'] != tenant or envelope['run_id'] != row['run_id']
                    or envelope['actor'] != row['actor'] or envelope['payload'] != view):
                raise ValueError
        except (InvalidInput, ValueError, KeyError, TypeError, RecursionError):
            raise Conflict('domain pack content or audit binding is inconsistent') from None
        return {**view, 'event_hash': row['event_hash']}

    def get(self, tenant, name, version):
        _text(tenant, 'tenant')
        with self.store.connect() as db:
            db.execute('BEGIN')
            return self._get_in(db, tenant, name, version)

    def catalog(self, tenant):
        _text(tenant, 'tenant')
        with self.store.connect() as db:
            db.execute('BEGIN')
            identities = [('software-dev', '0.1.0')] + [tuple(row) for row in db.execute(
                'SELECT name,version FROM domain_packs WHERE tenant=? ORDER BY name,version', (tenant,))]
            items = [self._get_in(db, tenant, name, version) for name, version in identities]
            return [dict(reference=view['reference'], title=view['bundle']['title'], origin=view['origin'],
                templates=[dict(id=t['id'], title=t['title']) for t in view['bundle']['templates']],
                grants_authority=False) for view in items]

    def template(self, tenant, name, version, template_id):
        view = self.get(tenant, name, version)
        template = next((t for t in view['bundle']['templates'] if t['id'] == template_id), None)
        if template is None:
            raise NotFound('template not found in domain pack version')
        definition = WorkflowDefinition(schema_version='loom.workflow/v1', pack=name,
            pack_ref=PackReference.model_validate(view['reference']), scope='local-workspace',
            roles=view['bundle']['roles'], nodes=template['nodes'])
        return dict(reference=view['reference'], template_id=template_id, definition=definition.model_dump(mode='json'),
                    plan_approved=False, grants_authority=False)

    def resolve_in(self, db, tenant, definition):
        reference = definition.pack_ref
        view = self._get_in(db, tenant, reference.name, reference.version)
        if (view['reference'] != reference.model_dump(mode='json')
                or canonical(view['bundle']['roles']) != canonical([role.model_dump(mode='json') for role in definition.roles])):
            raise Conflict('domain pack reference or frozen roles differ from the published version')
        return view
