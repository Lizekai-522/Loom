"""Immutable, explicitly approved auxiliary data for the local workflow.

Authentication is an adapter boundary: the caller of approve_input must be an
authenticated operator. A worker submission is data, never model provenance,
independent verification, task completion, or permission to run anything.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from uuid import uuid4

from pydantic import ValidationError

from .artifact_schemas import (ARTIFACT_ROLES, MAX_CONTEXT_BYTES, validate_body)
from .store import Claim, Conflict, InvalidInput, NotFound, Store, canonical, digest
from .workflow import WorkflowDefinition, WorkflowService


SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_candidates (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL,
 source_task_id TEXT NOT NULL, source_node TEXT NOT NULL, source_epoch INTEGER NOT NULL,
 kind TEXT NOT NULL, revision INTEGER NOT NULL, plan_digest TEXT NOT NULL,
 schema_version TEXT NOT NULL, content_hash TEXT NOT NULL, body TEXT NOT NULL,
 actor TEXT NOT NULL, source_event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,run_id,source_task_id,kind,revision),
 FOREIGN KEY(tenant,source_task_id) REFERENCES tasks(tenant,id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TABLE IF NOT EXISTS artifact_input_grants (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL,
 target_task_id TEXT NOT NULL, target_epoch INTEGER NOT NULL, artifact_id TEXT NOT NULL, source_task_id TEXT NOT NULL,
 source_node TEXT NOT NULL, kind TEXT NOT NULL, source_epoch INTEGER NOT NULL,
 revision INTEGER NOT NULL, content_hash TEXT NOT NULL, plan_digest TEXT NOT NULL,
 source_event_hash TEXT NOT NULL, actor TEXT NOT NULL, approval_event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,artifact_id) REFERENCES artifact_candidates(tenant,id),
 FOREIGN KEY(tenant,target_task_id) REFERENCES tasks(tenant,id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TRIGGER IF NOT EXISTS artifact_candidates_no_update BEFORE UPDATE ON artifact_candidates
 BEGIN SELECT RAISE(ABORT,'artifact candidates are append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_candidates_no_delete BEFORE DELETE ON artifact_candidates
 BEGIN SELECT RAISE(ABORT,'artifact candidates are append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_input_grants_no_update BEFORE UPDATE ON artifact_input_grants
 BEGIN SELECT RAISE(ABORT,'artifact input grants are append-only'); END;
CREATE TRIGGER IF NOT EXISTS artifact_input_grants_no_delete BEFORE DELETE ON artifact_input_grants
 BEGIN SELECT RAISE(ABORT,'artifact input grants are append-only'); END;
"""


def _text(value, label, maximum=200):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise InvalidInput(label + " requires bounded nonempty text")


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise InvalidInput("expected digest must be a SHA-256 hexadecimal digest")


def _subject(actor):
    _text(actor, "authenticated actor")
    if not actor.startswith("subject:") or not actor[8:].strip() or len(actor[8:]) > 128:
        raise InvalidInput("a named authenticated subject is required")


class ExecutionContextError(Conflict):
    """Expected auxiliary-input stop; never includes supplied data in errors."""
    code = "EXECUTION_CONTEXT_INVALID"
    REASONS = frozenset({"CONTEXT_REQUIRED_INPUT_MISSING", "CONTEXT_SNAPSHOT_CHANGED",
                         "CONTEXT_BINDING_INVALID", "CONTEXT_TOO_LARGE"})

    def __init__(self, reason):
        self.reason = reason if reason in self.REASONS else "CONTEXT_BINDING_INVALID"
        super().__init__(self.reason)


class ArtifactService:
    def __init__(self, store: Store):
        self.store = store
        WorkflowService(store)
        with store.connect() as db:
            db.executescript(SCHEMA)

    def _contract(self, db, tenant, run_id, task_id):
        """Rebuild from the frozen definition in the caller's same snapshot."""
        run = self.store._run(db, tenant, run_id)
        goal = WorkflowService._goal(db, tenant, run_id)
        row = db.execute("SELECT * FROM workflow_tasks WHERE tenant=? AND run_id=? AND task_id=?",
                         (tenant, run_id, task_id)).fetchone()
        if row is None:
            raise NotFound("workflow task not found in tenant/run")
        if goal["plan_status"] != "approved" or row["plan_digest"] != goal["plan_digest"]:
            raise Conflict("artifact operation requires the approved workflow plan")
        try:
            definition = WorkflowDefinition.model_validate(json.loads(goal["definition"]))
            actual = json.loads(row["contract"])
            node = next((node for node in definition.nodes if node.id == row["node_id"]), None)
            if node is None or run_id + ":" + node.id != task_id:
                raise ValueError
            expected = WorkflowService._contract(definition, node, goal, run)
            if (definition.execution_digest != goal["plan_digest"]
                    or expected["evaluation_digest"] != row["evaluation_digest"]
                    or canonical({k: v for k, v in actual.items() if k != "status"})
                    != canonical({k: v for k, v in expected.items() if k != "status"})):
                raise ValueError
        except (ValidationError, TypeError, ValueError, AttributeError, RecursionError):
            raise Conflict("approved artifact contract binding is inconsistent") from None
        return expected

    @staticmethod
    def _candidate_binding(row):
        return {"artifact_id": row["id"], "tenant_id": row["tenant"], "run_id": row["run_id"],
                "source_task_id": row["source_task_id"], "source_node": row["source_node"],
                "source_epoch": row["source_epoch"], "kind": row["kind"], "revision": row["revision"],
                "plan_digest": row["plan_digest"], "schema_version": row["schema_version"],
                "content_hash": row["content_hash"], "actor": row["actor"], "status": "candidate",
                "origin": "authenticated-worker-submission", "producer_verified": False,
                "counts_as_success": False}

    @staticmethod
    def _grant_binding(row):
        return {"grant_id": row["id"], "tenant_id": row["tenant"], "run_id": row["run_id"],
                "target_task_id": row["target_task_id"], "target_epoch": row["target_epoch"], "artifact_id": row["artifact_id"],
                "source_task_id": row["source_task_id"], "source_node": row["source_node"],
                "kind": row["kind"], "source_epoch": row["source_epoch"], "revision": row["revision"],
                "content_hash": row["content_hash"], "plan_digest": row["plan_digest"],
                "source_event_hash": row["source_event_hash"], "actor": row["actor"],
                "purpose": "auxiliary-input", "confers_authority": False,
                "producer_verified": False, "counts_as_success": False, "untrusted_data": True}

    @staticmethod
    def _event_matches(db, tenant, run_id, event_hash, event_type, actor, payload):
        row = db.execute("SELECT * FROM events WHERE tenant=? AND run_id=? AND hash=?",
                         (tenant, run_id, event_hash)).fetchone()
        if row is None:
            raise Conflict("artifact provenance event is missing")
        try:
            event = json.loads(row["envelope"])
            valid = (digest(event) == row["hash"] and event["event_id"] == row["id"]
                     and event["seq"] == row["seq"] and event["tenant_id"] == tenant
                     and event["run_id"] == run_id and event["chain_id"] == run_id
                     and event["type"] == event_type and event["actor"] == actor
                     and canonical(event["payload"]) == canonical(payload))
        except (KeyError, TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            raise Conflict("artifact provenance event binding is inconsistent")

    def _candidate(self, db, tenant, run_id, artifact_id):
        row = db.execute("SELECT * FROM artifact_candidates WHERE tenant=? AND run_id=? AND id=?",
                         (tenant, run_id, artifact_id)).fetchone()
        if row is None:
            raise NotFound("candidate artifact not found in tenant/run")
        try:
            raw = json.loads(row["body"])
            body = validate_body(row["kind"], raw)
            if canonical(body) != canonical(raw) or digest(body) != row["content_hash"]:
                raise ValueError
        except (InvalidInput, TypeError, ValueError, RecursionError):
            raise Conflict("candidate artifact content binding is inconsistent") from None
        binding = self._candidate_binding(row)
        self._event_matches(db, tenant, run_id, row["source_event_hash"], "artifact.candidate_saved",
                            row["actor"], {**binding, "body": body})
        return row, body

    def _stale_reasons(self, db, row):
        tenant, run_id = row["tenant"], row["run_id"]
        source = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?",
                            (tenant, run_id, row["source_task_id"])).fetchone()
        contract = self._contract(db, tenant, run_id, row["source_task_id"])
        if (contract["node_id"] != row["source_node"] or contract["role"] != ARTIFACT_ROLES.get(row["kind"])
                or contract["plan_digest"] != row["plan_digest"]
                or "artifacts.write" not in contract["tools"] or contract["task_id"] != row["source_task_id"]
                or row["schema_version"] != json.loads(row["body"])["schema_version"]):
            raise Conflict("candidate source differs from its approved contract")
        reasons = []
        if source is None or source["epoch"] != row["source_epoch"]:
            reasons.append("source_epoch_changed")
        if source is None or source["status"] not in {"running", "submitted", "done"}:
            reasons.append("source_not_active")
        elif source["status"] == "running" and (source["lease_until"] is None or source["lease_until"] <= self.store.now(db)):
            reasons.append("source_lease_expired")
        if self.store._run(db, tenant, run_id)["status"] in {"cancelled", "succeeded"}:
            reasons.append("run_terminal")
        latest = db.execute("SELECT max(revision) FROM artifact_candidates WHERE tenant=? AND run_id=? AND source_task_id=? AND kind=?",
                            (tenant, run_id, row["source_task_id"], row["kind"])).fetchone()[0]
        if latest != row["revision"]:
            reasons.append("source_revision_superseded")
        return reasons

    def _view(self, db, row, body, include_body=True):
        reasons = self._stale_reasons(db, row)
        result = {**self._candidate_binding(row), "source_event_hash": row["source_event_hash"],
                  "stale": bool(reasons), "stale_reasons": reasons, "untrusted_data": True}
        if include_body:
            result["body"] = body
        return result

    def save_candidate(self, claim: Claim, key: str, kind: str, body: dict, *, actor: str):
        if not isinstance(claim, Claim):
            raise InvalidInput("a current worker claim is required")
        for value, label in ((claim.tenant_id, "tenant"), (claim.run_id, "run ID"),
                             (claim.task_id, "source task"), (claim.worker_id, "worker subject")):
            _text(value, label)
        if type(claim.epoch) is not int or claim.epoch < 1:
            raise InvalidInput("a positive integer claim epoch is required")
        _text(key, "idempotency key")
        _subject(actor)
        if actor != "subject:" + claim.worker_id:
            raise Conflict("candidate actor does not own the worker claim")
        body = validate_body(kind, body)
        args = {"run_id": claim.run_id, "task_id": claim.task_id, "epoch": claim.epoch,
                "worker_id": claim.worker_id, "kind": kind, "body": body, "actor": actor}

        def apply(db):
            self.store._assert_claim(db, claim)
            contract = self._contract(db, claim.tenant_id, claim.run_id, claim.task_id)
            if contract["role"] != ARTIFACT_ROLES[kind] or "artifacts.write" not in contract["tools"]:
                raise Conflict("approved source role cannot produce this artifact type")
            revision = db.execute("SELECT coalesce(max(revision),0)+1 FROM artifact_candidates WHERE tenant=? AND run_id=? AND source_task_id=? AND kind=?",
                                  (claim.tenant_id, claim.run_id, claim.task_id, kind)).fetchone()[0]
            values = dict(tenant=claim.tenant_id, run_id=claim.run_id, id=str(uuid4()),
                          source_task_id=claim.task_id, source_node=contract["node_id"], source_epoch=claim.epoch,
                          kind=kind, revision=revision, plan_digest=contract["plan_digest"],
                          schema_version=body["schema_version"], content_hash=digest(body), body=canonical(body), actor=actor)
            event = self.store._event(db, claim.tenant_id, claim.run_id, "artifact.candidate_saved",
                                      {**self._candidate_binding(values), "body": body}, actor, key)
            values["source_event_hash"] = event["hash"]
            columns = list(values)
            db.execute("INSERT INTO artifact_candidates (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                       tuple(values[column] for column in columns))
            return self._view(db, values, body)

        return self.store._command(claim.tenant_id, key, "artifact.save_candidate", args, apply)

    def get_artifact(self, tenant, run_id, artifact_id, *, include_body=True):
        if type(include_body) is not bool:
            raise InvalidInput("include_body must be boolean")
        with self.store.connect() as db:
            db.execute("BEGIN")
            self.store._run(db, tenant, run_id)
            row, body = self._candidate(db, tenant, run_id, artifact_id)
            return self._view(db, row, body, include_body)

    def list_artifacts(self, tenant, run_id, *, task_id=None):
        with self.store.connect() as db:
            db.execute("BEGIN")
            self.store._run(db, tenant, run_id)
            if task_id is not None:
                self._contract(db, tenant, run_id, task_id)
            rows = db.execute("SELECT id FROM artifact_candidates WHERE tenant=? AND run_id=? AND (? IS NULL OR source_task_id=?) ORDER BY rowid",
                              (tenant, run_id, task_id, task_id)).fetchall()
            result = []
            for item in rows:
                row, body = self._candidate(db, tenant, run_id, item["id"])
                result.append(self._view(db, row, body, False))
            return result

    def approve_input(self, tenant, run_id, target_task_id, artifact_id, key, *, actor,
                      expected_plan_digest, expected_source_epoch, expected_target_epoch,
                      expected_revision, expected_content_hash):
        """Trusted operator entry point; adapters must enforce the operator role."""
        for value, name in ((tenant, "tenant"), (run_id, "run ID"), (target_task_id, "target task"),
                            (artifact_id, "artifact ID"), (key, "idempotency key")):
            _text(value, name)
        _subject(actor)
        _hash(expected_plan_digest)
        _hash(expected_content_hash)
        if any(type(value) is not int or value < 1 for value in (expected_source_epoch, expected_revision)):
            raise InvalidInput("source epoch and revision must be positive integers")
        if type(expected_target_epoch) is not int or expected_target_epoch < 0:
            raise InvalidInput("target epoch must be a nonnegative integer")
        args = dict(run_id=run_id, target_task_id=target_task_id, artifact_id=artifact_id, actor=actor,
                    expected_plan_digest=expected_plan_digest, expected_source_epoch=expected_source_epoch,
                    expected_target_epoch=expected_target_epoch,
                    expected_revision=expected_revision, expected_content_hash=expected_content_hash)

        def apply(db):
            if self.store._run(db, tenant, run_id)["status"] != "open":
                raise Conflict("new artifact grants require an open run")
            target = self._contract(db, tenant, run_id, target_task_id)
            target_row = db.execute("SELECT status,epoch FROM tasks WHERE tenant=? AND run_id=? AND id=?",
                                    (tenant, run_id, target_task_id)).fetchone()
            if target_row is None or target_row["status"] in {"stale", "cancelled", "done", "indeterminate"}:
                raise Conflict("target task is not eligible for auxiliary input")
            if target_row["epoch"] != expected_target_epoch:
                raise Conflict("target epoch changed since the input was reviewed")
            row, body = self._candidate(db, tenant, run_id, artifact_id)
            if self._stale_reasons(db, row):
                raise Conflict("candidate artifact is stale")
            selectors = target.get("input_artifacts") or []
            if "artifacts.read" not in target["tools"] or not any(
                    selector["source_node"] == row["source_node"] and selector["kind"] == row["kind"]
                    and selector["schema_version"] == row["schema_version"] for selector in selectors):
                raise Conflict("approved target does not permit this auxiliary input")
            if (target["plan_digest"] != expected_plan_digest or row["plan_digest"] != expected_plan_digest
                    or row["source_epoch"] != expected_source_epoch or row["revision"] != expected_revision
                    or row["content_hash"] != expected_content_hash):
                raise Conflict("approval does not match the reviewed candidate binding")
            values = dict(tenant=tenant, run_id=run_id, id=str(uuid4()), target_task_id=target_task_id,
                          target_epoch=target_row["epoch"],
                          artifact_id=artifact_id, source_task_id=row["source_task_id"], source_node=row["source_node"],
                          kind=row["kind"], source_epoch=row["source_epoch"], revision=row["revision"],
                          content_hash=row["content_hash"], plan_digest=row["plan_digest"],
                          source_event_hash=row["source_event_hash"], actor=actor)
            event = self.store._event(db, tenant, run_id, "artifact.input_approved", self._grant_binding(values), actor, key)
            values["approval_event_hash"] = event["hash"]
            columns = list(values)
            db.execute("INSERT INTO artifact_input_grants (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                       tuple(values[column] for column in columns))
            return {**self._grant_binding(values), "approval_event_hash": event["hash"], "execution_connected": False}

        return self.store._command(tenant, key, "artifact.approve_input", args, apply)

    def preview_context(self, tenant, run_id, target_task_id):
        """Read only. Never copies arbitrary task outputs or resolves references."""
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self._preview_context_in(db, tenant, run_id, target_task_id)

    @staticmethod
    def _bounded_context(context, *, execution=False):
        result = {**context, "context_digest": digest(context), "bytes": 0}
        # Both derived fields, including the number of digits in bytes, count.
        while (size := len(canonical(result).encode("utf-8"))) != result["bytes"]:
            result["bytes"] = size
        if result["bytes"] > MAX_CONTEXT_BYTES:
            if execution:
                raise ExecutionContextError("CONTEXT_TOO_LARGE")
            raise Conflict("artifact context exceeds its static byte limit")
        return result

    def _preview_context_in(self, db, tenant, run_id, target_task_id, *, execution=False):
        target = self._contract(db, tenant, run_id, target_task_id)
        state = db.execute("SELECT status,epoch FROM tasks WHERE tenant=? AND run_id=? AND id=?",
                           (tenant, run_id, target_task_id)).fetchone()
        if state is None or state["status"] in {"stale", "cancelled", "indeterminate"}:
            raise Conflict("target task is no longer valid for context preview")
        items, missing = [], []
        for selector in target.get("input_artifacts") or []:
            grant = db.execute("SELECT * FROM artifact_input_grants WHERE tenant=? AND run_id=? AND target_task_id=? AND source_node=? AND kind=? ORDER BY rowid DESC LIMIT 1",
                               (tenant, run_id, target_task_id, selector["source_node"], selector["kind"])).fetchone()
            if grant is None:
                missing.append(selector)
                continue
            self._event_matches(db, tenant, run_id, grant["approval_event_hash"], "artifact.input_approved",
                                grant["actor"], self._grant_binding(grant))
            if grant["target_epoch"] != state["epoch"]:
                raise Conflict("target epoch changed; explicit input approval is required again")
            row, body = self._candidate(db, tenant, run_id, grant["artifact_id"])
            if self._stale_reasons(db, row):
                raise Conflict("approved artifact input is stale; explicit approval is required again")
            if (grant["plan_digest"] != target["plan_digest"] or row["schema_version"] != selector["schema_version"]
                    or any(grant[key] != row[key] for key in ("source_task_id", "source_node", "kind", "source_epoch",
                                                             "revision", "content_hash", "plan_digest", "source_event_hash"))):
                raise Conflict("artifact input grant binding is inconsistent")
            items.append({**self._candidate_binding(row), "source_event_hash": row["source_event_hash"],
                          "grant_id": grant["id"], "target_epoch": grant["target_epoch"],
                          "approval_event_hash": grant["approval_event_hash"], "untrusted_data": True, "body": body})
        context = {"schema_version": "loom.artifact-context/v1", "tenant_id": tenant, "run_id": run_id,
                   "task_id": target_task_id, "target_epoch": state["epoch"],
                   "plan_digest": target["plan_digest"], "items": items,
                   "missing_inputs": missing, "ready": not any(item["required"] for item in missing),
                   "untrusted_data": True, "producer_verified": False, "counts_as_success": False,
                   "execution_connected": False, "max_bytes": MAX_CONTEXT_BYTES}
        return self._bounded_context(context, execution=execution)

    def _capture_execution_context_in(self, db, claim):
        self.store._assert_claim(db, claim)
        try:
            target = self._contract(db, claim.tenant_id, claim.run_id, claim.task_id)
            if not target.get("input_artifacts"):
                return None
            if "artifacts.read" not in target["tools"]:
                raise ExecutionContextError("CONTEXT_BINDING_INVALID")
            preview = self._preview_context_in(db, claim.tenant_id, claim.run_id, claim.task_id, execution=True)
            if not preview["ready"]:
                raise ExecutionContextError("CONTEXT_REQUIRED_INPUT_MISSING")
            context = {key: value for key, value in preview.items()
                       if key not in {"schema_version", "bytes", "context_digest", "execution_connected"}}
            context.update(schema_version="loom.execution-context/v1", worker_id=claim.worker_id)
            return self._bounded_context(context, execution=True)
        except ExecutionContextError:
            raise
        except (Conflict, InvalidInput, NotFound):
            raise ExecutionContextError("CONTEXT_BINDING_INVALID") from None

    def capture_execution_context(self, claim: Claim) -> dict | None:
        """Read an exact current snapshot; no persistence, dispatch or budgets."""
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self._capture_execution_context_in(db, claim)

    def assert_execution_context_in(self, db, claim: Claim, snapshot: dict | None):
        """Authority linearization point within a trusted caller's transaction."""
        current = self._capture_execution_context_in(db, claim)
        try:
            if snapshot is not None and type(snapshot) is not dict:
                raise ValueError
            supplied = canonical(snapshot)
            if len(supplied.encode("utf-8")) > MAX_CONTEXT_BYTES:
                raise ExecutionContextError("CONTEXT_TOO_LARGE")
            if supplied != canonical(current):
                raise ExecutionContextError("CONTEXT_SNAPSHOT_CHANGED")
        except (TypeError, ValueError, RecursionError, UnicodeError, OverflowError):
            raise ExecutionContextError("CONTEXT_BINDING_INVALID") from None
        return current

    def assert_execution_context(self, claim: Claim, snapshot: dict | None):
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self.assert_execution_context_in(db, claim, snapshot)

    def admit_execution_effect(self, claim: Claim, snapshot: dict | None, *, reservation_id: str,
                               max_cost: int, max_tokens: int, operation_id: str, request: dict) -> dict:
        """Validate inputs, reserve, mint intent and audit in one transaction.

        The event means local admission, not provider receipt or consumption.
        The trusted executor computes prompt_digest from its final messages.
        No transaction is held over external I/O; later invalidation must also
        be checked before accepting or submitting a returned candidate.
        """
        with self.store.transaction() as db:
            current = self.assert_execution_context_in(db, claim, snapshot)
            if current is None:
                return self.store._reserve_and_begin_effect(db, claim, reservation_id,
                    max_cost=max_cost, max_tokens=max_tokens, operation_id=operation_id, request=request)
            if type(request) is not dict:
                raise InvalidInput("model request metadata must be an object")
            _hash(request.get("prompt_digest"))
            if "context_digest" in request and request["context_digest"] != current["context_digest"]:
                raise ExecutionContextError("CONTEXT_SNAPSHOT_CHANGED")
            bound_request = {**request, "context_digest": current["context_digest"]}
            grant = self.store._reserve_and_begin_effect(db, claim, reservation_id,
                max_cost=max_cost, max_tokens=max_tokens, operation_id=operation_id, request=bound_request)
            if grant["dispatch"]:
                refs = [{key: item[key] for key in (
                    "grant_id", "approval_event_hash", "artifact_id", "source_node", "source_task_id",
                    "source_epoch", "target_epoch", "kind", "schema_version", "revision", "content_hash", "source_event_hash")}
                        for item in current["items"]]
                self.store._event(db, claim.tenant_id, claim.run_id, "execution.context_bound",
                    {"schema_version": "loom.context-bound/v1", "task_id": claim.task_id, "epoch": claim.epoch,
                     "worker_id": claim.worker_id, "plan_digest": current["plan_digest"],
                     "context_digest": current["context_digest"], "context_bytes": current["bytes"], "context_item_count": len(refs),
                     "refs": refs, "call_id": grant["call_id"], "reservation_id": reservation_id,
                     "prompt_digest": request["prompt_digest"], "untrusted_data": True,
                     "producer_verified": False, "counts_as_success": False}, "local-executor")
            return {**grant, "context_digest": current["context_digest"], "context_bytes": current["bytes"],
                    "context_item_count": len(current["items"])}

    @staticmethod
    def _snapshot_receipt_binding(snapshot):
        """Bound and copy caller data before hashing or consulting a receipt."""
        if type(snapshot) is not dict:
            raise ExecutionContextError("CONTEXT_BINDING_INVALID")
        nodes = 0

        def bounded(value, depth=0):
            nonlocal nodes
            nodes += 1
            if depth > 32 or nodes > MAX_CONTEXT_BYTES:
                raise ValueError
            if type(value) is dict:
                for key, child in value.items():
                    if type(key) is not str or len(key) > MAX_CONTEXT_BYTES:
                        raise ValueError
                    bounded(child, depth + 1)
            elif type(value) is list:
                for child in value:
                    bounded(child, depth + 1)
            elif type(value) is str:
                if len(value) > MAX_CONTEXT_BYTES:
                    raise ExecutionContextError("CONTEXT_TOO_LARGE")
            elif value is not None and type(value) not in {int, float, bool}:
                raise ValueError

        try:
            bounded(snapshot)
            raw = canonical(snapshot).encode("utf-8")
            if len(raw) > MAX_CONTEXT_BYTES:
                raise ExecutionContextError("CONTEXT_TOO_LARGE")
            # The copy used by apply is exactly the bytes in the receipt hash;
            # caller mutation cannot change the checked request afterwards.
            frozen = json.loads(raw)
            _hash(frozen["context_digest"])
            return frozen, sha256(raw).hexdigest()
        except (TypeError, ValueError, KeyError, InvalidInput, RecursionError, UnicodeError, OverflowError):
            raise ExecutionContextError("CONTEXT_BINDING_INVALID") from None

    def submit_execution_output(self, claim: Claim, key: str, output: dict, snapshot: dict | None) -> dict:
        """Input validation and standard submitted transition are indivisible."""
        if snapshot is None:
            # Still reject None for a task which declares auxiliary selectors;
            # _command's cached legacy receipt must not be a validation bypass.
            with self.store.transaction() as db:
                target = self._contract(db, claim.tenant_id, claim.run_id, claim.task_id)
                if target.get("input_artifacts"):
                    raise ExecutionContextError("CONTEXT_SNAPSHOT_CHANGED")
            return self.store.submit_output(claim, key, output)
        snapshot, snapshot_digest = self._snapshot_receipt_binding(snapshot)
        args = {"run_id": claim.run_id, "task_id": claim.task_id, "epoch": claim.epoch,
                "worker": claim.worker_id, "output": output,
                "execution_context_digest": snapshot["context_digest"],
                "execution_context_snapshot_digest": snapshot_digest}

        def apply(db):
            self.assert_execution_context_in(db, claim, snapshot)
            return self.store._submit_output(db, claim, key, output)

        return self.store._command(claim.tenant_id, key, "submit_execution_output", args, apply)
