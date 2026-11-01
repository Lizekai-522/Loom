"""Bind a same-Goal program selector only after its exact signed acceptance."""
import json
from .store import Conflict
from .validation_inputs import resolve_validation_input_in


def resolve_review_source_in(store,db,tenant,spec):
    from .workflow import WorkflowService
    selector=spec.get('review_source')
    if spec['role']!='reviewer' or not selector or spec.get('review_subject') is not None:
        raise Conflict('REVIEW_SOURCE_REQUIRED')
    source=WorkflowService._task_spec_in(db,store,tenant,spec['run_id'],spec['run_id']+':'+selector['source_node'])
    # Use the same authority reconstruction as the read-only validator. This
    # internal view cannot grant tools or change the actual frozen TaskSpec.
    view,binding=resolve_validation_input_in(store,db,tenant,{**spec,'role':'validator',
        'evaluation_digest':source['evaluation_digest'],
        'validation_source':dict(schema_version='loom.validation-source/v1',source_node=selector['source_node'])})
    from .review_inputs import ReviewInputService
    roots=[event for event in ReviewInputService(store)._events_in(db,tenant,spec['run_id']) if event['type']=='run.created']
    goal=db.execute('SELECT description,acceptance FROM workflow_goals WHERE tenant=? AND run_id=?',(tenant,spec['run_id'])).fetchone()
    if (len(roots)!=1 or goal is None or goal['description']!=roots[0]['payload']['description']
            or json.loads(goal['acceptance'])!=roots[0]['payload']['acceptance']):
        raise Conflict('REVIEW_SOURCE_REQUIREMENTS_CHANGED')
    requirements=dict(description=roots[0]['payload']['description'],instruction=source['instruction'],acceptance=roots[0]['payload']['acceptance'])
    return view,{**binding,'schema_version':'loom.review-source-binding/v1','source':selector,'original_requirements':requirements}
