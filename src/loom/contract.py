"""Cross-machine contract support (monsoon-harness plan v2.2, batch 3).

Loom stays the audited centre; external executors (ms instances) poll it. This
module holds the small, deterministic pieces the contract adds:

* per-consumer event cursors (``after_seq``/``limit``) with an explicit invalid
  cursor marker instead of silently restarting;
* requester attribution: a ``requested-by:<user>`` description prefix is parsed
  synchronously when a goal is created and stored in a derived table;
* execution-domain policy: a tenant default (``local``/``remote``) and an
  optional per-agent override, with the effective value and its source.

No server push exists: consumers own their cursors and revocation is learned
by polling (plan sections 8.2 and 8.4).
"""
from __future__ import annotations

import json
import re

from .store import Conflict, InvalidInput, NotFound

REQUESTER_PREFIX = 'requested-by:'
_REQUESTER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}')
EXECUTION_DOMAINS = ('local', 'remote')
MAX_PAGE = 1000

SCHEMA = '''
CREATE TABLE IF NOT EXISTS goal_requesters (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, requester TEXT, source TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(tenant,run_id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE INDEX IF NOT EXISTS goal_requesters_by_user ON goal_requesters(tenant,requester);
CREATE TRIGGER IF NOT EXISTS goal_requesters_no_update BEFORE UPDATE ON goal_requesters
 BEGIN SELECT RAISE(ABORT,'requester attribution is append-only'); END;
CREATE TABLE IF NOT EXISTS tenant_execution_policy (
 tenant TEXT NOT NULL, revision INTEGER NOT NULL, default_execution_domain TEXT NOT NULL
   CHECK(default_execution_domain IN ('local','remote')),
 actor TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(tenant,revision));
CREATE TRIGGER IF NOT EXISTS tenant_execution_policy_no_update BEFORE UPDATE ON tenant_execution_policy
 BEGIN SELECT RAISE(ABORT,'execution policy revisions are append-only'); END;
'''


class CursorInvalid(Conflict):
    code = 'CURSOR_INVALID'


def parse_requester(description):
    """Return the user named by a leading ``requested-by:`` prefix, or None.

    A malformed prefix is a readable error, never a silently dropped value.
    """
    if not isinstance(description, str) or not description.startswith(REQUESTER_PREFIX):
        return None
    token = description[len(REQUESTER_PREFIX):].split(None, 1)[0] if description[len(REQUESTER_PREFIX):].strip() else ''
    if not _REQUESTER.fullmatch(token):
        raise InvalidInput('requested-by prefix must be followed by a user identifier, for example "requested-by:alice 需求…"')
    return token


def record_requester(store, db, tenant, run_id, description):
    requester = parse_requester(description)
    db.execute('INSERT OR IGNORE INTO goal_requesters VALUES(?,?,?,?,?)',
               (tenant, run_id, requester, 'description-prefix' if requester else 'none', store.now(db)))
    return requester


def requester_rows(store, tenant, *, run_id=None, requester=None):
    query, args = 'SELECT run_id,requester,source,created_at FROM goal_requesters WHERE tenant=?', [tenant]
    if run_id is not None:
        query += ' AND run_id=?'
        args.append(run_id)
    if requester is not None:
        query += ' AND requester=?'
        args.append(requester)
    with store.connect() as db:
        return [dict(r) for r in db.execute(query + ' ORDER BY created_at,rowid LIMIT 1000', args)]


def events_page(store, tenant, run_id, *, after_seq=0, limit=100):
    """One consumer's page: events with seq > after_seq, oldest first."""
    if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise InvalidInput(f'after_seq must be >= 0 and limit between 1 and {MAX_PAGE}')
    with store.connect() as db:
        store._run(db, tenant, run_id)
        head = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE tenant=? AND run_id=?', (tenant, run_id)).fetchone()[0]
        if after_seq > head:
            raise CursorInvalid('cursor points past the last event of this run; restart from after_seq=0')
        rows = db.execute('SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND seq>? ORDER BY seq LIMIT ?',
                          (tenant, run_id, after_seq, limit + 1)).fetchall()
    items = [{**json.loads(r['envelope']), 'hash': r['hash']} for r in rows[:limit]]
    return dict(events=items, after_seq=after_seq, next_after_seq=items[-1]['seq'] if items else after_seq,
                has_more=len(rows) > limit, head_seq=head, cursor_owner='consumer', push='none')


class ExecutionDomains:
    """Tenant default + per-agent override; not a security boundary (plan 6.2)."""

    def __init__(self, store):
        self.store = store

    def policy(self, tenant):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM tenant_execution_policy WHERE tenant=? ORDER BY revision DESC LIMIT 1',
                             (tenant,)).fetchone()
        if row is None:
            return dict(tenant_id=tenant, default_execution_domain='local', revision=0, source='builtin-default')
        return dict(tenant_id=tenant, default_execution_domain=row['default_execution_domain'], revision=row['revision'],
                    actor=row['actor'], reason=row['reason'], updated_at=row['created_at'], source='tenant-policy')

    def set_default(self, tenant, key, *, domain, expected_revision, reason, actor):
        if domain not in EXECUTION_DOMAINS:
            raise InvalidInput('default_execution_domain must be local or remote')
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidInput('expected_revision must be a nonnegative integer')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise InvalidInput('a reason is required')
        args = dict(domain=domain, expected_revision=expected_revision, reason=reason, actor=actor)

        def apply(db):
            row = db.execute('SELECT COALESCE(MAX(revision),0) FROM tenant_execution_policy WHERE tenant=?', (tenant,)).fetchone()
            if row[0] != expected_revision:
                raise Conflict('execution policy changed; reread it before updating')
            db.execute('INSERT INTO tenant_execution_policy VALUES(?,?,?,?,?,?)',
                       (tenant, expected_revision + 1, domain, actor, reason, self.store.now(db)))
            return dict(tenant_id=tenant, default_execution_domain=domain, revision=expected_revision + 1)
        return self.store._command(tenant, key, 'contract.execution_policy', args, apply)

    def effective(self, tenant, agent=None):
        """Effective domain and where it came from (inherited vs override)."""
        policy = self.policy(tenant)
        override = (agent or {}).get('execution_domain')
        if override is not None:
            return dict(execution_domain=override, source='agent-override', tenant_default=policy['default_execution_domain'])
        return dict(execution_domain=policy['default_execution_domain'], source='inherited-from-tenant',
                    tenant_default=policy['default_execution_domain'])


def register_effect(store, claim, *, action_id, effect_level, request):
    """Register an external executor's side-effect intent before acting.

    Same trusted intent ledger as Loom's own gateway calls: if the executor dies
    before finishing, recovery sees an unresolved intent and holds the task for
    a person instead of replaying the irreversible action (plan 8.1, criterion G1).
    """
    if effect_level not in {'read', 'write', 'external_write', 'irreversible'}:
        raise InvalidInput('effect_level must be read, write, external_write or irreversible')
    if not isinstance(action_id, str) or not 1 <= len(action_id) <= 200:
        raise InvalidInput('action_id is required')
    body = dict(kind='external-executor-effect', action_id=action_id, effect_level=effect_level, request=request)
    try:
        json.dumps(body, allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidInput('effect request must be plain JSON') from None
    if len(json.dumps(body)) > 16384:
        raise InvalidInput('effect request is too large')
    return store.begin_effect(claim, 'external:' + action_id, body)


def lookup_effect(store, tenant, run_id, call_id):
    with store.connect() as db:
        store._run(db, tenant, run_id)
        row = db.execute('SELECT id,task_id,epoch,state FROM effects WHERE tenant=? AND run_id=? AND id=?',
                         (tenant, run_id, call_id)).fetchone()
    if row is None:
        raise NotFound('effect not found')
    return dict(row)
