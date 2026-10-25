"""Reviewer -> exact operator grant -> fixer. Provider and sandbox are doubles."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.approved_execution import ApprovedExecutionService
from loom.artifacts import ArtifactService, ExecutionContextError
from loom.auth import LocalIdentity
from loom.execution import LocalTaskRunner
from loom.model_gateway import ModelGateway, ModelRoute
from loom.operator import OperatorService
from loom.role_execution import RoleTaskRunner
from loom.store import Conflict, InvalidInput, canonical, digest
from loom.workflow import WorkflowDefinition
from tests.test_execution import FakeSandbox, SOURCE
from tests.test_review_inputs import review_case, ready, candidate, ROOT
from tests.test_approved_execution import OP, WORKER


@pytest.fixture
def handoff(review_case):
    b = review_case; subject = ready(b)['subject']
    definition = json.loads((ROOT / 'packs/software-dev/workflow-v1.json').read_bytes())
    limits = {'cost_limit_microusd': 1000000, 'token_limit': 2000000, 'max_attempts': 1}
    definition['nodes'] = [
        dict(id='review', title='审查代码', instruction='审阅代码；仅报告可定位的问题，按中文输出。', role='reviewer',
            priority=20, limits=limits, review_subject=subject,
            role_output={'kind': 'review-findings', 'schema_version': 'loom.review-findings/v1'}),
        dict(id='fix', title='按批准意见修复', instruction='仅依据批准意见修改原代码，保持原验收要求。', role='fixer',
            priority=10, limits=limits, review_subject=subject, evaluation_digest=subject['evaluation_digest'],
            input_artifacts=[dict(source_node='review', kind='review-findings', schema_version='loom.review-findings/v1', required=True)]),
        dict(id='verify', title='独立验收', instruction='待独立模型验证，不得自报完成', role='validator', channel='verification',
            depends=['review', 'fix'], limits={'cost_limit_microusd': 100, 'token_limit': 100, 'max_attempts': 1})]
    goal = b['workflow'].create_goal('a', 'review-goal', title='审查修复', description='审查并按明确批准意见修复代码',
        acceptance=['tests.pass'], cost_limit=2000100, token_limit=4000100)
    target = goal['run_id']
    saved = b['workflow'].save_plan('a', target, 'review-save', definition, 0, actor='subject:operator')
    b['workflow'].approve_plan('a', target, 'review-approve', saved['revision'], saved['digest'], actor='subject:operator')
    service = ApprovedExecutionService(b['store']); artifacts = ArtifactService(b['store'])
    b.update(target=target, definition=definition, saved=saved, admission=service, artifacts=artifacts,
             body=candidate({'subject': subject}), requests=[], callback=None)
    return b


def reviewer_claim(b):
    params = ('a', b['target'], 'review', b['saved']['digest'], None)
    preview = b['admission'].preview(*params)
    return b['admission'].claim(*params, preview['preview_digest'], operator=OP, worker=WORKER)


async def generate(b, claim, *, content=None, timeout=False):
    async def handler(request):
        b['requests'].append(json.loads(request.content))
        if b['callback']:
            b['callback']()
        if timeout:
            raise httpx.ReadTimeout('simulated reviewer timeout', request=request)
        return httpx.Response(200, json={'id': 'reviewer-fixture', 'object': 'chat.completion', 'created': 1792700000,
            'model': 'deepseek-flash', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': content if content is not None else canonical(b['body'])}}],
            'usage': {'prompt_tokens': 25, 'completion_tokens': 10, 'total_tokens': 35}})
    async with ModelGateway([ModelRoute('deepseek-flash')], transport=httpx.MockTransport(handler),
            environ={'DEEPSEEK_API_KEY': 'reviewer-public-fixture-only'}) as gateway:
        return await RoleTaskRunner(b['store'], gateway).run_role(claim, max_output_tokens=1024)


def approve_fix(b, artifact):
    return b['artifacts'].approve_input('a', b['target'], b['target'] + ':fix', artifact['artifact_id'],
        'approve-findings', actor='subject:operator', expected_plan_digest=b['saved']['digest'],
        expected_source_epoch=artifact['source_epoch'], expected_target_epoch=0,
        expected_revision=artifact['revision'], expected_content_hash=artifact['content_hash'])


def review_and_approve(b):
    result = asyncio.run(generate(b, reviewer_claim(b)))
    assert result['status'] == 'submitted'
    artifact = b['artifacts'].get_artifact('a', b['target'], result['artifact_id'])
    approve_fix(b, artifact)
    return artifact


def test_review_generation_provenance_and_prompt_are_bound_without_hidden_tests(handoff):
    b = handoff; result = asyncio.run(generate(b, reviewer_claim(b)))
    assert result['status'] == 'submitted' and not result['verified'] and not result['counts_as_success']
    artifact = b['artifacts'].get_artifact('a', b['target'], result['artifact_id'])
    assert artifact['model_provenance']['role'] == 'reviewer' and artifact['body'] == b['body']
    prompt = canonical(b['requests'][0])
    assert SOURCE in json.loads(b['requests'][0]['messages'][1]['content'])['auxiliary_inputs']['review_input']['files']['main.py']
    for hidden in ('hidden-name-marker', 'secret-golden', 'SIMULATED OUTPUT'):
        assert hidden not in prompt
    with pytest.raises(Conflict, match='explicit operator'): b['admission'].preview('a', b['target'], 'fix', b['saved']['digest'], b['suite'])
    events = b['store'].events('a', b['target'])
    bound = next(e['payload'] for e in events if e['type'] == 'execution.context_bound')
    assert bound['review_subject'] == b['body']['subject'] and bound['context_item_count'] == 0
    assert not any(e['type'] in {'task.completed', 'artifact.verified'} for e in events)


def test_operator_grant_is_reapproved_atomically_for_exact_fixer_epoch(handoff):
    b = handoff; artifact = review_and_approve(b)
    params = ('a', b['target'], 'fix', b['saved']['digest'], b['suite'])
    preview = b['admission'].preview(*params)
    assert preview['input_grants'][0]['content_hash'] == artifact['content_hash']
    claim = b['admission'].claim(*params, preview['preview_digest'], operator=OP, worker=WORKER)
    context = b['artifacts'].capture_execution_context(claim)
    assert context['items'][0]['target_epoch'] == 1 and context['items'][0]['body'] == b['body']
    grants = [e for e in b['store'].events('a', b['target']) if e['type'] == 'artifact.input_approved']
    assert [e['payload']['target_epoch'] for e in grants] == [0, 1]
    assert all(e['actor'] == 'subject:operator' for e in grants)
    with pytest.raises(Conflict, match='already started'): b['admission'].claim(*params, preview['preview_digest'], operator=OP, worker=WORKER)


@pytest.mark.parametrize('change', ['subject', 'line', 'authority', 'duplicate-json', 'fenced', 'verdict'])
def test_invalid_model_body_keeps_known_usage_and_no_candidate(handoff, change):
    b = handoff; body = copy.deepcopy(b['body'])
    if change == 'subject': body['subject']['content_hash'] = '0' * 64
    elif change == 'line': body['findings'][0]['end_line'] = 500
    elif change == 'authority': body['verified'] = True
    elif change == 'verdict': body['verdict'] = 'passed'
    content = canonical(body)
    if change == 'duplicate-json': content = '{"schema_version":"x",' + content[1:]
    elif change == 'fenced': content = '```json\n' + content + '\n```'
    result = asyncio.run(generate(b, reviewer_claim(b), content=content))
    assert result['status'] == 'awaiting_human' and result['stop_reason'] == 'MODEL_ROLE_OUTPUT_INVALID'
    assert result['usage']['total_tokens'] == 35 and len(b['requests']) == 1
    assert b['artifacts'].list_artifacts('a', b['target']) == []
    assert b['store'].snapshot('a', b['target'])['reserved'] == 0


@pytest.mark.parametrize('change', ['source', 'pause', 'terminate', 'lease'])
def test_response_races_never_publish_review_candidate(handoff, change):
    b = handoff; claim = reviewer_claim(b)
    def alter():
        if change == 'source': b['store'].invalidate('a', b['run'], b['claim'].task_id, 'source changed')
        elif change == 'pause': OperatorService(b['store']).pause('a', b['target'], 'pause', 'subject:operator', 'pause model')
        elif change == 'terminate': OperatorService(b['store']).terminate('a', b['target'], 'terminate', 'subject:operator', 'cancel model')
        elif change == 'lease':
            with b['store'].transaction() as db: db.execute('UPDATE tasks SET lease_until=0 WHERE id=?', (claim.task_id,))
    b['callback'] = alter
    result = asyncio.run(generate(b, claim))
    assert result['status'] != 'submitted' and not result['verified']
    assert b['artifacts'].list_artifacts('a', b['target']) == [] and len(b['requests']) == 1


def test_unknown_model_call_keeps_reservation_and_cannot_be_repeated(handoff):
    b = handoff; result = asyncio.run(generate(b, reviewer_claim(b), timeout=True))
    assert result['status'] == 'awaiting_human' and b['store'].snapshot('a', b['target'])['reserved'] > 0
    with pytest.raises(Conflict): reviewer_claim(b)
    assert len(b['requests']) == 1


def test_stale_review_stays_readable_but_cannot_be_consumed(handoff):
    b = handoff; artifact = review_and_approve(b)
    b['store'].invalidate('a', b['run'], b['claim'].task_id, 'source changed')
    stale = b['artifacts'].get_artifact('a', b['target'], artifact['artifact_id'])
    assert stale['stale'] and 'review_subject_changed' in stale['stale_reasons'] and stale['body'] == b['body']
    with pytest.raises(Conflict): b['admission'].preview('a', b['target'], 'fix', b['saved']['digest'], b['suite'])


def test_no_findings_does_not_authorize_an_unnecessary_fixer(handoff):
    b = handoff; b['body'].update(verdict='no_findings', findings=[])
    result = asyncio.run(generate(b, reviewer_claim(b)))
    artifact = b['artifacts'].get_artifact('a', b['target'], result['artifact_id'])
    with pytest.raises(Conflict, match='explicit findings'): approve_fix(b, artifact)


def test_fixer_claim_audit_failure_rolls_back_epoch_and_reapproval(handoff):
    b = handoff; review_and_approve(b)
    params = ('a', b['target'], 'fix', b['saved']['digest'], b['suite']); preview = b['admission'].preview(*params)
    before = b['store'].events('a', b['target']); original = b['store']._event
    def fail(*args, **kwargs):
        if args[3] == 'execution.existing_plan_admitted': raise RuntimeError('synthetic audit fault')
        return original(*args, **kwargs)
    with patch.object(b['store'], '_event', side_effect=fail), pytest.raises(RuntimeError):
        b['admission'].claim(*params, preview['preview_digest'], operator=OP, worker=WORKER)
    assert b['store'].events('a', b['target']) == before
    assert b['admission'].preview(*params)['preview_digest'] == preview['preview_digest']


def test_concurrent_fixer_claims_have_one_owner_and_one_new_grant(handoff):
    b = handoff; review_and_approve(b); params = ('a', b['target'], 'fix', b['saved']['digest'], b['suite'])
    preview = b['admission'].preview(*params)
    def attempt(_):
        try: return b['admission'].claim(*params, preview['preview_digest'], operator=OP, worker=WORKER)
        except Conflict: return None
    with ThreadPoolExecutor(max_workers=2) as pool: claims = list(pool.map(attempt, range(2)))
    assert sum(c is not None for c in claims) == 1
    grants = [e for e in b['store'].events('a', b['target']) if e['type'] == 'artifact.input_approved']
    assert len(grants) == 2


@pytest.mark.parametrize('change', ['missing-subject', 'wrong-role', 'changed-acceptance', 'auto-repair', 'extra-attempts', 'optional-input', 'different-subject'])
def test_review_contract_rejects_ambiguous_or_expanded_authority(handoff, change):
    d = copy.deepcopy(handoff['definition']); review, fix = d['nodes'][:2]
    if change == 'missing-subject': review.pop('review_subject')
    elif change == 'wrong-role': review['role'] = 'analyst'
    elif change == 'changed-acceptance': fix['evaluation_digest'] = '0' * 64
    elif change == 'auto-repair': fix['repair'] = dict(schema_version='loom.rebind/v1', development_digest='1' * 64, max_repairs=1)
    elif change == 'extra-attempts': fix['limits']['max_attempts'] = 2
    elif change == 'optional-input': fix['input_artifacts'][0]['required'] = False
    elif change == 'different-subject': fix['review_subject'] = {**fix['review_subject'], 'content_hash': '0' * 64}
    with pytest.raises(ValueError): WorkflowDefinition.model_validate(d)


def test_source_change_at_atomic_admission_leaves_no_effect_or_reservation(handoff):
    b = handoff; claim = reviewer_claim(b); original = ArtifactService.admit_execution_effect
    def changed(service, *args, **kwargs):
        b['store'].invalidate('a', b['run'], b['claim'].task_id, 'changed before intent')
        return original(service, *args, **kwargs)
    with patch.object(ArtifactService, 'admit_execution_effect', changed): result = asyncio.run(generate(b, claim))
    assert result['status'] == 'awaiting_human' and not b['requests']
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM effects WHERE run_id=?', (b['target'],)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM reservations WHERE run_id=?', (b['target'],)).fetchone()[0] == 0


def test_hard_dependency_and_old_review_epoch_cannot_be_bypassed(handoff):
    b = handoff; review_and_approve(b)
    with b['store'].transaction() as db:
        db.execute('UPDATE tasks SET depends=? WHERE id=?', (canonical([b['target'] + ':review']), b['target'] + ':fix'))
    with pytest.raises(Conflict, match='hard dependencies'):
        b['admission'].preview('a', b['target'], 'fix', b['saved']['digest'], b['suite'])


def test_review_plan_approval_requires_operator_and_current_source(handoff):
    b = handoff
    goal = b['workflow'].create_goal('a', 'new-draft', title='审查草案', description='精确引用',
        acceptance=['tests.pass'], cost_limit=2000100, token_limit=4000100)
    saved = b['workflow'].save_plan('a', goal['run_id'], 'save-draft', b['definition'], 0)
    with pytest.raises(InvalidInput): b['workflow'].approve_plan('a', goal['run_id'], 'legacy-approve', saved['revision'], saved['digest'])
    b['store'].invalidate('a', b['run'], b['claim'].task_id, 'source drift')
    with pytest.raises(Conflict): b['workflow'].approve_plan('a', goal['run_id'], 'operator-approve', saved['revision'], saved['digest'], actor='subject:operator')


def test_review_api_shows_bound_source_and_only_operator_approves_input(handoff):
    b = handoff; result = asyncio.run(generate(b, reviewer_claim(b)))
    identities = (OP, WORKER, LocalIdentity('a', 'viewer', frozenset({'viewer'}), 'reviewer-fixture-viewer-00000000'))
    with TestClient(create_app(b['store'], {'a': 'reviewer-fixture-legacy-00000000'}, identities=identities)) as client:
        headers = lambda i: {'Authorization': 'Bearer ' + i.token, 'Idempotency-Key': 'http-approve-findings'}
        response = client.get('/v1/runs/' + b['target'] + '/review', headers=headers(identities[2]))
        task = next(t for t in response.json()['tasks'] if t['role'] == 'reviewer')
        assert task['artifact']['model_source_recorded'] and task['artifact']['model_provenance']['role'] == 'reviewer'
        assert client.post('/v1/goals/' + b['target'] + '/plan/approve',
            headers={'Authorization': 'Bearer reviewer-fixture-legacy-00000000', 'Idempotency-Key': 'legacy-approve'},
            json={'expected_revision': b['saved']['revision'], 'expected_digest': b['saved']['digest']}).status_code == 403
        artifact = b['artifacts'].get_artifact('a', b['target'], result['artifact_id'])
        body = dict(artifact_id=artifact['artifact_id'], expected_plan_digest=b['saved']['digest'],
            expected_source_epoch=artifact['source_epoch'], expected_target_epoch=0,
            expected_revision=artifact['revision'], expected_content_hash=artifact['content_hash'])
        path = f'/v1/runs/{b["target"]}/tasks/{b["target"]}:fix/auxiliary-inputs'
        for identity in identities[1:]: assert client.post(path, headers=headers(identity), json=body).status_code == 403
        assert client.post(path, headers=headers(OP), json=body).status_code == 200
        before = b['store'].events('a', b['target'])
        assert client.post(path, headers=headers(OP), json=body).status_code == 200
        assert b['store'].events('a', b['target']) == before


def test_reviewer_cli_previews_without_private_reads_and_rejects_changed_execute(handoff, tmp_path):
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('reviewer_cli', ROOT / 'scripts/run_reviewer_task.py')
    script = importlib.util.module_from_spec(spec); spec.loader.exec_module(script)
    b = handoff
    args = SimpleNamespace(db=b['store'].path, tenant='a', run_id=b['target'], node_id='review',
        expected_plan_digest=b['saved']['digest'], max_output_tokens=1024, execute=False, preview_digest=None,
        identities_file=None, secrets_file=None, model_config=ROOT / 'configs/models.example.json')
    with patch.object(script, 'load_identities', side_effect=AssertionError('private read')), \
         patch.object(script, 'load_project_secrets', side_effect=AssertionError('private read')):
        preview = asyncio.run(script.run(args))
        assert preview['mode'] == 'existing-plan-preview' and not preview['creates_new_goal']
        args.execute = True; args.preview_digest = '0' * 64
        with pytest.raises(ValueError, match='CURRENT_PREVIEW_REQUIRED'): asyncio.run(script.run(args))


def test_reviewer_budget_exhaustion_before_claim_creates_no_effect(handoff):
    b = handoff
    with b['store'].transaction() as db:
        db.execute('UPDATE runs SET spent=cost_limit WHERE id=?', (b['target'],))
    with pytest.raises(Conflict, match='budget'): reviewer_claim(b)
    with b['store'].connect() as db:
        assert db.execute('SELECT count(*) FROM effects WHERE run_id=?', (b['target'],)).fetchone()[0] == 0
