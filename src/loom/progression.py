"""Read-only operation guidance from one authority snapshot, never a dispatch."""
import json

from .approved_execution import ApprovedExecutionService
from .artifacts import ArtifactService
from .execution import FLASH_CONTEXT_LIMIT, cost_upper_microusd
from .review_decisions import ReviewDecisionService
from .store import Conflict, InvalidInput, NotFound, digest

ROLES=('analyst','architect','tester','developer','reviewer','fixer','validator')


class ProgressionService:
    def __init__(self,store):
        self.store=store
        self.artifacts=ArtifactService(store,initialize=False)
        self.decisions=ReviewDecisionService(store,initialize=False)

    def read(self,tenant,run_id):
        with self.store.connect() as db:
            db.execute('BEGIN')
            run=self.store._run(db,tenant,run_id)
            plan=db.execute('SELECT plan_status,plan_revision,plan_digest FROM workflow_goals WHERE tenant=? AND run_id=?',(tenant,run_id)).fetchone()
            tasks=db.execute('SELECT * FROM tasks WHERE tenant=? AND run_id=? ORDER BY priority DESC,rowid',(tenant,run_id)).fetchall()
            done={t['id'] for t in tasks if t['status']=='done'}
            unknown=bool(db.execute("SELECT 1 FROM effects WHERE tenant=? AND run_id=? AND state IN ('intent','indeterminate','partial')",(tenant,run_id)).fetchone())
            reserved=bool(db.execute("SELECT 1 FROM reservations WHERE tenant=? AND run_id=? AND state='reserved'",(tenant,run_id)).fetchone())
            candidates=[]
            for row in db.execute('SELECT id FROM artifact_candidates WHERE tenant=? AND run_id=? ORDER BY rowid DESC',(tenant,run_id)):
                try:
                    candidate,body=self.artifacts._candidate(db,tenant,run_id,row['id'])
                    candidates.append((candidate,body,not self.artifacts._stale_reasons(db,candidate)))
                except (Conflict,InvalidInput,NotFound):continue
            nodes=[]
            for task in tasks:
                item=dict(task_id=task['id'],title=task['title'],epoch=task['epoch'],status=task['status'],role=None,
                    action='view_evidence',stage='contract_unavailable',blocked_by=[],suggested_inputs=[],artifact_id=None,
                    ready_for_preview=False,requires_host_tests=False,execution_preview_digest=None,review_state=None)
                if plan is None or plan['plan_status']!='approved':
                    item.update(action='view_plan',stage='plan_approval_required');nodes.append(item);continue
                try:spec=self.artifacts._contract(db,tenant,run_id,task['id'])
                except (Conflict,InvalidInput,NotFound):nodes.append(item);continue
                item.update(role=spec['role'],node_id=spec['node_id'],evaluation_digest=spec['evaluation_digest'],requires_host_tests=spec['role'] in {'developer','fixer'},
                    blocked_by=[i for i in json.loads(task['depends']) if i not in done])
                own=next(((row,body,current) for row,body,current in candidates if row['source_task_id']==task['id'] and row['source_epoch']==task['epoch']),None)
                if own and own[2]:item['artifact_id']=own[0]['id']
                if task['status']=='done':item.update(stage='verified',action='view_evidence')
                elif task['status']=='submitted':
                    item.update(stage='independent_verification_required',action='view_candidate' if item['artifact_id'] else 'view_evidence')
                    if item['artifact_id'] and own[0]['kind']=='review-findings':
                        try:
                            preview,_=self.decisions._preview_in(db,tenant,run_id,item['artifact_id'])
                            item.update(review_state=preview['state'],stage='review_routing_required' if preview['allowed_decisions'] else preview['state'],action='review_decision')
                        except (Conflict,InvalidInput,NotFound):item.update(stage='review_source_unavailable')
                elif task['status']=='running':item.update(stage='running',action='view_evidence')
                elif task['status']!='pending' or task['epoch']!=0:item.update(stage='attempt_requires_review',action='view_interventions')
                elif run['status']!='open':item.update(stage='run_control_required',action='view_interventions')
                elif unknown or reserved:item.update(stage='effect_reconciliation_required',action='view_interventions')
                elif spec['role']=='validator':item.update(stage='independent_model_pending',action='view_interventions')
                elif item['blocked_by']:item.update(stage='hard_dependencies_blocked',action='view_evidence')
                else:
                    try:
                        context=self.artifacts._preview_context_in(db,tenant,run_id,task['id'])
                        if not context['ready']:
                            item.update(stage='input_approval_required',action='view_inputs')
                            for selector in context['missing_inputs']:
                                if not selector['required']:continue
                                match=next(((row,body) for row,body,current in candidates if current and row['source_node']==selector['source_node'] and row['kind']==selector['kind'] and row['schema_version']==selector['schema_version']),None)
                                if match:
                                    candidate,body=match
                                    if candidate['kind']=='review-findings' and body['verdict']=='no_findings':
                                        item.update(stage='no_fix_findings',action='review_decision',artifact_id=candidate['id'])
                                    else:item['suggested_inputs'].append(dict(artifact_id=candidate['id'],kind=candidate['kind'],source_node=candidate['source_node']))
                        elif (spec['limits']['cost_limit_microusd']<cost_upper_microusd(FLASH_CONTEXT_LIMIT,1024)
                              or spec['limits']['token_limit']<FLASH_CONTEXT_LIMIT+1024
                              or run['cost_limit']-run['spent']-run['reserved']<cost_upper_microusd(FLASH_CONTEXT_LIMIT,1024)
                              or run['token_limit']-run['tokens_spent']-run['tokens_reserved']<FLASH_CONTEXT_LIMIT+1024):
                            item.update(stage='budget_insufficient',action='view_interventions')
                        elif spec['role'] in {'analyst','architect','tester','reviewer'}:
                            preview=ApprovedExecutionService(self.store)._check_in(db,tenant,run_id,spec['node_id'],plan['plan_digest'],None,1024)[1]
                            item.update(stage='local_execution_ready',action='view_execution',ready_for_preview=True,execution_preview_digest=preview['preview_digest'])
                        elif spec['evaluation_digest'] is not None and (
                            (spec['role']=='developer' and not spec.get('development_suite') and not spec.get('repair'))
                            or (spec['role'] in {'developer','fixer'} and spec.get('development_suite') and spec.get('repair') and not spec.get('input_artifacts'))
                            or (spec['role']=='fixer' and spec.get('review_subject'))):
                            item.update(stage='host_tests_required',action='view_execution',ready_for_preview=True)
                        else:item.update(stage='contract_unavailable',action='view_plan')
                    except (Conflict,InvalidInput,NotFound):item.update(stage='source_or_contract_changed',action='view_inputs')
                nodes.append(item)
            result=dict(schema_version='loom.progression/v1',tenant_id=tenant,run_id=run_id,run_status=run['status'],
                plan_status=plan['plan_status'] if plan else None,plan_digest=plan['plan_digest'] if plan else None,
                nodes=nodes,roles=[dict(role=role,task_count=sum(n['role']==role for n in nodes),verified_count=sum(n['role']==role and n['status']=='done' for n in nodes)) for role in ROLES],
                unresolved_effects=unknown,unsettled_reservations=reserved,model_dispatched=False,counts_as_success=False,
                independent_second_model='pending_configuration',requires_explicit_execute=True)
            return {**result,'progress_digest':digest(result)}
