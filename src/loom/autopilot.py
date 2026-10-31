"""Conversation sessions with delegated automatic progression.

A named operator opts a session into ``auto`` mode once. The private executor
then performs the same audited actions the console would (plan approval, input
grants, execution requests, suite freeze, verification requests) under that
operator's subject, using deterministic ``ap:<session>:...`` idempotency keys
so every delegated receipt is identifiable and replay-safe.

Autopilot never weakens the kernel: budgets, leases, unknown-effect holds,
frozen contracts and Gate-V are unchanged. It stops and asks a human whenever
progression reports anything other than a routine next step. Held-out
acceptance cases are written by the tester role model, not by an independent
maintainer, and are labelled that way; they never count as dual verification.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from .artifacts import ArtifactService
from .builtin_resources import software_workflow
from .development_suites import DevelopmentSuiteService
from .dispatch import DispatchService
from .execution import freeze_tests
from .progression import ProgressionService
from .sandbox import FrozenTest
from .store import Conflict, InvalidInput, LoomError, NotFound, canonical, digest
from .workflow import WorkflowService

SCHEMA = '''
CREATE TABLE IF NOT EXISTS autopilot_sessions (
 tenant TEXT NOT NULL, id TEXT NOT NULL, record TEXT NOT NULL, event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS autopilot_links (
 tenant TEXT NOT NULL, session_id TEXT NOT NULL, phase TEXT NOT NULL, run_id TEXT NOT NULL,
 record TEXT NOT NULL, event_hash TEXT NOT NULL, PRIMARY KEY(tenant,session_id,phase),
 FOREIGN KEY(tenant,session_id) REFERENCES autopilot_sessions(tenant,id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TRIGGER IF NOT EXISTS autopilot_sessions_no_update BEFORE UPDATE ON autopilot_sessions
 BEGIN SELECT RAISE(ABORT,'autopilot sessions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS autopilot_sessions_no_delete BEFORE DELETE ON autopilot_sessions
 BEGIN SELECT RAISE(ABORT,'autopilot sessions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS autopilot_links_no_update BEFORE UPDATE ON autopilot_links
 BEGIN SELECT RAISE(ABORT,'autopilot links are append-only'); END;
CREATE TRIGGER IF NOT EXISTS autopilot_links_no_delete BEFORE DELETE ON autopilot_links
 BEGIN SELECT RAISE(ABORT,'autopilot links are append-only'); END;
'''

TEMPLATE = 'loom.autopilot.python-json/v1'
RUNTIME_CONTRACT = ('运行约定：用 Python 3 标准库实现一个程序 main.py；从标准输入读取一个 JSON 值，'
                    '向标准输出写恰好一个 JSON 结果，不输出解释或调试文本；无网络、无文件读写。')
ROLE_LIMITS = dict(cost_limit_microusd=330000, token_limit=1060000, max_attempts=1)
SPEC_BUDGET = dict(cost_limit=700000, token_limit=2200000)
BUILD_LIMITS = dict(cost_limit_microusd=700000, token_limit=2200000, max_attempts=2, context_limit_bytes=65536)
BUILD_BUDGET = dict(cost_limit=750000, token_limit=2300000)
ACCEPTANCE = ['schema.valid', 'tests.pass', 'report.traceable']
MIN_CASES = 2

# Progression stages that need a person. Everything else is routine or waiting.
ATTENTION = {
    'budget_insufficient': '预算不足以安全发起下一次模型调用，请调整预算后新建会话。',
    'effect_reconciliation_required': '有一次模型或工具调用结果未知，需要先在“人工待办”核对，避免重复计费或重复执行。',
    'attempt_requires_review': '上一次执行没有通过或被中断，需要人工查看证据后决定是否继续。',
    'contract_unavailable': '任务合同无法读取或不受支持，自动推进已停止。',
    'source_or_contract_changed': '输入来源或合同已变化，需要重新核对。',
    'review_routing_required': '审查意见需要人工决定处置方式。',
    'no_fix_findings': '审查未发现问题，需要人工确认是否跳过修复。',
    'review_source_unavailable': '审查来源不可用，需要人工核对。',
    'independent_model_pending': '计划包含需要独立模型的验收节点，自动会话不会伪造该结果。',
}
MESSAGES = {
    'EXECUTOR_NOT_CONFIGURED': '本机执行器未配置，无法自动执行。',
    'HOST_TESTS_NOT_REGISTERED': '验收用例没有登记到执行器，无法开始实现。',
    'HOST_TESTS_CHANGED': '验收用例与冻结摘要不一致，已停止。',
    'ADMISSION_NOT_READY': '执行准入条件未满足（预算、依赖或身份配置），已停止。',
    'REQUEST_LIMIT_REACHED': '该任务的执行请求次数已达上限。',
    'EXECUTION_BLOCKED': '执行在调用模型前被拦截（配置、镜像或身份），不会自动重试。',
    'EXECUTION_UNFINISHED': '执行未正常结束，需要人工核对执行账本，不会自动重试。',
    'TOO_FEW_CASES': '测试设计少于 2 个用例，无法同时提供开发检查和留出验收。',
    'PUBLIC_CHECKS_INVALID': '测试设计不符合可执行用例格式。',
    'AUTO_ACCEPTANCE_NOT_CONFIGURED': '执行器没有配置自动验收用例目录（auto_acceptance_dir）。',
    'ACCEPTANCE_FAILED': '程序没有通过留出验收用例（已用完允许的修复次数），请查看证据后决定下一步。',
    'AUTOPILOT_ERROR': '自动推进遇到无法分类的状态，已停止以免误操作。',
}
FAILURE_MESSAGES = {
    'REPEATED_SOURCE': '修复后模型生成了与上一版相同的程序，自动修复已停止。',
    'BUDGET_EXHAUSTED': '本任务预算已用完，自动修复已停止。',
    'MODEL_SOURCE_INVALID': '模型没有返回可用的程序源码。',
    'MODEL_OUTPUT_TRUNCATED': '模型输出被截断，没有得到完整程序。',
    'SANDBOX_UNAVAILABLE_OR_REJECTED': '隔离执行环境不可用或拒绝运行，请检查 Docker。',
    'REPAIR_ATTEMPTS_EXHAUSTED': '已用完允许的修复次数，公开检查仍未通过。',
    'UNRESOLVED_CALL_REQUIRES_HUMAN': '有调用结果未知，需要先人工核对。',
}
MESSAGES.update(FAILURE_MESSAGES)
FINAL_STATES = {'terminated', 'delivered', 'verified'}


def _text(value, label, limit):
    if type(value) is not str or not value.strip() or len(value) > limit or '\x00' in value:
        raise InvalidInput(f'invalid {label}')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise InvalidInput(f'invalid {label} encoding') from None
    return value


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit] + '\n…（已截断）'


ACTIVITY = {
    'execution.requested': '已排队，等待本机执行器领取',
    'execution.request_claimed': '执行器已领取，准备调用模型',
    'budget.reserved': '已按上限预留预算，正在调用模型',
    'tool.intent': '正在调用模型',
    'execution.context_bound': '正在调用模型',
    'tool.done': '模型已返回，正在校验结果',
    'role.candidate_generated': '已生成结果，正在提交',
    'execution.candidate': '正在隔离环境运行公开检查',
    'development.evaluated': '公开检查已完成',
    'repair.rebind': '公开检查未通过，正在让模型修复',
    'execution.evaluated': '留出验收已完成',
    'artifact.input_approved': '已把上一步结果交给下一步',
    'development.suite_frozen': '已冻结开发检查用例',
    'autopilot.acceptance_frozen': '已留出验收用例',
    'workflow.plan_approved': '计划已自动批准',
}


def split_cases(names):
    """Deterministic held-out selection: about one third, never empty, never all."""
    if len(names) < MIN_CASES:
        raise InvalidInput('TOO_FEW_CASES')
    held = [name for index, name in enumerate(names) if index % 3 == 2] or [names[-1]]
    return [name for name in names if name not in held], held


def _write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.new')
    with open(temporary, 'x', encoding='utf-8') as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


class SessionService:
    def __init__(self, store, *, executor_config=None, identities=(), verification=None, initialize=True):
        self.store, self.config, self.identities = store, executor_config, tuple(identities)
        self.verification = verification
        self.workflow = WorkflowService(store)
        self.artifacts = ArtifactService(store, initialize=initialize)
        self.suites = DevelopmentSuiteService(store, artifacts=self.artifacts)
        self.progression = ProgressionService(store)
        self.dispatch = DispatchService(store, config=executor_config, identities=identities)
        if initialize:
            with store.connect() as db:
                db.executescript(SCHEMA)

    # ----- persistence -------------------------------------------------
    def _session_row(self, db, tenant, session_id):
        row = db.execute('SELECT * FROM autopilot_sessions WHERE tenant=? AND id=?', (tenant, session_id)).fetchone()
        if row is None:
            raise NotFound('session not found')
        record = json.loads(row['record'])
        links = {r['phase']: r['run_id'] for r in db.execute(
            'SELECT phase,run_id FROM autopilot_links WHERE tenant=? AND session_id=? ORDER BY rowid', (tenant, session_id))}
        return record, links

    def _control_events(self, db, tenant, session_id, runs):
        marks = ','.join('?' for _ in runs)
        return [json.loads(r['envelope']) for r in db.execute(
            f"SELECT envelope FROM events WHERE tenant=? AND run_id IN ({marks}) "
            "AND json_extract(envelope,'$.type') IN ('autopilot.stopped','autopilot.resumed','autopilot.delivered') "
            "AND json_extract(envelope,'$.payload.session_id')=? ORDER BY json_extract(envelope,'$.ts'),rowid",
            (tenant, *runs, session_id))]

    @staticmethod
    def _operator(identity):
        return 'subject:' + identity.subject_id

    def _registered_operator(self, tenant, subject):
        found = [i for i in self.identities if i.tenant_id == tenant and i.subject_id == subject and 'operator' in i.roles]
        if len(found) != 1:
            raise InvalidInput('autopilot requires the registered named operator who created the session')
        return found[0]

    # ----- creation ----------------------------------------------------
    def _spec_definition(self, prompt):
        definition = software_workflow()
        task = f'用户需求：\n{prompt}\n\n{RUNTIME_CONTRACT}'
        definition['nodes'] = [
            dict(id='requirements', title='分析需求', role='analyst', priority=20,
                 instruction=task + '\n请澄清输入输出格式、边界与错误处理，列出未决事实。',
                 role_output=dict(kind='requirements', schema_version='loom.requirements/v1'), limits=dict(ROLE_LIMITS)),
            # Hard depends require independently verified (done) sources; the input
            # grant alone orders this step after the analyst's submitted candidate.
            dict(id='checks', title='设计测试用例', role='tester', priority=10,
                 instruction=task + '\n请依据已批准需求设计 4 到 8 个互不重复、可直接比较的 JSON 输入与期望输出用例，'
                             '覆盖正常、边界和错误输入。约三分之一用例会被留出作验收，不提供给实现者。',
                 input_artifacts=[dict(source_node='requirements', kind='requirements',
                                       schema_version='loom.requirements/v1', required=True)],
                 role_output=dict(kind='public-checks', schema_version='loom.public-checks/v1'), limits=dict(ROLE_LIMITS)),
        ]
        return definition

    def _compose_follow_up(self, tenant, parent_id, message):
        """A follow-up is a new audited session whose prompt carries the thread.

        The previous program is quoted as untrusted reference data; nothing from
        the parent's held-out acceptance set is copied.
        """
        view = self.get(tenant, parent_id)
        if view['state'] in {'running', 'paused'}:
            raise Conflict('previous turn is still in progress')
        chain, current = [], parent_id
        with self.store.connect() as db:
            while current is not None and len(chain) < 20:
                record, _ = self._session_row(db, tenant, current)
                chain.append(record)
                current = record.get('parent_session_id')
        chain.reverse()
        root = chain[0]
        source = next((s.get('source') for s in view['steps'] if s['key'] == 'implement'), None)
        parts = ['原始需求：\n' + _clip(root.get('request') or root['prompt'], 4000)]
        earlier = [r.get('request') for r in chain[1:] if r.get('request')]
        if earlier:
            parts.append('之前的补充要求（按顺序）：\n' + _clip('\n'.join(f'{i + 1}. {t}' for i, t in enumerate(earlier)), 3000))
        if source:
            parts.append('上一版程序（仅供参考，未经独立验证，可能有缺陷）：\n' + _clip(source, 5000))
        parts.append('本次补充或修改要求（优先满足）：\n' + message)
        return '\n\n'.join(parts), root['title'], root.get('thread_id') or root['session_id']

    def create(self, identity, key, *, prompt, title=None, parent_session_id=None):
        _text(prompt, 'prompt', 4000)
        _text(key, 'idempotency key', 120)
        tenant = identity.tenant_id
        message, thread_id = prompt, None
        if parent_session_id is not None:
            _text(parent_session_id, 'parent session', 100)
            prompt, parent_title, thread_id = self._compose_follow_up(tenant, parent_session_id, message)
            title = title if title is not None else parent_title
        title = title if title is not None else message.strip().splitlines()[0][:60]
        _text(title, 'title', 200)
        actor = self._operator(identity)
        base = 'ap-create:' + digest({'key': key, 'subject': identity.subject_id, 'parent': parent_session_id})[:40]
        goal = self.workflow.create_goal(tenant, base + ':goal', title=title, description=prompt,
                                         acceptance=list(ACCEPTANCE), actor=actor, **SPEC_BUDGET)
        run_id = goal['run_id']
        plan = self.workflow.get_plan(tenant, run_id)
        if plan['revision'] == 0:
            plan = self.workflow.save_plan(tenant, run_id, base + ':plan', self._spec_definition(prompt), 0, actor=actor)
        if plan['status'] == 'draft':
            self.workflow.approve_plan(tenant, run_id, base + ':approve', plan['revision'], plan['digest'], actor=actor)
        args = dict(run_id=run_id, prompt=prompt, title=title, actor=actor, parent_session_id=parent_session_id, message=message)

        def apply(db):
            session_id = str(uuid4())
            thread = {} if thread_id is None else dict(thread_id=thread_id, parent_session_id=parent_session_id)
            record = dict(schema_version='loom.autopilot-session/v1', session_id=session_id, tenant_id=tenant,
                          title=title, prompt=prompt, request=message, **thread, mode='auto', template=TEMPLATE,
                          operator_id=identity.subject_id, spec_run_id=run_id,
                          policy=dict(max_repairs=1, min_cases=MIN_CASES, automatic_retry=False,
                                      acceptance_origin='tester-model-heldout'))
            event = self.store._event(db, tenant, run_id, 'autopilot.session_created',
                                      dict(session_id=session_id, record=record, record_digest=digest(record),
                                           delegated_by=actor, counts_as_success=False), actor, base + ':session')
            db.execute('INSERT INTO autopilot_sessions VALUES(?,?,?,?)', (tenant, session_id, canonical(record), event['hash']))
            link = dict(session_id=session_id, phase='spec', run_id=run_id)
            db.execute('INSERT INTO autopilot_links VALUES(?,?,?,?,?,?)',
                       (tenant, session_id, 'spec', run_id, canonical(link), event['hash']))
            return dict(session_id=session_id, run_id=run_id)
        created = self.store._command(tenant, base + ':session', 'autopilot.create_session', args, apply)
        return self.get(tenant, created['session_id'])

    def resume(self, identity, session_id, key, *, reason):
        _text(reason, 'reason', 2000)
        tenant = identity.tenant_id
        args = dict(session_id=session_id, reason=reason, actor=self._operator(identity))

        def apply(db):
            record, links = self._session_row(db, tenant, session_id)
            controls = self._control_events(db, tenant, session_id, list(links.values()))
            if not controls or controls[-1]['type'] != 'autopilot.stopped':
                raise Conflict('session is not stopped')
            run_id = controls[-1]['run_id']
            self.store._event(db, tenant, run_id, 'autopilot.resumed',
                              dict(session_id=session_id, reason=reason, resumes=controls[-1]['payload']['code']),
                              self._operator(identity), key)
            return dict(session_id=session_id, resumed=True)
        self.store._command(tenant, key, 'autopilot.resume', args, apply)
        return self.get(tenant, session_id)

    # ----- automatic progression --------------------------------------
    def _stop(self, tenant, session_id, run_id, code, actor, detail=None):
        key = f'ap:{session_id}:stop:{run_id}:{code}:{detail or ""}'[:200]
        args = dict(session_id=session_id, run_id=run_id, code=code, detail=detail)

        def apply(db):
            record, links = self._session_row(db, tenant, session_id)
            controls = self._control_events(db, tenant, session_id, list(links.values()))
            if controls and controls[-1]['type'] == 'autopilot.stopped':
                return dict(already_stopped=True)
            self.store._event(db, tenant, run_id, 'autopilot.stopped',
                              dict(session_id=session_id, code=code, detail=detail,
                                   message=ATTENTION.get(code) or MESSAGES.get(code) or MESSAGES['AUTOPILOT_ERROR'],
                                   automatic_retry=False), actor, key)
            return dict(stopped=True)
        try:
            return self.store._command(tenant, key, 'autopilot.stop', args, apply)
        except Conflict:
            # Same key after a resume: record a fresh stop for the new episode.
            return self.store._command(tenant, key + ':' + uuid4().hex[:8], 'autopilot.stop', args, apply)

    def active(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT tenant,id FROM autopilot_sessions ORDER BY rowid').fetchall()
        return [(r['tenant'], r['id']) for r in rows]

    def tick(self):
        """Advance every non-final session by at most one step each. DB only."""
        acted = []
        for tenant, session_id in self.active():
            try:
                result = self.advance(tenant, session_id)
            except LoomError:
                # Usually a benign race (a preview digest moved); re-read next tick.
                result = None
            except Exception:
                result = self._stop_unexpected(tenant, session_id)
            if result:
                acted.append(dict(session_id=session_id, action=result))
        return acted

    def _stop_unexpected(self, tenant, session_id):
        try:
            with self.store.connect() as db:
                record, links = self._session_row(db, tenant, session_id)
            run_id = links.get('build') or links['spec']
            self._stop(tenant, session_id, run_id, 'AUTOPILOT_ERROR', 'subject:' + record['operator_id'])
            return 'stopped'
        except Exception:
            return None

    def advance(self, tenant, session_id):
        with self.store.connect() as db:
            record, links = self._session_row(db, tenant, session_id)
            controls = self._control_events(db, tenant, session_id, list(links.values()))
        if controls and controls[-1]['type'] in {'autopilot.stopped', 'autopilot.delivered'}:
            return None
        if self.config is None or getattr(self.config, 'executor_id', None) is None:
            return None
        operator = self._registered_operator(tenant, record['operator_id'])
        actor = self._operator(operator)
        phase = 'build' if 'build' in links else 'spec'
        run_id = links[phase]
        progress = self.progression.read(tenant, run_id)
        if progress['run_status'] in {'paused', 'cancelled', 'terminated', 'succeeded', 'failed'}:
            return None
        if progress['run_status'] == 'awaiting_human':
            code = ('effect_reconciliation_required' if progress['unresolved_effects'] or progress['unsettled_reservations']
                    else self._failure_code(tenant, run_id))
            self._stop(tenant, session_id, run_id, code, actor)
            return 'stopped'
        for node in progress['nodes']:
            action = self._node(tenant, session_id, run_id, node, operator, actor)
            if action:
                return action
        if phase == 'spec' and progress['nodes'] and all(n['status'] in {'submitted', 'done'} for n in progress['nodes']):
            return self._build(tenant, session_id, record, run_id, progress, operator, actor)
        if phase == 'build' and progress['nodes'] and all(n['status'] in {'submitted', 'done'} for n in progress['nodes']):
            return self._deliver(tenant, session_id, run_id, progress, operator, actor)
        return None

    def _failure_code(self, tenant, run_id):
        with self.store.connect() as db:
            row = db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? "
                             "AND json_extract(envelope,'$.type')='failure.classified' ORDER BY seq DESC LIMIT 1",
                             (tenant, run_id)).fetchone()
        code = json.loads(row['envelope'])['payload'].get('code') if row else None
        if code == 'ACCEPTANCE_TESTS_FAILED':
            return 'ACCEPTANCE_FAILED'
        if code in FAILURE_MESSAGES:
            return code
        return 'attempt_requires_review'

    def _node(self, tenant, session_id, run_id, node, operator, actor):
        stage, task_id = node['stage'], node['task_id']
        if stage in ATTENTION:
            history = self.dispatch.history(tenant, run_id, task_id)['items'] if self.config else []
            code = stage
            if stage == 'attempt_requires_review' and history:
                code = 'ACCEPTANCE_FAILED' if node['role'] in {'developer', 'fixer'} and history[-1]['state'] == 'awaiting_human' else 'EXECUTION_UNFINISHED'
            self._stop(tenant, session_id, run_id, code, actor, task_id)
            return 'stopped'
        if stage == 'input_approval_required':
            for item in node['suggested_inputs']:
                with self.store.connect() as db:
                    db.execute('BEGIN')
                    row, _ = self.artifacts._candidate(db, tenant, run_id, item['artifact_id'])
                self.artifacts.approve_input(
                    tenant, run_id, task_id, item['artifact_id'],
                    f'ap:{session_id}:input:{task_id}:{node["epoch"]}:{row["content_hash"][:16]}', actor=actor,
                    expected_plan_digest=row['plan_digest'], expected_source_epoch=row['source_epoch'],
                    expected_target_epoch=node['epoch'], expected_revision=row['revision'],
                    expected_content_hash=row['content_hash'])
            return 'input_approved' if node['suggested_inputs'] else None
        if stage in {'local_execution_ready', 'host_tests_required'}:
            preview = self.dispatch.preview(tenant, run_id, task_id)
            latest = preview['latest_request']
            if preview['reason'] == 'REQUEST_ALREADY_ACTIVE':
                return None
            if latest and latest['state'] in {'blocked', 'awaiting_human', 'indeterminate'}:
                self._stop(tenant, session_id, run_id,
                           'EXECUTION_BLOCKED' if latest['state'] == 'blocked' else 'EXECUTION_UNFINISHED', actor, task_id)
                return 'stopped'
            if not preview['ready']:
                self._stop(tenant, session_id, run_id, preview['reason'] or 'ADMISSION_NOT_READY', actor, task_id)
                return 'stopped'
            self.dispatch.enqueue(tenant, run_id, task_id, f'ap:{session_id}:exec:{task_id}:{preview["history_count"]}',
                                  actor=actor, expected_dispatch_digest=preview['dispatch_digest'],
                                  reason='自动会话：委托人已开启自动推进，按冻结计划与预算发起本步执行')
            return 'execution_requested'
        return None

    def _build(self, tenant, session_id, record, spec_run, progress, operator, actor):
        checks = next((n for n in progress['nodes'] if n['role'] == 'tester'), None)
        requirements = next((n for n in progress['nodes'] if n['role'] == 'analyst'), None)
        if checks is None or not checks['artifact_id']:
            self._stop(tenant, session_id, spec_run, 'PUBLIC_CHECKS_INVALID', actor)
            return 'stopped'
        auto_dir = getattr(self.config, 'auto_acceptance_dir', None)
        if auto_dir is None:
            self._stop(tenant, session_id, spec_run, 'AUTO_ACCEPTANCE_NOT_CONFIGURED', actor)
            return 'stopped'
        with self.store.connect() as db:
            db.execute('BEGIN')
            row, body = self.artifacts._candidate(db, tenant, spec_run, checks['artifact_id'])
            summary = ''
            if requirements and requirements['artifact_id']:
                _, req = self.artifacts._candidate(db, tenant, spec_run, requirements['artifact_id'])
                summary = json.dumps(req, ensure_ascii=False)[:6000]
        names = [case['name'] for case in body['cases']]
        try:
            dev, held = split_cases(names)
            cases = {case['name']: case for case in body['cases']}
            tests, evaluation = freeze_tests([FrozenTest(n, cases[n]['input'], cases[n]['expected']) for n in held])
        except (InvalidInput, TypeError, ValueError) as error:
            self._stop(tenant, session_id, spec_run, 'TOO_FEW_CASES' if str(error) == 'TOO_FEW_CASES' else 'PUBLIC_CHECKS_INVALID', actor)
            return 'stopped'
        base = f'ap:{session_id}:build:{row["content_hash"][:16]}'
        preview = self.suites.preview(tenant, spec_run, checks['artifact_id'], selected_case_names=dev)
        frozen = self.suites.freeze(tenant, spec_run, checks['artifact_id'], base + ':suite', actor=actor,
                                    expected_review_digest=preview['review_digest'], selected_case_names=dev)
        suite = self.suites.get_suite(tenant, spec_run, frozen['suite_id'])
        reference = suite['execution_reference']
        if evaluation == reference['development_digest']:
            self._stop(tenant, session_id, spec_run, 'PUBLIC_CHECKS_INVALID', actor)
            return 'stopped'
        _write_private(Path(auto_dir) / f'{evaluation}.json',
                       {'tests': [dict(name=t.name, input=t.input, expected=t.expected) for t in tests]})
        acceptance_args = dict(session_id=session_id, suite_id=frozen['suite_id'], evaluation_digest=evaluation,
                               heldout_case_names=held, development_case_names=dev, artifact_id=checks['artifact_id'],
                               content_hash=row['content_hash'])

        def record_acceptance(db):
            event = self.store._event(db, tenant, spec_run, 'autopilot.acceptance_frozen',
                                      {**acceptance_args, 'acceptance_origin': 'tester-model-heldout',
                                       'independent_maintainer_tests': False, 'hidden_from_developer': True,
                                       'counts_as_success': False}, actor, base + ':acceptance')
            return dict(event_hash=event['hash'])
        self.store._command(tenant, base + ':acceptance', 'autopilot.freeze_acceptance', acceptance_args, record_acceptance)
        prompt = record['prompt']
        goal = self.workflow.create_goal(tenant, base + ':goal', title=record['title'] + ' · 实现', description=prompt,
                                         acceptance=list(ACCEPTANCE), actor=actor, **BUILD_BUDGET)
        build_run = goal['run_id']
        plan = self.workflow.get_plan(tenant, build_run)
        if plan['revision'] == 0:
            definition = software_workflow()
            instruction = f'用户需求：\n{_clip(prompt, 8000)}\n\n{RUNTIME_CONTRACT}'
            visible = [dict(name=n, input=cases[n]['input'], expected=cases[n]['expected']) for n in dev]
            instruction += ('\n\n公开开发用例（程序须通过；另有留出验收用例不提供）：\n'
                            + _clip(json.dumps(visible, ensure_ascii=False), 4500))
            if summary:
                instruction += '\n\n已提交的需求分析（未经独立验证，仅供参考）：\n' + _clip(summary, 2500)
            definition['nodes'] = [dict(id='implement', title='编写并测试程序', role='developer', instruction=instruction[:16000],
                                        evaluation_digest=evaluation, development_suite=reference,
                                        repair=dict(schema_version='loom.rebind/v1', development_digest=reference['development_digest'],
                                                    max_repairs=1, stop_on_duplicate_source=True),
                                        limits=dict(BUILD_LIMITS))]
            plan = self.workflow.save_plan(tenant, build_run, base + ':plan', definition, 0, actor=actor)
        if plan['status'] == 'draft':
            self.workflow.approve_plan(tenant, build_run, base + ':approve', plan['revision'], plan['digest'], actor=actor)
        link_args = dict(session_id=session_id, run_id=build_run)

        def link(db):
            if db.execute('SELECT 1 FROM autopilot_links WHERE tenant=? AND session_id=? AND phase=?',
                          (tenant, session_id, 'build')).fetchone():
                raise Conflict('build phase already linked')
            payload = dict(session_id=session_id, phase='build', run_id=build_run, spec_run_id=spec_run,
                           suite_id=frozen['suite_id'], evaluation_digest=evaluation)
            event = self.store._event(db, tenant, spec_run, 'autopilot.phase_linked', payload, actor, base + ':link')
            db.execute('INSERT INTO autopilot_links VALUES(?,?,?,?,?,?)',
                       (tenant, session_id, 'build', build_run, canonical(payload), event['hash']))
            return payload
        self.store._command(tenant, base + ':link', 'autopilot.link_phase', link_args, link)
        return 'build_started'

    def _deliver(self, tenant, session_id, run_id, progress, operator, actor):
        node = progress['nodes'][0]
        if node['status'] == 'submitted' and self.verification is not None:
            try:
                self.verification.check(tenant)
                preview = self.verification.jobs.preview(tenant, run_id, node['task_id'])
            except (Conflict, InvalidInput, NotFound):
                preview = None
            if preview and preview.get('ready'):
                self.verification.jobs.request(operator, run_id, node['task_id'], f'ap:{session_id}:verify:{node["epoch"]}',
                                               expected_preview_digest=preview['preview_digest'],
                                               reason='自动会话：程序已通过留出验收，申请独立双模型复核')
                return 'verification_requested'
            if preview and preview.get('latest') and preview['latest'].get('state') in {'pending', 'running'}:
                return None
        key = f'ap:{session_id}:delivered:{node["epoch"]}:{node["status"]}'
        args = dict(session_id=session_id, run_id=run_id, status=node['status'])

        def apply(db):
            self.store._event(db, tenant, run_id, 'autopilot.delivered',
                              dict(session_id=session_id, task_id=node['task_id'], task_status=node['status'],
                                   independently_verified=node['status'] == 'done', counts_as_success=False), actor, key)
            return dict(delivered=True)
        self.store._command(tenant, key, 'autopilot.deliver', args, apply)
        return 'delivered'

    # ----- read model for the conversation UI ----------------------------
    def list(self, tenant):
        with self.store.connect() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM autopilot_sessions WHERE tenant=? ORDER BY rowid DESC', (tenant,))]
        result = []
        for session_id in ids:
            try:
                view = self.get(tenant, session_id, include_source=False)
            except LoomError:
                continue
            result.append({k: view[k] for k in ('session_id', 'title', 'state', 'created_at', 'updated_at', 'thread_id')})
        # One sidebar row per conversation thread, showing its latest session.
        latest = {}
        for item in result:
            latest.setdefault(item['thread_id'], item)
        return [dict(item, session_id=item['session_id'], turns=sum(1 for r in result if r['thread_id'] == item['thread_id']))
                for item in latest.values()]

    def _events(self, db, tenant, run_id):
        return [json.loads(r['envelope']) for r in db.execute(
            'SELECT envelope FROM events WHERE tenant=? AND run_id=? ORDER BY seq', (tenant, run_id))]

    def get(self, tenant, session_id, *, include_source=True):
        with self.store.connect() as db:
            db.execute('BEGIN')
            record, links = self._session_row(db, tenant, session_id)
            runs = list(links.values())
            controls = self._control_events(db, tenant, session_id, runs)
            events = {phase: self._events(db, tenant, run) for phase, run in links.items()}
            run_rows = {phase: dict(self.store._run(db, tenant, run)) for phase, run in links.items()}
            candidates = {}
            for phase, run in links.items():
                for row in db.execute('SELECT id FROM artifact_candidates WHERE tenant=? AND run_id=? ORDER BY rowid', (tenant, run)):
                    try:
                        cand, body = self.artifacts._candidate(db, tenant, run, row['id'])
                    except LoomError:
                        continue
                    candidates[(cand['source_task_id'], cand['source_epoch'])] = (cand, body)
        progress = {}
        for phase, run in links.items():
            try:
                progress[phase] = self.progression.read(tenant, run)
            except LoomError:
                progress[phase] = None
        created_at = events['spec'][0]['ts'] if events['spec'] else None
        updated_at = max((e[-1]['ts'] for e in events.values() if e), default=created_at)
        steps = []
        role_steps = (('requirements', 'analyst', '分析需求'), ('checks', 'tester', '设计测试用例'))
        spec_nodes = {n['role']: n for n in (progress.get('spec') or {}).get('nodes', [])}
        for key, role, label in role_steps:
            node = spec_nodes.get(role)
            step = dict(key=key, label=label, role=role, run_id=links['spec'], task_id=node and node['task_id'],
                        status=self._step_status(node), content=None)
            if node and (node['task_id'], node['epoch']) in candidates:
                cand, body = candidates[(node['task_id'], node['epoch'])]
                step['artifact_id'] = cand['id']
                step['content'] = body
            steps.append(step)
        frozen = next((e['payload'] for e in events['spec'] if e['type'] == 'autopilot.acceptance_frozen'), None)
        if frozen:
            steps[1]['split'] = dict(development=len(frozen['development_case_names']),
                                     heldout=len(frozen['heldout_case_names']), origin='tester-model-heldout',
                                     heldout_names=list(frozen['heldout_case_names']))
        build = dict(key='implement', label='编写并测试程序', role='developer', run_id=links.get('build'),
                     task_id=None, status='waiting', attempts=[], source=None)
        if 'build' in links and progress.get('build'):
            node = progress['build']['nodes'][0] if progress['build']['nodes'] else None
            build.update(task_id=node and node['task_id'], status=self._step_status(node))
            for event in events['build']:
                payload = event['payload']
                if event['type'] == 'development.evaluated':
                    cases = payload.get('cases') or []
                    build['attempts'].append(dict(phase='development', epoch=payload.get('epoch'), passed=payload.get('passed'),
                                                  total=len(cases), passed_cases=sum(1 for c in cases if c.get('passed')),
                                                  cases=[dict(name=c.get('name'), passed=c.get('passed'), status=c.get('status')) for c in cases],
                                                  at=event['ts']))
                elif event['type'] == 'execution.evaluated':
                    # Aggregate counts only: held-out names/inputs stay out of the session view.
                    build['attempts'].append(dict(phase='acceptance', epoch=payload.get('epoch'),
                                                  passed=payload.get('hard_criteria_passed'),
                                                  total=payload.get('host_cases_total'), passed_cases=payload.get('host_cases_passed'),
                                                  at=event['ts']))
                elif event['type'] == 'failure.classified':
                    build['attempts'].append(dict(phase='stopped', epoch=payload.get('epoch'), code=payload.get('code'),
                                                  message=MESSAGES.get(payload.get('code')), at=event['ts']))
            if include_source and node and node['status'] in {'submitted', 'done'}:
                try:
                    from .review_inputs import ReviewInputService
                    review = ReviewInputService(self.store).preview(tenant, links['build'], node['task_id'], include_source=True)
                    build['source'] = review.get('files', {}).get('main.py')
                    build['execution_summary'] = review.get('execution_summary')
                except LoomError:
                    build['source'] = None
        steps.append(build)
        verify_status = 'waiting'
        if build['status'] == 'verified':
            verify_status = 'done'
        elif build['status'] == 'submitted':
            verify_status = 'pending' if self.verification is not None else 'not_configured'
        steps.append(dict(key='verify', label='独立双模型复核', role='validator', status=verify_status))
        state, attention = 'running', None
        if controls and controls[-1]['type'] == 'autopilot.stopped':
            payload = controls[-1]['payload']
            state, attention = 'needs_attention', dict(code=payload['code'], message=payload['message'],
                                                       task_id=payload.get('detail'), run_id=controls[-1]['run_id'])
        elif controls and controls[-1]['type'] == 'autopilot.delivered':
            state = 'verified' if controls[-1]['payload']['independently_verified'] else 'delivered'
        current = run_rows.get('build') or run_rows['spec']
        if state == 'running' and current['status'] == 'paused':
            state = 'paused'
        if state == 'running' and current['status'] in {'cancelled', 'terminated'}:
            state = 'terminated'
        spend = dict(cost_microusd=sum(r['spent'] for r in run_rows.values()),
                     reserved_microusd=sum(r['reserved'] for r in run_rows.values()),
                     tokens=sum(r['tokens_spent'] for r in run_rows.values()),
                     cost_limit_microusd=SPEC_BUDGET['cost_limit'] + BUILD_BUDGET['cost_limit'])
        activity = None
        if state == 'running':
            current_phase = 'build' if 'build' in links else 'spec'
            for event in reversed(events[current_phase]):
                if event['type'] in ACTIVITY:
                    activity = dict(text=ACTIVITY[event['type']], since=event['ts'])
                    break
        thread_id = record.get('thread_id') or session_id
        with self.store.connect() as db:
            thread = [json.loads(r['record'])['session_id'] for r in db.execute(
                'SELECT record FROM autopilot_sessions WHERE tenant=? ORDER BY rowid', (tenant,))
                if (json.loads(r['record']).get('thread_id') or json.loads(r['record'])['session_id']) == thread_id]
        return dict(schema_version='loom.session/v1', session_id=session_id, title=record['title'], prompt=record['prompt'],
                    request=record.get('request') or record['prompt'], thread_id=thread_id, thread=thread,
                    parent_session_id=record.get('parent_session_id'), activity=activity,
                    mode=record['mode'], state=state, attention=attention, created_at=created_at, updated_at=updated_at,
                    current_run_id=links.get('build') or links['spec'], runs=links, steps=steps, spend=spend,
                    executor_configured=self.config is not None,
                    acceptance_origin='tester-model-heldout', independently_verified=state == 'verified',
                    counts_as_success=False)

    @staticmethod
    def _step_status(node):
        if node is None:
            return 'waiting'
        if node['status'] == 'done':
            return 'verified'
        if node['status'] == 'submitted':
            return 'submitted'
        if node['status'] == 'running' or node['stage'] == 'running':
            return 'running'
        if node['stage'] in ATTENTION:
            return 'attention'
        return 'queued' if node['stage'] in {'local_execution_ready', 'host_tests_required'} else 'waiting'
