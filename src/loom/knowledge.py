"""Isolated knowledge with independent review (FR-004) and scoped memory (FR-005).

Knowledge is proposed by one subject and becomes recallable only after a
different named subject approves it; rejected or pending entries are never
recalled. Memory is scoped to a conversation thread, a task run, or the tenant
(cross-task). Everything is tenant-isolated and append-only.
"""
from __future__ import annotations

import re

from .platform_ledger import Ledger, conflict, new_id, require, text
from .store import InvalidInput, NotFound

MEMORY_SCOPES = ('session', 'task', 'tenant')


def _terms(value):
    """Words for Latin text plus character bigrams for CJK, lower-cased."""
    value = value.lower()
    words = set(re.findall(r'[a-z0-9_]{2,}', value))
    for run in re.findall(r'[一-鿿]+', value):
        words.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return words


def _rank(query, items, fields):
    wanted = _terms(query)
    scored = []
    for item in items:
        have = set().union(*(_terms(str(item.get(f, ''))) for f in fields))
        overlap = len(wanted & have)
        if overlap:
            scored.append((overlap / max(1, len(wanted)), item))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get('created_at', 0)))
    return [dict(item, relevance=round(score, 3)) for score, item in scored]


class KnowledgeBase:
    KIND = 'knowledge'

    def __init__(self, store, *, ledger=None):
        self.ledger = ledger or Ledger(store)

    def propose(self, tenant, key, *, title, body, tags=(), actor):
        text(title, 'title', 200)
        text(body, 'body', 8000)
        require(isinstance(tags, (list, tuple)) and len(tags) <= 10 and all(isinstance(t, str) and 0 < len(t) <= 40 for t in tags),
                'tags must be up to 10 short strings')
        entry_id = 'kn-' + new_id()
        record = dict(schema_version='loom.knowledge/v1', entry_id=entry_id, title=title, body=body, tags=sorted(set(tags)),
                      status='proposed', author=actor, reviewer=None, review_note=None)
        result = self.ledger.append(tenant, key, self.KIND, entry_id, record, actor)
        return dict(result['record'], seq=result['seq'], recallable=False)

    def review(self, tenant, key, entry_id, *, decision, note, actor):
        require(decision in {'approved', 'rejected'}, 'decision must be approved or rejected')
        text(note, 'review note', 2000)

        def check(db):
            current = self.ledger.latest_in(db, tenant, self.KIND, entry_id)
            if current is None:
                raise NotFound('knowledge entry not found')
            conflict(current['record']['status'] == 'proposed', 'knowledge entry was already reviewed')
            conflict(current['record']['author'] != actor, 'independent review required: the author cannot approve their own entry')
        with self.ledger.store.connect() as db:
            current = self.ledger.latest_in(db, tenant, self.KIND, entry_id)
        if current is None:
            raise NotFound('knowledge entry not found')
        record = dict(current['record'], status=decision, reviewer=actor, review_note=note)
        result = self.ledger.append(tenant, key, self.KIND, entry_id, record, actor, check=check)
        return dict(result['record'], seq=result['seq'], recallable=decision == 'approved')

    def list(self, tenant, *, status=None):
        items = [dict(r['record'], created_at=r['ts']) for r in self.ledger.current(tenant, self.KIND)]
        return [i for i in items if status is None or i['status'] == status]

    def recall(self, tenant, query, *, limit=5):
        text(query, 'query', 500)
        approved = [i for i in self.list(tenant, status='approved')]
        return _rank(query, approved, ('title', 'body', 'tags'))[:max(1, min(int(limit), 20))]


class Memory:
    KIND = 'memory'

    def __init__(self, store, *, ledger=None):
        self.ledger = ledger or Ledger(store)

    def write(self, tenant, key, *, scope, scope_id, content, actor, source='user'):
        require(scope in MEMORY_SCOPES, 'scope must be session, task or tenant')
        scope_id = 'tenant' if scope == 'tenant' else text(scope_id, 'scope id', 100)
        text(content, 'memory content', 4000)
        require(source in {'user', 'autopilot'}, 'invalid memory source')
        entry_id = 'mem-' + new_id()
        record = dict(schema_version='loom.memory/v1', entry_id=entry_id, scope=scope, scope_id=scope_id, content=content,
                      source=source, untrusted_data=True)
        return self.ledger.append(tenant, key, self.KIND, entry_id, record, actor)['record']

    def list(self, tenant, *, scope=None, scope_id=None):
        items = [dict(r['record'], created_at=r['ts']) for r in self.ledger.current(tenant, self.KIND)]
        return [i for i in items if (scope is None or i['scope'] == scope) and (scope_id is None or i['scope_id'] == scope_id)]

    def recall(self, tenant, query, *, session_id=None, task_id=None, limit=5):
        """Session first, then task, then tenant-wide memory; never other tenants."""
        text(query, 'query', 500)
        pools = [('session', session_id), ('task', task_id), ('tenant', 'tenant')]
        results = []
        for scope, scope_id in pools:
            if scope_id is None:
                continue
            for item in _rank(query, self.list(tenant, scope=scope, scope_id=scope_id), ('content',)):
                results.append(item)
        return results[:max(1, min(int(limit), 20))]
