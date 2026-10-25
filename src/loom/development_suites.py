"""Append-only operator review records; execution/plan binding is separate.

Trusted callers must authenticate the named operator before freeze(). No HTTP
write or executor entry point is exposed here. A frozen suite neither verifies
its producer nor modifies an approved workflow or confers execution authority.
"""
from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from pydantic import ValidationError

from .artifact_schemas import DevelopmentSuiteReference
from .artifacts import ArtifactService, _hash, _subject, _text
from .public_checks import compile_public_checks
from .store import Conflict, InvalidInput, NotFound, Store, canonical, digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS development_suites (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, id TEXT NOT NULL,
 artifact_id TEXT NOT NULL, record TEXT NOT NULL, event_hash TEXT NOT NULL,
 PRIMARY KEY(tenant,id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id),
 FOREIGN KEY(tenant,artifact_id) REFERENCES artifact_candidates(tenant,id));
CREATE TRIGGER IF NOT EXISTS development_suites_no_update BEFORE UPDATE ON development_suites
 BEGIN SELECT RAISE(ABORT,'development suites are append-only'); END;
CREATE TRIGGER IF NOT EXISTS development_suites_no_delete BEFORE DELETE ON development_suites
 BEGIN SELECT RAISE(ABORT,'development suites are append-only'); END;
"""


class DevelopmentSuiteService:
    def __init__(self, store: Store, *, artifacts=None, initialize=True):
        self.store = store
        self.artifacts = ArtifactService(store) if artifacts is None else artifacts
        if initialize:
            with store.connect() as db:
                db.executescript(SCHEMA)

    @staticmethod
    def _selection(names):
        if names is not None and (
                type(names) is not list or not 1 <= len(names) <= 32
                or any(type(name) is not str or not 1 <= len(name) <= 48 for name in names)
                or len(names) != len(set(names))):
            raise InvalidInput("selected development cases must be unique bounded names")
        return None if names is None else list(names)

    def _preview_in(self, db, tenant, run_id, artifact_id, selection, *, allow_stale=False):
        row, body = self.artifacts._candidate(db, tenant, run_id, artifact_id)
        if row["kind"] != "public-checks":
            raise Conflict("only public-checks candidates can be reviewed as development suites")
        reasons = self.artifacts._stale_reasons(db, row)
        if self.store._run(db, tenant, run_id)["status"] != "open":
            reasons.append("source_run_not_open")
        if reasons and not allow_stale:
            raise Conflict("development suite source is no longer current")
        # Validate the whole original candidate before selecting a subset.
        original = compile_public_checks(body, expected_body_hash=row["content_hash"])
        selected = [case["name"] for case in body["cases"]] if selection is None else selection
        selected_set = set(selected)
        selected_cases = [case for case in body["cases"] if case["name"] in selected_set]
        if [case["name"] for case in selected_cases] != selected:
            raise InvalidInput("selected cases must exist in their original source order")
        selected_body = {**body, "cases": selected_cases}
        compiled = compile_public_checks(selected_body)
        binding = self.artifacts._candidate_binding(row)
        source = {
            "tenant_id": tenant, "run_id": run_id, "artifact_id": artifact_id,
            "task_id": row["source_task_id"], "node": row["source_node"],
            "epoch": row["source_epoch"], "revision": row["revision"],
            "plan_digest": row["plan_digest"], "schema_version": row["schema_version"],
            "body_hash": original.source_body_hash, "event_hash": row["source_event_hash"],
            "origin": binding["origin"],
        }
        if binding.get("model_source_recorded"):
            source["model_provenance_digest"] = digest(binding["model_provenance"])
        reviewed = {
            "schema_version": "loom.development-suite-review/v1", "source": source,
            "selected_case_names": selected, "case_count": len(selected),
            "selected_body_hash": compiled.source_body_hash,
            "compiler_version": compiled.compiler_version,
            "development_digest": compiled.development_digest,
            "compilation_digest": compiled.compilation_digest,
        }
        return {**reviewed, "review_digest": digest(reviewed)}, selected_body, reasons

    @staticmethod
    def _flags():
        return dict(visibility="development", execution_connected=False,
                    confers_authority=False, producer_verified=False,
                    independently_verified=False, counts_as_success=False)

    def preview(self, tenant, run_id, artifact_id, *, selected_case_names=None):
        """Read-only exact review, before an authenticated explicit freeze."""
        for value, label in ((tenant, "tenant"), (run_id, "run ID"), (artifact_id, "artifact ID")):
            _text(value, label)
        selection = self._selection(selected_case_names)
        with self.store.connect() as db:
            db.execute("BEGIN")
            reviewed, _, _ = self._preview_in(db, tenant, run_id, artifact_id, selection)
            return {**reviewed, **self._flags(), "frozen": False}

    def freeze(self, tenant, run_id, artifact_id, key, *, actor, expected_review_digest,
               selected_case_names=None):
        """Trusted operator boundary; adapter authentication remains mandatory."""
        for value, label in ((tenant, "tenant"), (run_id, "run ID"), (artifact_id, "artifact ID"),
                             (key, "idempotency key")):
            _text(value, label)
        _subject(actor)
        _hash(expected_review_digest)
        selection = self._selection(selected_case_names)
        args = dict(run_id=run_id, artifact_id=artifact_id, actor=actor,
                    expected_review_digest=expected_review_digest, selected_case_names=selection)

        def apply(db):
            reviewed, body, _ = self._preview_in(db, tenant, run_id, artifact_id, selection)
            if reviewed["review_digest"] != expected_review_digest:
                raise Conflict("freeze does not match the exact reviewed source and compilation")
            suite_id = str(uuid4())
            record = {**reviewed, "suite_id": suite_id, "actor": actor, "body": body}
            payload = {**reviewed, "suite_id": suite_id, "actor": actor,
                       "suite_digest": digest(record), **self._flags(), "frozen": True}
            event = self.store._event(db, tenant, run_id, "development.suite_frozen", payload, actor, key)
            db.execute("INSERT INTO development_suites(tenant,run_id,id,artifact_id,record,event_hash) VALUES (?,?,?,?,?,?)",
                       (tenant, run_id, suite_id, artifact_id, canonical(record), event["hash"]))
            return {**payload, "freeze_event_hash": event["hash"]}
        return self.store._command(tenant, key, "development.freeze_suite", args, apply)

    def get_suite(self, tenant, run_id, suite_id, *, include_body=False):
        if type(include_body) is not bool:
            raise InvalidInput("include_body must be boolean")
        for value, label in ((tenant, "tenant"), (run_id, "run ID"), (suite_id, "suite ID")):
            _text(value, label)
        with self.store.connect() as db:
            db.execute("BEGIN")
            result = self._suite_in(db, tenant, run_id, suite_id, include_body=include_body)
            if not result["stale"]:
                result["execution_reference"] = self.reference(result)
            return result

    def _suite_in(self, db, tenant, run_id, suite_id, *, include_body=False):
        row = db.execute("SELECT * FROM development_suites WHERE tenant=? AND run_id=? AND id=?",
                             (tenant, run_id, suite_id)).fetchone()
        if row is None:
            raise NotFound("development suite not found in tenant/run")
        try:
            record = json.loads(row["record"])
            actor = record["actor"]
            _subject(actor)
            selection = self._selection(record["selected_case_names"])
            reviewed, body, reasons = self._preview_in(
                db, tenant, run_id, row["artifact_id"], selection, allow_stale=True)
            expected = {**reviewed, "suite_id": suite_id, "actor": actor, "body": body}
            if canonical(record) != canonical(expected):
                raise ValueError
            payload = {**reviewed, "suite_id": suite_id, "actor": actor,
                       "suite_digest": digest(record), **self._flags(), "frozen": True}
            self.artifacts._event_matches(db, tenant, run_id, row["event_hash"],
                                         "development.suite_frozen", actor, payload)
        except (KeyError, TypeError, ValueError, InvalidInput, RecursionError, UnicodeError, OverflowError):
            raise Conflict("development suite record binding is inconsistent") from None
        result = {**payload, "freeze_event_hash": row["event_hash"],
                  "stale": bool(reasons), "stale_reasons": reasons}
        if include_body:
            result["body"] = json.loads(canonical(body))
        return result

    @staticmethod
    def reference(suite):
        """Build a typed pointer from a suite view, not a copied artifact."""
        try:
            raw = {"schema_version": "loom.development-suite-ref/v1",
                   "source_run_id": suite["source"]["run_id"],
                   **{key: suite[key] for key in ("suite_id", "suite_digest", "freeze_event_hash",
                       "review_digest", "compiler_version", "development_digest", "selected_body_hash")}}
            return DevelopmentSuiteReference.model_validate(raw).model_dump(mode="json")
        except (KeyError, TypeError, ValueError, ValidationError):
            raise InvalidInput("invalid development suite reference") from None

    def resolve_reference_in(self, db, tenant, target_run_id, reference):
        """Validate full origin and freeze event in the caller's snapshot."""
        try:
            raw = reference.model_dump(mode="json") if isinstance(reference, DevelopmentSuiteReference) else reference
            checked = DevelopmentSuiteReference.model_validate(raw).model_dump(mode="json")
        except (ValidationError, TypeError, ValueError):
            raise InvalidInput("invalid development suite reference") from None
        if checked["source_run_id"] == target_run_id:
            raise Conflict("reviewed suites require a separately approved execution run")
        try:
            suite = self._suite_in(db, tenant, checked["source_run_id"], checked["suite_id"], include_body=True)
        except sqlite3.OperationalError:
            raise Conflict("development suite registry is unavailable") from None
        if suite["stale"] or self.reference(suite) != checked:
            raise Conflict("development suite reference is stale or differs from the frozen review")
        return suite
