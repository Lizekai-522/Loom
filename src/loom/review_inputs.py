"""Read-only, exact reviewer inputs. No model calls, approvals or dispatch.

The local executor is the trust boundary for finite host tests. A consistent
local event chain is not an independent signature or a production trust root.
Hidden case names, inputs, expectations, stdout and diagnostics stay private.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from .artifact_schemas import (BODY_MODELS, MAX_CONTEXT_BYTES, ReviewSubjectReference, ReviewFinding,
                               ReviewFindingsBody)
from .artifacts import ArtifactService
from .sandbox import RUNNER_SHA256
from .store import Conflict, InvalidInput, NotFound, canonical, digest



class ReviewInputService:
    def __init__(self, store):
        self.store = store
        self.artifacts = ArtifactService(store, initialize=False)

    def _events_in(self, db, tenant, run_id):
        events = []
        try:
            for row in db.execute("SELECT * FROM events WHERE tenant=? AND run_id=? ORDER BY seq", (tenant, run_id)):
                event = json.loads(row["envelope"])
                if (type(event) is not dict or event["event_id"] != row["id"]
                        or event["seq"] != row["seq"] or event["tenant_id"] != tenant
                        or event["run_id"] != run_id or event["chain_id"] != run_id
                        or type(event["payload"]) is not dict):
                    raise ValueError
                events.append({**event, "hash": row["hash"]})
            if not events or not self.store.verify_events(events):
                raise ValueError
        except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
            raise Conflict("review source event chain is inconsistent") from None
        return events

    def _preview_in(self, db, tenant, run_id, task_id, include_source):
        run = self.store._run(db, tenant, run_id)
        row = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?", (tenant, run_id, task_id)).fetchone()
        if row is None:
            raise NotFound("review source task not found in tenant/run")
        if run["status"] == "cancelled" or row["status"] not in {"submitted", "done"} or not row["output"]:
            raise Conflict("review requires a current submitted implementation")
        spec = self.artifacts._contract(db, tenant, run_id, task_id)
        if spec['role'] in {'analyst','architect','tester','reviewer'}:
            return self._artifact_preview_in(db,tenant,run_id,row,spec,include_source)
        if (spec["role"] not in {"developer", "fixer"} or spec["scope"] != "local-workspace"
                or not {"workspace.write", "tests.run"} <= set(spec["tools"])):
            raise Conflict("review source is not an approved executable task")
        unknown = db.execute("SELECT 1 FROM effects WHERE tenant=? AND run_id=? AND task_id=? AND state IN ('intent','indeterminate','partial')",
                             (tenant, run_id, task_id)).fetchone()
        if unknown:
            raise Conflict("review source has an unresolved external effect")
        try:
            output = json.loads(row["output"])
            source = output["source"]
            report = output["tests"]
            if (output["kind"] != "python-json-program" or type(source) is not str or not source.strip()
                    or len(source.encode("utf-8")) > 262144
                    or output["plan_digest"] != spec["plan_digest"]
                    or output["evaluation_digest"] != spec["evaluation_digest"]
                    or output["source_sha256"] != digest({"main.py": source})
                    or type(report) is not dict or type(report["passed"]) is not bool
                    or report["source_sha256"] != output["source_sha256"]
                    or report["tests_sha256"] != spec["evaluation_digest"]
                    or report["runner_sha256"] != RUNNER_SHA256
                    or report["verdict_source"] != "host-frozen-json-v1"
                    or type(report["cases"]) is not list or not 1 <= len(report["cases"]) <= 32):
                raise ValueError
            cases = report["cases"]
            if (any(type(case) is not dict or type(case.get("passed")) is not bool for case in cases)
                    or report["passed"] != all(case["passed"] for case in cases)):
                raise ValueError
            content_hash = digest(output)
            events = self._events_in(db, tenant, run_id)
            owner=row['worker']
            if row['status']=='done' and owner is None:
                owners=[e['payload'].get('worker') for e in events if e['type']=='task.claimed'
                    and e['payload'].get('task_id')==task_id and type(e['payload'].get('epoch')) is int and e['payload']['epoch']==row['epoch']]
                if len(owners)!=1 or not isinstance(owners[0],str) or not owners[0]:raise ValueError
                owner=owners[0]
            evaluations = [e for e in events if e["type"] == "execution.evaluated"
                and e["actor"] == "local-executor" and e["payload"].get("task_id") == task_id
                and type(e["payload"].get("epoch")) is int and e["payload"]["epoch"] == row["epoch"]
                and e["payload"].get("content_hash") == content_hash]
            submissions = [e for e in events if e["type"] == "task.output_submitted"
                and e["actor"] == "worker:" + owner and e["payload"].get("task_id") == task_id
                and e["payload"].get("content_hash") == content_hash
                and canonical(e["payload"].get("output")) == canonical(output)]
            claims = [e for e in events if e["type"] == "task.claimed"
                and e["payload"].get("task_id") == task_id
                and type(e["payload"].get("epoch")) is int and e["payload"]["epoch"] == row["epoch"]
                and e["payload"].get("worker") == owner]
            if len(evaluations) != 1 or len(submissions) != 1 or len(claims) != 1:
                raise ValueError
            evaluated, submitted, claimed = evaluations[0], submissions[0], claims[0]
            evidence = evaluated["payload"]
            if (not claimed["seq"] < evaluated["seq"] < submitted["seq"]
                    or evidence.get("evaluation_digest") != spec["evaluation_digest"]
                    or type(evidence.get("hard_criteria_passed")) is not bool
                    or evidence["hard_criteria_passed"] != report["passed"]
                    or ("acceptance_report" in evidence and canonical(evidence["acceptance_report"]) != canonical(report))):
                raise ValueError
            subject = ReviewSubjectReference(schema_version="loom.review-subject-ref/v1",
                run_id=run_id, task_id=task_id, epoch=row["epoch"], plan_digest=spec["plan_digest"],
                content_hash=content_hash, source_sha256=output["source_sha256"],
                evaluation_digest=spec["evaluation_digest"], evaluation_event_hash=evaluated["hash"],
                submission_event_hash=submitted["hash"]).model_dump(mode="json")
        except (KeyError, TypeError, ValueError, RecursionError, UnicodeError, ValidationError):
            raise Conflict("review implementation or executor evidence binding is inconsistent") from None
        summary = {"cases_executed": len(cases), "cases_passed": sum(c["passed"] for c in cases),
                   "hard_criteria_passed": report["passed"], "report_digest": digest(report),
                   "evidence_source": "local-executor-host-frozen-tests"}
        context = {"schema_version": "loom.review-input/v1", "subject": subject,
                   "execution_summary": summary, "files": {"main.py": source}}
        if len(canonical(context).encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise Conflict("review context exceeds the static byte limit")
        result = {"schema_version": context["schema_version"], "subject": subject,
                  "execution_summary": summary, "input_digest": digest(context),
                  "source_included": include_source, "untrusted_source": True,
                  "independently_verified": False, "counts_as_success": False,
                  "execution_authorized": False, "hidden_tests_included": False,
                  "reviewer_generation_connected": True}
        if include_source:
            result["files"] = context["files"]
        return result

    def _artifact_preview_in(self,db,tenant,run_id,task,spec,include_source):
        if (spec['role']=='reviewer' and (not spec.get('review_subject')
                or spec['review_subject'].get('artifact_kind')=='review-findings')):
            # One primary review may be independently audited. Do not recursively
            # verify arbitrarily deep reviews of review reports.
            raise Conflict('nested review reports require a separately bounded adjudication path')
        """Schema evidence plus exact gateway origin; semantics still need two reviews."""
        if spec['scope']!='local-workspace' or 'artifacts.write' not in spec['tools']:
            raise Conflict('document review requires an approved typed source')
        if db.execute("SELECT 1 FROM effects WHERE tenant=? AND task_id=? AND state IN ('intent','indeterminate','partial')",(tenant,task['id'])).fetchone():
            raise Conflict('document review source has an unresolved external effect')
        try:
            output=json.loads(task['output'])
            if output['kind']!='role-artifact':raise ValueError
            row,body=self.artifacts._candidate(db,tenant,run_id,output['artifact_id'])
            provenance=self.artifacts._candidate_binding(row)
            stale=self.artifacts._stale_reasons(db,row)
            if task['status']=='done' and self.store._run(db,tenant,run_id)['status']=='succeeded':
                stale=[reason for reason in stale if reason!='run_terminal']
            if (stale or provenance.get('model_source_recorded') is not True
                    or row['source_task_id']!=task['id'] or row['source_epoch']!=task['epoch']
                    or row['kind']!=spec['role_output']['kind'] or row['plan_digest']!=spec['plan_digest']
                    or output['artifact_content_hash']!=row['content_hash'] or output['artifact_kind']!=row['kind']
                    or output['artifact_schema_version']!=row['schema_version'] or output['plan_digest']!=spec['plan_digest']
                    or output['model_call_id']!=provenance['model_provenance']['call_id']):raise ValueError
            events=self._events_in(db,tenant,run_id)
            claims=[e for e in events if e['type']=='task.claimed' and e['payload'].get('task_id')==task['id'] and e['payload'].get('epoch')==task['epoch']]
            generated=[e for e in events if e['type']=='role.candidate_generated' and e['hash']==row['source_event_hash'] and e['actor']=='local-executor']
            submitted=[e for e in events if e['type']=='task.output_submitted' and e['payload'].get('task_id')==task['id']
                and e['payload'].get('content_hash')==digest(output) and e['payload'].get('output')==output]
            if len(claims)!=1 or len(generated)!=1 or len(submitted)!=1:raise ValueError
            if (not claims[0]['seq']<generated[0]['seq']<submitted[0]['seq']
                    or submitted[0]['actor']!='worker:'+claims[0]['payload']['worker']
                    or (task['worker'] is not None and task['worker']!=claims[0]['payload']['worker'])):raise ValueError
            files={'artifact.json':json.dumps(body,ensure_ascii=False,indent=2,sort_keys=True)+'\n'}
            if row['kind']=='review-findings':
                if body['subject']!=spec['review_subject']:raise ValueError
                original=self.resolve_reference_in(db,tenant,spec['review_subject'])
                original_ref=original['subject']
                original_spec=self.artifacts._contract(db,tenant,original_ref['run_id'],original_ref['task_id'])
                roots=[event for event in self._events_in(db,tenant,original_ref['run_id']) if event['type']=='run.created']
                if len(roots)!=1 or not roots[0]['payload'].get('managed_workflow'):raise ValueError
                goal=db.execute('SELECT description,acceptance FROM workflow_goals WHERE tenant=? AND run_id=?',
                    (tenant,original_ref['run_id'])).fetchone()
                requirements=dict(description=roots[0]['payload']['description'],instruction=original_spec['instruction'],
                    acceptance=roots[0]['payload']['acceptance'])
                if (goal is None or goal['description']!=requirements['description']
                        or json.loads(goal['acceptance'])!=requirements['acceptance']):raise ValueError
                # Only source text, trusted summary counts and frozen requirements.
                # Private test inputs, expected outputs and model transcripts stay out.
                files['review-context.json']=canonical(dict(schema_version='loom.review-context/v1',
                    subject=original_ref,files=original['files'],execution_summary=original['execution_summary'],
                    original_requirements=requirements,untrusted_source=True))+'\n'
            schema_digest=digest(BODY_MODELS[row['kind']].model_json_schema())
            subject=ReviewSubjectReference(schema_version='loom.review-subject-ref/v2',run_id=run_id,task_id=task['id'],epoch=task['epoch'],
                plan_digest=spec['plan_digest'],content_hash=digest(output),source_sha256=digest(files),evaluation_digest=schema_digest,
                evaluation_event_hash=row['source_event_hash'],submission_event_hash=submitted[0]['hash'],artifact_id=row['id'],
                artifact_kind=row['kind'],artifact_schema_version=row['schema_version'],artifact_content_hash=row['content_hash']).model_dump(mode='json')
        except (KeyError,TypeError,ValueError,ValidationError):raise Conflict('document review source binding is inconsistent') from None
        summary=dict(cases_executed=0,cases_passed=0,hard_criteria_passed=True,report_digest=schema_digest,
            evidence_source='local-executor-typed-schema')
        context=dict(schema_version='loom.review-input/v1',subject=subject,execution_summary=summary,files=files)
        if len(canonical(context).encode('utf-8'))>MAX_CONTEXT_BYTES:raise Conflict('review context exceeds the static byte limit')
        result=dict(schema_version=context['schema_version'],subject=subject,execution_summary=summary,input_digest=digest(context),
            source_included=include_source,untrusted_source=True,independently_verified=False,counts_as_success=False,
            execution_authorized=False,hidden_tests_included=False,reviewer_generation_connected=True)
        if include_source:result['files']=files
        return result

    def preview(self, tenant, run_id, task_id, *, include_source=False):
        if type(include_source) is not bool:
            raise InvalidInput("include_source must be boolean")
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self._preview_in(db, tenant, run_id, task_id, include_source)

    def resolve_reference_in(self, db, tenant, reference):
        try:
            raw = reference.model_dump(mode="json") if isinstance(reference, ReviewSubjectReference) else reference
            ref = ReviewSubjectReference.model_validate(raw).model_dump(mode="json")
        except (ValidationError, TypeError, ValueError):
            raise InvalidInput("invalid review subject reference") from None
        current = self._preview_in(db, tenant, ref["run_id"], ref["task_id"], True)
        if current["subject"] != ref:
            raise Conflict("review subject reference is no longer current")
        return current

    @staticmethod
    def check_body(body, current):
        if body["subject"] != current["subject"]:
            raise Conflict("review findings refer to a changed implementation or evidence")
        path='main.py' if current['subject']['schema_version']=='loom.review-subject-ref/v1' else 'artifact.json'
        line_count = len(current["files"][path].splitlines())
        if any(f['path']!=path for f in body['findings']):raise InvalidInput('finding path differs from current source')
        if any(f["end_line"] > line_count for f in body["findings"]):
            raise InvalidInput("review finding line range exceeds the bound source")

    def validate_findings(self, tenant, run_id, task_id, body):
        try:
            if len(canonical(body).encode("utf-8")) > 32768:
                raise ValueError
            checked = ReviewFindingsBody.model_validate(body).model_dump(mode="json")
        except (ValidationError, TypeError, ValueError, RecursionError, UnicodeError, OverflowError):
            raise InvalidInput("review findings violate the strict candidate schema") from None
        with self.store.connect() as db:
            db.execute("BEGIN")
            current = self._preview_in(db, tenant, run_id, task_id, True)
            self.check_body(checked, current)
            return {"schema_validated": True, "subject_bound": True,
                    "content_hash": digest(checked), "input_digest": current["input_digest"],
                    "body": checked, "persisted": False, "model_source_recorded": False,
                    "independently_verified": False, "counts_as_success": False,
                    "execution_authorized": False}
