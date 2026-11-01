"""Public diagnostics must not become a hidden-case or transcript download."""
import re


SAFE_FIELDS=frozenset({'task_id','run_id','epoch','artifact_id','content_hash','plan_digest','context_digest',
    'call_id','reservation_id','state','status','run_status','cost','tokens','actual_cost','actual_tokens',
    'counts_as_success','verified','hard_criteria_passed','generation_model_used','usage_cost_upper_microusd',
    'host_cases_total','host_cases_passed','host_cases_mismatch','host_cases_timeout','host_cases_runtime_error','host_cases_other_failure'})


def public_event_summaries(events,*,source_chain_verified):
    result=[]
    for event in events:
        payload={key:value for key,value in event['payload'].items() if key in SAFE_FIELDS
                 and (type(value) in (bool,int,float) or value is None or type(value) is str and len(value)<=200)}
        reason=event['payload'].get('reason')
        if type(reason) is str and re.fullmatch(r'[A-Z][A-Z0-9_]{0,199}',reason):payload['reason']=reason
        result.append({key:event[key] for key in ('event_id','seq','type','ts','occurred_at','actor') if key in event}
            |dict(payload=payload,original_event_hash=event['hash'],payload_redacted=True))
    return dict(schema_version='loom.public-event-summaries/v1',events=result,redacted=True,
        source_chain_verified=source_chain_verified,original_envelopes_included=False,counts_as_success=False)


def public_replay_summary(state):
    return {**state,'schema_version':'loom.public-replay/v1',
        'tasks':{task:{key:value for key,value in data.items() if key!='output'} for task,data in state['tasks'].items()},
        'outputs_redacted':True,'counts_as_success':False}
