"""Resolve a frozen in-Goal validator selector against actual signed provenance."""
import json

from .review_inputs import ReviewInputService
from .store import Conflict, digest


def resolve_validation_input_in(store,db,tenant,spec):
    from .verification_jobs import VerificationJobs
    from .workflow import WorkflowService
    selector=spec.get('validation_source')
    if spec['role']!='validator' or not selector:
        raise Conflict('VALIDATION_SOURCE_REQUIRED')
    task_id=spec['run_id']+':'+selector['source_node']
    source=db.execute('SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?',(tenant,spec['run_id'],task_id)).fetchone()
    if source is None or source['status']!='done':raise Conflict('VALIDATION_SOURCE_NOT_VERIFIED')
    source_spec=WorkflowService._task_spec_in(db,store,tenant,spec['run_id'],task_id)
    if (source_spec['role'] not in {'developer','fixer'} or task_id not in spec['depends_on']
            or source_spec['evaluation_digest']!=spec['evaluation_digest']):
        raise Conflict('VALIDATION_SOURCE_CONTRACT_CHANGED')
    view=ReviewInputService(store)._preview_in(db,tenant,spec['run_id'],task_id,True)
    if view['subject']['schema_version']!='loom.review-subject-ref/v1' or not view['execution_summary']['hard_criteria_passed']:
        raise Conflict('VALIDATION_SOURCE_NOT_A_PASSED_PROGRAM')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='verification_requests'").fetchone():
        raise Conflict('VALIDATION_SOURCE_NOT_VERIFIED')
    jobs=VerificationJobs(store,initialize=False)
    rows=db.execute('SELECT * FROM verification_requests WHERE tenant=? AND run_id=? AND task_id=? AND epoch=?',
                    (tenant,spec['run_id'],task_id,source['epoch'])).fetchall()
    if len(rows)!=1:raise Conflict('VALIDATION_SOURCE_NOT_VERIFIED')
    state=jobs._state(db,rows[0])
    binding,_,_=jobs._binding_in(db,tenant,spec['run_id'],task_id,allow_verified_read=True)
    if state['state']!='verified' or state.get('task_verified') is not True or state['binding']!=binding.model_dump():
        raise Conflict('VALIDATION_SOURCE_PROOF_CHANGED')
    signed=jobs._attestations_in(db,tenant,rows[0]['id'])
    if (len(signed)!=2 or {s['body']['receipt']['model_id'] for s in signed}!={'deepseek-flash','deepseek-v4-pro'}
            or any(s['body']['passed'] is not True for s in signed)):
        raise Conflict('VALIDATION_SOURCE_PROOF_CHANGED')
    finished=[json.loads(row['envelope'])|{'hash':row['hash']} for row in db.execute(
        "SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='verification.finished' AND json_extract(envelope,'$.payload.job_id')=?",
        (tenant,spec['run_id'],rows[0]['id']))]
    if len(finished)!=1 or finished[0]['payload']['binding_digest']!=digest(binding.model_dump()):
        raise Conflict('VALIDATION_SOURCE_PROOF_CHANGED')
    return view,dict(schema_version='loom.validation-binding/v1',source=selector,subject=view['subject'],
        verification_job_id=rows[0]['id'],verification_event_hash=finished[0]['hash'])
