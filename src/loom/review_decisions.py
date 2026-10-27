"""Audited review routing, not acceptance, task completion or model dispatch."""
from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator

from .artifact_schemas import ArtifactModel, Hash, ReviewIdentifier, ReviewSubjectReference
from .artifacts import ArtifactService, _hash, _subject, _text
from .store import Conflict, InvalidInput, NotFound, canonical, digest


Decision = Literal["defer", "await_independent_review", "approve_fixer"]
STATES = {"defer": "awaiting_operator", "await_independent_review": "awaiting_independent_models",
          "approve_fixer": "fixer_input_approved"}
MAX_DECISIONS = 32
SCHEMA = """
CREATE TABLE IF NOT EXISTS review_decisions (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
 id TEXT NOT NULL, revision INTEGER NOT NULL, record TEXT NOT NULL, event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,run_id,artifact_id,revision),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id),
 FOREIGN KEY(tenant,artifact_id) REFERENCES artifact_candidates(tenant,id));
CREATE TRIGGER IF NOT EXISTS review_decisions_no_update BEFORE UPDATE ON review_decisions
 BEGIN SELECT RAISE(ABORT,'review decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS review_decisions_no_delete BEFORE DELETE ON review_decisions
 BEGIN SELECT RAISE(ABORT,'review decisions are append-only'); END;
"""


class CandidateReference(ArtifactModel):
    tenant_id: ReviewIdentifier
    run_id: ReviewIdentifier
    artifact_id: ReviewIdentifier
    source_task_id: ReviewIdentifier
    source_epoch: int = Field(ge=1)
    revision: int = Field(ge=1)
    plan_digest: Hash
    content_hash: Hash
    source_event_hash: Hash


class DecisionRecord(ArtifactModel):
    schema_version: Literal["loom.review-decision/v1"]
    decision_id: ReviewIdentifier
    revision: int = Field(ge=1, le=MAX_DECISIONS)
    reference: CandidateReference
    subject: ReviewSubjectReference
    preview_digest: Hash
    decision: Decision
    state: str
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    target_task_id: ReviewIdentifier | None = None
    grant_id: ReviewIdentifier | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (STATES[self.decision] != self.state or not self.reason.strip()
                or (self.decision == "approve_fixer") != (self.target_task_id is not None and self.grant_id is not None)
                or (self.decision != "approve_fixer" and (self.target_task_id is not None or self.grant_id is not None))):
            raise ValueError("review decision state or target differs")
        return self


class ReviewDecisionService:
    def __init__(self, store, *, initialize=True):
        self.store = store
        self.artifacts = ArtifactService(store, initialize=initialize)
        if initialize:
            with store.connect() as db:
                db.executescript(SCHEMA)

    @staticmethod
    def flags():
        return dict(counts_as_success=False, independently_verified=False, model_dispatched=False,
                    task_completed=False, independent_second_model="pending_configuration")

    @staticmethod
    def _reference(row):
        return CandidateReference(tenant_id=row["tenant"], run_id=row["run_id"], artifact_id=row["id"],
            **{key: row[key] for key in ("source_task_id", "source_epoch", "revision", "plan_digest", "content_hash", "source_event_hash")}).model_dump(mode="json")

    def _payload(self, record):
        return {"record": record, "record_digest": digest(record), **self.flags()}

    def _records_in(self, db, tenant, run_id, artifact_id):
        result = []
        for revision, row in enumerate(db.execute("SELECT * FROM review_decisions WHERE tenant=? AND run_id=? AND artifact_id=? ORDER BY revision",
                                                  (tenant, run_id, artifact_id)), 1):
            try:
                raw = json.loads(row["record"])
                checked = DecisionRecord.model_validate(raw).model_dump(mode="json")
                ref = checked["reference"]
                _subject(checked["actor"])
                if (canonical(raw) != canonical(checked) or checked["decision_id"] != row["id"]
                        or checked["revision"] != row["revision"] or row["revision"] != revision
                        or (ref["tenant_id"], ref["run_id"], ref["artifact_id"]) != (tenant, run_id, artifact_id)):
                    raise ValueError
            except (ValidationError, KeyError, TypeError, ValueError, RecursionError, InvalidInput):
                raise Conflict("review decision record binding is inconsistent") from None
            self.artifacts._event_matches(db, tenant, run_id, row["event_hash"], "review.decision_recorded",
                                          checked["actor"], self._payload(checked))
            result.append({**checked, "event_hash": row["event_hash"], "record_digest": digest(checked)})
        events = db.execute("SELECT hash FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='review.decision_recorded' AND json_extract(envelope,'$.payload.record.reference.artifact_id')=?",
                            (tenant, run_id, artifact_id)).fetchall()
        if {r[0] for r in events} != {r["event_hash"] for r in result}:
            raise Conflict("review decision history differs from its audit events")
        return result

    def _preview_in(self, db, tenant, run_id, artifact_id):
        run = self.store._run(db, tenant, run_id)
        row, body = self.artifacts._candidate(db, tenant, run_id, artifact_id)
        if row["kind"] != "review-findings" or not self.artifacts._candidate_binding(row).get("model_source_recorded"):
            raise Conflict("review routing requires a gateway-recorded review candidate")
        records = self._records_in(db, tenant, run_id, artifact_id)
        reasons = list(self.artifacts._stale_reasons(db, row))
        owner = db.execute("SELECT status FROM tasks WHERE tenant=? AND run_id=? AND id=?",
                           (tenant, run_id, row["source_task_id"])).fetchone()
        if owner is None or owner["status"] != "submitted": reasons.append("review_not_submitted")
        if run["status"] != "open": reasons.append("run_not_open")
        if db.execute("SELECT 1 FROM effects WHERE tenant=? AND run_id=? AND state IN ('intent','indeterminate','partial')", (tenant, run_id)).fetchone():
            reasons.append("unresolved_effects")
        if db.execute("SELECT 1 FROM reservations WHERE tenant=? AND run_id=? AND state='reserved'", (tenant, run_id)).fetchone():
            reasons.append("unsettled_reservations")
        targets = []
        done = {r[0] for r in db.execute("SELECT id FROM tasks WHERE tenant=? AND run_id=? AND status='done'", (tenant, run_id))}
        for task in db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? ORDER BY rowid", (tenant, run_id)):
            spec = self.artifacts._contract(db, tenant, run_id, task["id"])
            if (spec["role"] != "fixer" or spec.get("review_subject") != body["subject"]
                    or not any(s["source_node"] == row["source_node"] and s["kind"] == row["kind"] and s["required"] for s in spec.get("input_artifacts") or [])):
                continue
            prior = db.execute("SELECT id FROM artifact_input_grants WHERE tenant=? AND run_id=? AND target_task_id=? AND kind='review-findings' ORDER BY rowid DESC LIMIT 1",
                               (tenant, run_id, task["id"])).fetchone()
            approved = False
            if prior:
                try:
                    context = self.artifacts._preview_context_in(db, tenant, run_id, task["id"])
                    approved = any(i["artifact_id"] == artifact_id for i in context["items"])
                except (Conflict, InvalidInput, NotFound):
                    pass
            eligible = (task["status"] == "pending" and task["epoch"] == 0 and prior is None
                        and set(json.loads(task["depends"])) <= done)
            targets.append(dict(task_id=task["id"], title=task["title"], epoch=task["epoch"], status=task["status"],
                                eligible=eligible, input_already_approved=approved, existing_grant_id=prior["id"] if prior else None))
        latest = records[-1] if records else None
        routed = bool(latest and latest["decision"] != "defer") or any(t["input_already_approved"] for t in targets)
        allowed = []
        if not reasons and not routed and len(records) < MAX_DECISIONS:
            if len(records) < MAX_DECISIONS - 1: allowed.append("defer")
            if body["verdict"] == "no_findings": allowed.append("await_independent_review")
            elif any(t["eligible"] for t in targets): allowed.append("approve_fixer")
        state = latest["state"] if latest else "awaiting_operator"
        if any(t["input_already_approved"] for t in targets): state = "fixer_input_approved"
        preview = dict(schema_version="loom.review-decision-preview/v1", reference=self._reference(row), subject=body["subject"],
            verdict=body["verdict"], finding_count=len(body["findings"]), run_status=run["status"], state=state,
            ready=not reasons, stale=bool(reasons), stale_reasons=reasons, allowed_decisions=allowed,
            fixer_targets=targets, latest_decision_id=latest["decision_id"] if latest else None,
            latest_event_hash=latest["event_hash"] if latest else None, history_count=len(records),
            max_decisions=MAX_DECISIONS, **self.flags())
        return {**preview, "preview_digest": digest(preview)}, records

    def preview(self, tenant, run_id, artifact_id):
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self._preview_in(db, tenant, run_id, artifact_id)[0]

    def history(self, tenant, run_id, artifact_id):
        with self.store.connect() as db:
            db.execute("BEGIN")
            self.store._run(db, tenant, run_id)
            row, _ = self.artifacts._candidate(db, tenant, run_id, artifact_id)
            if row["kind"] != "review-findings":
                raise Conflict("review routing requires a review candidate")
            return dict(items=self._records_in(db, tenant, run_id, artifact_id), **self.flags())

    def decide(self, tenant, run_id, artifact_id, key, *, actor, expected_preview_digest, decision, reason, target_task_id=None):
        for value, label in ((tenant, "tenant"), (run_id, "run"), (artifact_id, "artifact"), (key, "idempotency key")):
            _text(value, label)
        _subject(actor); _hash(expected_preview_digest); _text(reason, "review reason", 2000)
        if "\x00" in reason: raise InvalidInput("review reason contains a forbidden character")
        try: reason.encode("utf-8")
        except UnicodeError: raise InvalidInput("review reason must be UTF-8") from None
        if type(decision) is not str or decision not in STATES:
            raise InvalidInput("unknown review decision")
        if decision == "approve_fixer": _text(target_task_id, "fixer target")
        elif target_task_id is not None: raise InvalidInput("this decision has no fixer target")
        args = dict(run_id=run_id, artifact_id=artifact_id, actor=actor, expected_preview_digest=expected_preview_digest,
                    decision=decision, reason=reason, target_task_id=target_task_id)

        def apply(db):
            current, records = self._preview_in(db, tenant, run_id, artifact_id)
            if current["preview_digest"] != expected_preview_digest or decision not in current["allowed_decisions"]:
                raise Conflict("review decision does not match the current eligible preview")
            grant_id = None
            if decision == "approve_fixer":
                if not any(t["task_id"] == target_task_id and t["eligible"] for t in current["fixer_targets"]):
                    raise Conflict("fixer target is not eligible for the reviewed candidate")
                ref = current["reference"]
                grant = self.artifacts._approve_input_in(db, tenant=tenant, key=key, run_id=run_id,
                    target_task_id=target_task_id, artifact_id=artifact_id, actor=actor,
                    expected_plan_digest=ref["plan_digest"], expected_source_epoch=ref["source_epoch"], expected_target_epoch=0,
                    expected_revision=ref["revision"], expected_content_hash=ref["content_hash"])
                grant_id = grant["grant_id"]
            record = DecisionRecord(schema_version="loom.review-decision/v1", decision_id=str(uuid4()),
                revision=len(records)+1, reference=current["reference"], subject=current["subject"],
                preview_digest=expected_preview_digest, decision=decision, state=STATES[decision], actor=actor,
                reason=reason, target_task_id=target_task_id, grant_id=grant_id).model_dump(mode="json")
            event = self.store._event(db, tenant, run_id, "review.decision_recorded", self._payload(record), actor, key)
            db.execute("INSERT INTO review_decisions VALUES (?,?,?,?,?,?,?)", (tenant, run_id, artifact_id,
                       record["decision_id"], record["revision"], canonical(record), event["hash"]))
            return {**record, "event_hash": event["hash"], "record_digest": digest(record), **self.flags()}
        return self.store._command(tenant, key, "review.decide", args, apply)
