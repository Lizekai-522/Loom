"""HTTP surface of the assistant (installed into the Loom FastAPI app).

The API process never calls a model: it stores messages, decisions and
settings, and streams the per-thread event log to the browser (same-origin
server-sent events). Approvals, mandates and task completion are human-only.
"""

import asyncio
import json
from typing import Any, Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from loom.auth import is_human
from loom.store import Conflict, InvalidInput, NotFound

from .mail import MAIL_FAILURE, MailStore, PLACEHOLDERS
from .store import AssistantStore, MANDATE_SCOPES, RETRY_SAFE_BLOCKS

STREAM_SECONDS = 300


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, strict=True)


class MessageIn(Body):
    text: str = Field(min_length=1, max_length=8000)
    model_alias: str | None = Field(default=None, min_length=1, max_length=100)
    project_id: str | None = Field(default=None, min_length=1, max_length=100)
    confirmation_mode: Literal['ask', 'read_only'] | None = None


class TodoIn(Body):
    text: str = Field(min_length=1, max_length=1000)
    due: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    research: bool = False


class TodoUpdate(Body):
    status: Literal['open', 'done', 'dismissed']


class DecisionIn(Body):
    decision: Literal['approved', 'rejected']


class MandateIn(Body):
    scopes: list[Literal['research', 'resume']] = Field(min_length=1, max_length=len(MANDATE_SCOPES))
    daily_budget_microusd: int = Field(gt=0, le=20_000_000)
    days: int = Field(ge=1, le=90)


class DraftIn(Body):
    to: list[str] = Field(min_length=1, max_length=20)
    cc: list[str] = Field(default_factory=list, max_length=20)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=50000)
    expected_version: int = Field(ge=1)


class SendIn(Body):
    expected_version: int = Field(ge=1)


class SettingsIn(Body):
    patrol_minutes: int | None = Field(default=None, ge=15, le=1440)
    brief_hour: int | None = Field(default=None, ge=-1, le=23)
    tz_offset_minutes: int | None = Field(default=None, ge=-720, le=840)
    daily_budget_microusd: int | None = Field(default=None, ge=0, le=20_000_000)


def install(app, store, *, Tenant, Reader, Writer, Approver, Key, sessions):
    data = AssistantStore(store)
    mail = MailStore(data)

    def own_thread(identity, thread_id):
        thread = data.thread(identity.tenant_id, thread_id)
        if thread['subject'] != identity.subject_id:
            raise NotFound('thread not found')
        return thread

    # Tasks, todos and approvals belong to one person's thread. Another principal in
    # the same tenant (a second user, an operator or a worker) sees none of them.
    def mine(identity):
        thread = data.thread_of(identity.tenant_id, identity.subject_id)
        return thread['id'] if thread else None

    def own_task(identity, task_id):
        task = data.task(identity.tenant_id, task_id)
        if task['thread_id'] != mine(identity):
            raise NotFound('task not found')
        return task

    def own_todo(identity, todo_id):
        todo = data.todo(identity.tenant_id, todo_id)
        if todo['thread_id'] != mine(identity):
            raise NotFound('todo not found')
        return todo

    def own_mail(identity, mail_id):
        item = mail.message(identity.tenant_id, mail_id)
        if item['thread_id'] != mine(identity):
            raise NotFound('mail not found')
        return item

    def own_draft(identity, draft_id):
        draft = mail.draft(identity.tenant_id, draft_id)
        if draft['thread_id'] != mine(identity):
            raise NotFound('draft not found')
        return draft

    def own_approval(identity, approval_id):
        approval = data.approval(identity.tenant_id, approval_id)
        if approval['thread_id'] != mine(identity):
            raise NotFound('approval not found')
        return approval

    @app.get('/v1/assistant/thread')
    def my_thread(identity: Reader, limit: int = Query(50, ge=1, le=200)):
        thread = data.thread_for(identity.tenant_id, identity.subject_id)
        head = data.events(identity.tenant_id, thread['id'], after_seq=0, limit=1)['head_seq']
        return dict(thread=thread, messages=data.messages(identity.tenant_id, thread['id'], limit=limit), head_seq=head,
                    runtime=data.runtime(identity.tenant_id))

    @app.get('/v1/assistant/threads/{thread_id}/messages')
    def list_messages(thread_id: str, identity: Reader, before: float | None = None, limit: int = Query(50, ge=1, le=200)):
        own_thread(identity, thread_id)
        return dict(items=data.messages(identity.tenant_id, thread_id, limit=limit, before=before))

    @app.post('/v1/assistant/threads/{thread_id}/messages', status_code=201)
    def send_message(thread_id: str, body: MessageIn, identity: Writer, idempotency_key: Key):
        own_thread(identity, thread_id)
        def message_options():
            meta = {}
            workspace = getattr(app.state, 'workspace', None)
            if workspace is not None:
                preferences = workspace.preferences(identity)
                alias = body.model_alias or preferences.get('preferred_model')
                if alias:
                    aliases = {item['alias'] for item in workspace.models()['items']}
                    if alias not in aliases:
                        raise InvalidInput('selected model is not configured')
                    meta['model_alias'] = alias
                project_id = body.project_id or preferences.get('selected_project_id')
                if project_id:
                    project = workspace.project(identity, project_id)
                    meta.update(project_id=project_id, project_name=project['name'])
                meta['confirmation_mode'] = body.confirmation_mode or preferences.get('confirmation_mode', 'ask')
            elif body.model_alias or body.project_id or body.confirmation_mode:
                raise InvalidInput('workspace is not configured')
            return meta

        def apply(db):
            return data.add_message(identity.tenant_id, thread_id, role='user', kind='text', body=body.text, status='queued',
                                    author='subject:' + identity.subject_id, meta=message_options(), db=db)
        return store._command(identity.tenant_id, 'ms-msg:' + idempotency_key, 'assistant.message',
                              dict(thread_id=thread_id, **body.model_dump(exclude_none=True)), apply)

    @app.get('/v1/assistant/threads/{thread_id}/events')
    def thread_events(thread_id: str, identity: Reader, after_seq: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        own_thread(identity, thread_id)
        return data.events(identity.tenant_id, thread_id, after_seq=after_seq, limit=limit)

    @app.get('/v1/assistant/threads/{thread_id}/stream')
    async def thread_stream(thread_id: str, request: Request, identity: Reader,
                            after_seq: int = Query(0, ge=0), wait: int = Query(STREAM_SECONDS, ge=0, le=STREAM_SECONDS)):
        """Same-origin SSE over the append-only event log; resume with after_seq / Last-Event-ID."""
        own_thread(identity, thread_id)
        last = request.headers.get('last-event-id')
        cursor = int(last) if last and last.isdigit() else after_seq
        tenant = identity.tenant_id

        async def stream():
            nonlocal cursor
            loop = asyncio.get_running_loop()
            deadline, ping = loop.time() + wait, loop.time()
            yield 'retry: 2000\n\n'
            while True:
                page = await asyncio.to_thread(data.events, tenant, thread_id, after_seq=cursor, limit=200)
                for event in page['events']:
                    cursor = event['seq']
                    yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                if loop.time() >= deadline or await request.is_disconnected():
                    break
                if loop.time() - ping > 15:
                    ping = loop.time()
                    yield ': keep-alive\n\n'
                await asyncio.sleep(0.3)
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    # ----- tasks ----------------------------------------------------------------
    @app.get('/v1/assistant/tasks')
    def list_tasks(identity: Reader, status: str | None = Query(None, max_length=200)):
        statuses = tuple(s for s in status.split(',') if s) if status else None
        thread_id = mine(identity)
        return dict(items=data.tasks(identity.tenant_id, statuses=statuses, thread_id=thread_id) if thread_id else [])

    @app.get('/v1/assistant/tasks/{task_id}')
    def get_task(task_id: str, identity: Reader):
        return own_task(identity, task_id)

    @app.post('/v1/assistant/tasks/{task_id}/confirm')
    def confirm_task(task_id: str, identity: Approver):
        """Only the person marks work complete; the assistant can only propose it."""
        own_task(identity, task_id)
        task = data.update_task(identity.tenant_id, task_id, actor='subject:' + identity.subject_id, status='completed',
                                expect=('awaiting_confirmation',))
        todo_id = task['state'].get('todo_id')
        if todo_id:
            try:
                data.update_todo(identity.tenant_id, todo_id, status='done', actor='subject:' + identity.subject_id)
            except NotFound:
                pass
        return task

    @app.post('/v1/assistant/tasks/{task_id}/cancel')
    def cancel_task(task_id: str, identity: Writer):
        own_task(identity, task_id)
        return data.update_task(identity.tenant_id, task_id, actor='subject:' + identity.subject_id, status='cancelled',
                                expect=('queued', 'blocked', 'awaiting_confirmation', 'waiting_approval'))

    @app.post('/v1/assistant/tasks/{task_id}/retry')
    def retry_task(task_id: str, identity: Writer):
        task = own_task(identity, task_id)
        if task['kind'] != 'research' or task['status'] != 'blocked':
            raise Conflict('only a paused research task can be retried')
        if task['state'].get('blocked') not in RETRY_SAFE_BLOCKS:
            raise Conflict('an unknown model call must be reconciled first; it is never re-sent automatically')
        return data.update_task(identity.tenant_id, task_id, actor='subject:' + identity.subject_id, status='queued',
                                state=dict(blocked=None))

    # ----- todos ----------------------------------------------------------------
    @app.get('/v1/assistant/todos')
    def list_todos(identity: Reader, include_closed: bool = False):
        statuses = ('open', 'in_progress', 'done', 'dismissed') if include_closed else ('open', 'in_progress')
        thread_id = mine(identity)
        return dict(items=data.todos(identity.tenant_id, statuses=statuses, thread_id=thread_id) if thread_id else [])

    @app.post('/v1/assistant/todos', status_code=201)
    def add_todo(body: TodoIn, identity: Writer):
        thread = data.thread_for(identity.tenant_id, identity.subject_id)
        action = dict(type='research', question=body.text) if body.research else None
        return data.add_todo(identity.tenant_id, thread['id'], body=body.text, source='user', due=body.due, action=action,
                             actor='subject:' + identity.subject_id)

    @app.post('/v1/assistant/todos/{todo_id}')
    def update_todo(todo_id: str, body: TodoUpdate, identity: Writer):
        own_todo(identity, todo_id)
        return data.update_todo(identity.tenant_id, todo_id, status=body.status, actor='subject:' + identity.subject_id)

    # ----- approvals --------------------------------------------------------------
    @app.get('/v1/assistant/approvals')
    def list_approvals(identity: Reader, status: Literal['pending', 'approved', 'rejected', 'all'] = 'pending'):
        thread_id = mine(identity)
        items = data.approvals(identity.tenant_id, status=None if status == 'all' else status)
        return dict(items=[a for a in items if thread_id and a['thread_id'] == thread_id])

    @app.post('/v1/assistant/approvals/{approval_id}')
    def decide(approval_id: str, body: DecisionIn, identity: Approver, idempotency_key: Key):
        own_approval(identity, approval_id)
        approval = data.decide_approval(identity.tenant_id, approval_id, decision=body.decision, subject=identity.subject_id)
        if approval['action'] == 'send_mail':
            # The person's approval of this exact draft version is the only way into the outbox.
            if body.decision == 'rejected':
                mail.release_draft(identity.tenant_id, approval['payload']['draft_id'], approval_id)
                return dict(approval=approval)
            return dict(approval=approval, outbox=mail.queue_send(identity.tenant_id, approval))
        if body.decision == 'rejected':
            todo_id = approval['payload'].get('todo_id')
            if todo_id:
                data.update_todo(identity.tenant_id, todo_id, status='dismissed', actor='subject:' + identity.subject_id)
            return dict(approval=approval)
        actor = 'subject:' + identity.subject_id
        if approval['action'] == 'start_research':
            payload = approval['payload']
            task = data.create_task(identity.tenant_id, approval['thread_id'], kind='research', title=payload['title'],
                                    brief=payload['question'], origin='user', actor=actor,
                                    state=dict(step='plan', todo_id=payload.get('todo_id'), approval_id=approval_id))
            if payload.get('todo_id'):
                data.update_todo(identity.tenant_id, payload['todo_id'], status='in_progress', actor=actor, task_id=task['id'])
            return dict(approval=approval, task=task)
        # start_coding: this human decision is the request that creates the session and
        # approves its analysis/test-design plan; the implementation plan asks again later.
        payload = approval['payload']
        session = sessions.create(identity, 'ms-coding:' + approval_id, prompt=payload['prompt'], title=payload['title'])
        task = data.create_task(identity.tenant_id, approval['thread_id'], kind='coding', title=payload['title'],
                                brief=payload['prompt'], origin='user', actor=actor, status='running',
                                state=dict(session_id=session['session_id'], approval_id=approval_id))
        return dict(approval=approval, task=task, session_id=session['session_id'])

    # ----- mail (executor holds the mailbox password; this process never does) --------
    @app.get('/v1/assistant/mail/status')
    def mail_status(identity: Reader):
        thread_id = mine(identity)
        state = mail.state_for_thread(identity.tenant_id, thread_id) if thread_id else None
        if state is None:
            return dict(configured=False)
        return dict(configured=True, account=state['account'], status=state['status'], error=state['error'],
                    message=MAIL_FAILURE.get(state['error']) if state['error'] else None, last_sync_at=state['last_sync_at'],
                    send_enabled=bool(state['send_enabled']))

    @app.post('/v1/assistant/mail/sync', status_code=202)
    def mail_sync(identity: Writer):
        """Ask the executor to check the inbox now (e.g. after updating the private password file)."""
        thread_id = mine(identity)
        state = mail.state_for_thread(identity.tenant_id, thread_id) if thread_id else None
        if state is None:
            raise NotFound('no mailbox configured for you')
        mail.save_state(identity.tenant_id, state['account'], status='sync_requested')
        return dict(requested=True)

    @app.get('/v1/assistant/mail/messages')
    def mail_messages(identity: Reader, limit: int = Query(50, ge=1, le=200)):
        thread_id = mine(identity)
        items = mail.messages(identity.tenant_id, thread_id, limit=limit) if thread_id else []
        for item in items:
            item.pop('safe_text', None)
        return dict(items=items)

    @app.get('/v1/assistant/mail/messages/{mail_id}')
    def mail_message(mail_id: str, identity: Reader):
        item = own_mail(identity, mail_id)
        try:
            draft = mail.draft(identity.tenant_id, mail_id=mail_id)
        except NotFound:
            draft = None
        outbox = mail.outbox(identity.tenant_id, draft_id=draft['id']) if draft else []
        return dict(mail=item, draft=draft, outbox=outbox)

    @app.post('/v1/assistant/mail/messages/{mail_id}/draft', status_code=202)
    def request_draft(mail_id: str, identity: Writer):
        item = own_mail(identity, mail_id)
        if item['status'] == 'filtered':
            raise Conflict('this mail broke the content rules and is not given to the model')
        try:
            draft = mail.draft(identity.tenant_id, mail_id=mail_id)
        except NotFound:
            draft = None
        if draft and draft['status'] in ('awaiting_approval', 'queued', 'sent', 'unknown'):
            raise Conflict('the draft is already awaiting approval or sent')
        return mail.update_message(identity.tenant_id, mail_id, draft_state='requested')

    @app.get('/v1/assistant/mail/drafts/{draft_id}')
    def get_draft(draft_id: str, identity: Reader):
        return own_draft(identity, draft_id)

    @app.put('/v1/assistant/mail/drafts/{draft_id}')
    def edit_draft(draft_id: str, body: DraftIn, identity: Writer):
        draft = own_draft(identity, draft_id)
        return mail.save_draft(identity.tenant_id, draft['mail_id'], to=body.to, cc=body.cc, subject=body.subject, body=body.body,
                               origin='person', expected_version=body.expected_version)

    @app.post('/v1/assistant/mail/drafts/{draft_id}/request-send', status_code=201)
    def request_send(draft_id: str, body: SendIn, identity: Writer):
        draft = own_draft(identity, draft_id)
        state = mail.state_for_thread(identity.tenant_id, draft['thread_id'])
        if not state or not state['send_enabled']:
            raise Conflict('MAIL_SEND_DISABLED: ' + MAIL_FAILURE['MAIL_SEND_DISABLED'])
        if draft['version'] != body.expected_version or draft['status'] != 'draft':
            raise Conflict('DRAFT_CHANGED')
        warning = ('注意：草稿里还有 ' + '、'.join(draft['placeholders']) + ' 等占位符，请先改成真实内容。') if draft['placeholders'] else ''
        approval = data.request_approval(
            identity.tenant_id, draft['thread_id'], action='send_mail', title='发送邮件：' + draft['subject'][:80],
            detail=f"收件人 {len(draft['to'])} 位、抄送 {len(draft['cc'])} 位，第 {draft['version']} 版草稿。批准后立即提交给邮件服务器；"
                   '草稿再改动，这次批准自动作废。' + warning,
            payload=dict(draft_id=draft_id, version=draft['version'], mail_id=draft['mail_id']),
            actor='subject:' + identity.subject_id, ref=f"send:{draft_id}:{draft['version']}:{draft['updated_at']}")
        mail.attach_approval(identity.tenant_id, draft_id, approval['id'], draft['version'])
        data.add_message(identity.tenant_id, draft['thread_id'], role='assistant', kind='approval',
                         body='需要你批准后才发送：' + draft['subject'][:80], meta=dict(approval_id=approval['id'], draft_id=draft_id),
                         author='subject:' + identity.subject_id)
        return approval

    # ----- mandates and settings ----------------------------------------------------
    @app.get('/v1/assistant/mandates')
    def list_mandates(identity: Reader):
        items = [m for m in data.mandates(identity.tenant_id) if m['subject'] == identity.subject_id]
        return dict(items=items, scopes=dict(research='在授权预算内自动开始待办里的只读调研',
                                                            resume='自动重新排队因网络/预算原因暂停、且无未知调用的调研'))

    @app.post('/v1/assistant/mandates', status_code=201)
    def grant(body: MandateIn, identity: Approver):
        """A standing authorisation signed by the person (never by the assistant)."""
        return data.grant_mandate(identity.tenant_id, identity.subject_id, scopes=list(body.scopes),
                                  daily_budget_microusd=body.daily_budget_microusd, days=body.days)

    @app.post('/v1/assistant/mandates/{mandate_id}/revoke')
    def revoke(mandate_id: str, identity: Writer):
        if not any(m['id'] == mandate_id and m['subject'] == identity.subject_id for m in data.mandates(identity.tenant_id)):
            raise NotFound('mandate not found')
        return data.revoke_mandate(identity.tenant_id, mandate_id, identity.subject_id)

    @app.get('/v1/assistant/settings')
    def read_settings(tenant: Tenant):
        return dict(data.settings(tenant), runtime=data.runtime(tenant))

    @app.put('/v1/assistant/settings')
    def write_settings(body: SettingsIn, identity: Writer):
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        if not changes:
            raise InvalidInput('no settings supplied')
        # Lowering spend is always allowed; raising it is a budget decision for a named person.
        if (changes.get('daily_budget_microusd', 0) > data.settings(identity.tenant_id)['daily_budget_microusd']
                and not is_human(identity)):
            raise HTTPException(403, 'raising the daily budget requires a named human user')
        return data.save_settings(identity.tenant_id, actor='subject:' + identity.subject_id, **changes)

    return data
