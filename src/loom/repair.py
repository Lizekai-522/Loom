"""Trusted, atomic same-node Rebind. Public checks never authorize acceptance."""
from dataclasses import asdict
import json

from .failures import classify_failure
from .store import BudgetExceeded, Claim, Conflict, InvalidInput, Store, canonical, digest
from .workflow import WorkflowService


class RepairStopped(Conflict):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def public_cases(result):
    """Bound untrusted program output, retaining it only as public task data."""
    return [{"name": case.name, "passed": case.passed, "status": case.status,
             "exit_code": case.exit_code, "stdout": case.stdout[:512], "stderr": case.stderr[:512]}
            for case in result.cases[:32]]


class RebindService:
    ACTOR = "local-repair-coordinator"

    def __init__(self, store: Store):
        from .artifacts import ArtifactService
        self.store = store
        self.workflows = WorkflowService(store)
        self.artifacts = ArtifactService(store)

    def _assert_reviewed_suite_in(self, db, claim, spec):
        from .artifacts import ExecutionContextError
        from .development_suites import DevelopmentSuiteService
        from .store import NotFound
        current = self.artifacts._contract(db, claim.tenant_id, claim.run_id, claim.task_id)
        if current.get("development_suite") is None and spec.get("development_suite") is None:
            return
        try:
            if current.get("development_suite") != spec.get("development_suite"):
                raise Conflict("reviewed suite contract changed")
            DevelopmentSuiteService(self.store, artifacts=self.artifacts, initialize=False).resolve_reference_in(
                db, claim.tenant_id, claim.run_id, current["development_suite"])
        except (Conflict, InvalidInput, NotFound):
            raise ExecutionContextError("CONTEXT_SUITE_INVALID") from None

    def record_candidate(self, claim, spec, source, call_id, reservation_id):
        payload = {"task_id": claim.task_id, "epoch": claim.epoch, "plan_digest": spec["plan_digest"],
                   "development_digest": spec["repair"]["development_digest"],
                   "evaluation_digest": spec["evaluation_digest"], "source": source,
                   "source_sha256": digest({"main.py": source}), "model_call_id": call_id,
                   "reservation_id": reservation_id}
        with self.store.transaction() as db:
            self.store._assert_claim(db, claim, allow_paused=True)
            self._assert_reviewed_suite_in(db, claim, spec)
            return self.store._event(db, claim.tenant_id, claim.run_id, "execution.candidate", payload, "local-executor")

    def record_development(self, claim, spec, candidate, evaluated):
        payload = {"task_id": claim.task_id, "epoch": claim.epoch, "plan_digest": spec["plan_digest"],
                   "development_digest": evaluated.tests_sha256, "source_sha256": evaluated.source_sha256,
                   "candidate_event_hash": candidate["hash"], "passed": evaluated.passed,
                   "cases": public_cases(evaluated), "runner_sha256": evaluated.runner_sha256,
                   "image_id": evaluated.image_id}
        with self.store.transaction() as db:
            self.store._assert_claim(db, claim, allow_paused=True)
            self._assert_reviewed_suite_in(db, claim, spec)
            return self.store._event(db, claim.tenant_id, claim.run_id, "development.evaluated", payload, "local-executor")

    def _record(self, db, claim, event_hash, kind, actor):
        row = db.execute("SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND hash=?",
                         (claim.tenant_id, claim.run_id, event_hash)).fetchone()
        if row is None:
            raise Conflict("repair evidence is not in the current tenant/run")
        event = json.loads(row["envelope"])
        if (event.get("type") != kind or event.get("actor") != actor or digest(event) != row["hash"]
                or event.get("tenant_id") != claim.tenant_id or event.get("run_id") != claim.run_id):
            raise Conflict("repair evidence is not a trusted bound event")
        return event["payload"]

    def _context(self, db, claim, spec, candidate_hash, development_hash, from_epoch):
        candidate = self._record(db, claim, candidate_hash, "execution.candidate", "local-executor")
        development = self._record(db, claim, development_hash, "development.evaluated", "local-executor")
        for value in (candidate, development):
            if (value.get("task_id") != claim.task_id or value.get("epoch") != from_epoch
                    or value.get("plan_digest") != spec["plan_digest"]
                    or value.get("development_digest") != spec["repair"]["development_digest"]):
                raise Conflict("repair evidence differs from the frozen task contract")
        source = candidate.get("source")
        if (not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > 131072
                or candidate.get("source_sha256") != digest({"main.py": source})
                or candidate.get("evaluation_digest") != spec["evaluation_digest"]
                or development.get("candidate_event_hash") != candidate_hash
                or development.get("source_sha256") != candidate["source_sha256"]
                or development.get("passed") is not False):
            raise Conflict("repair evidence does not prove this public-development failure")
        cases = development.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 32 or not any(case.get("passed") is False for case in cases):
            raise Conflict("repair requires failed public development checks")
        return {"previous_source": source, "source_sha256": candidate["source_sha256"],
                "public_failures": [dict(case) for case in cases if case.get("passed") is False][:8]}, candidate

    def context_for(self, claim):
        spec = self.workflows.get_task_spec(claim.tenant_id, claim.run_id, claim.task_id)
        with self.store.connect() as db:
            for row in db.execute("SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='repair.applied' ORDER BY seq DESC",
                                  (claim.tenant_id, claim.run_id)):
                event = json.loads(row["envelope"])
                grant = event["payload"]
                if grant.get("task_id") != claim.task_id or grant.get("to_epoch") != claim.epoch:
                    continue
                if (event["actor"] != self.ACTOR or digest(event) != row["hash"]
                        or grant.get("plan_digest") != spec["plan_digest"]):
                    raise Conflict("repair context grant is invalid")
                context, _ = self._context(db, claim, spec, grant["candidate_event_hash"],
                                          grant["development_event_hash"], grant["from_epoch"])
                if digest(context) != grant.get("context_digest"):
                    raise Conflict("repair context does not match its recorded input version")
                return context
        return None

    def repeated_source(self, claim, plan_digest, source_sha256, candidate_event_hash):
        with self.store.connect() as db:
            for row in db.execute("SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='execution.candidate'",
                                  (claim.tenant_id, claim.run_id)):
                event = json.loads(row["envelope"])
                data = event["payload"]
                if (row["hash"] != candidate_event_hash and event["actor"] == "local-executor"
                        and data.get("task_id") == claim.task_id and data.get("plan_digest") == plan_digest
                        and data.get("source_sha256") == source_sha256):
                    return True
        return False

    def rebind(self, claim: Claim, candidate_event_hash: str, development_event_hash: str, *, max_cost: int, max_tokens: int):
        if type(max_cost) is not int or max_cost < 0 or type(max_tokens) is not int or max_tokens < 0:
            raise InvalidInput("invalid next-attempt reservation maximum")
        key = f"rebind:{claim.task_id}:{claim.epoch}"
        args = {"run_id": claim.run_id, "task_id": claim.task_id, "epoch": claim.epoch, "worker": claim.worker_id,
                "candidate_event_hash": candidate_event_hash, "development_event_hash": development_event_hash,
                "max_cost": max_cost, "max_tokens": max_tokens}

        def apply(db):
            row = self.store._assert_claim(db, claim, allow_paused=True)
            if self.store._run(db, claim.tenant_id, claim.run_id)["status"] != "open":
                raise RepairStopped("EXECUTION_RUN_PAUSED")
            spec = self.workflows.get_task_spec(claim.tenant_id, claim.run_id, claim.task_id)
            self._assert_reviewed_suite_in(db, claim, spec)
            if not spec.get("repair"):
                raise Conflict("the task has no approved repair policy")
            context, candidate = self._context(db, claim, spec, candidate_event_hash, development_event_hash, claim.epoch)
            if db.execute("SELECT 1 FROM effects WHERE tenant=? AND task_id=? AND state IN ('intent','indeterminate','partial')",
                          (claim.tenant_id, claim.task_id)).fetchone():
                raise RepairStopped("UNRESOLVED_CALL_REQUIRES_HUMAN")
            effect = db.execute("SELECT * FROM effects WHERE tenant=? AND run_id=? AND task_id=? AND epoch=? AND id=?",
                                (claim.tenant_id, claim.run_id, claim.task_id, claim.epoch, candidate["model_call_id"])).fetchone()
            expected_call = digest({"tenant_id": claim.tenant_id, "run_id": claim.run_id,
                                    "task_id": claim.task_id, "operation_id": f"model-generation:{claim.epoch}"})
            reservation_id = f"python:{claim.task_id}:{claim.epoch}"
            reservation = db.execute("SELECT * FROM reservations WHERE tenant=? AND run_id=? AND task_id=? AND epoch=? AND id=?",
                                     (claim.tenant_id, claim.run_id, claim.task_id, claim.epoch, reservation_id)).fetchone()
            if (effect is None or effect["state"] != "done" or effect["id"] != expected_call
                    or reservation is None or reservation["state"] != "settled"
                    or candidate["reservation_id"] != reservation_id):
                raise RepairStopped("UNRESOLVED_CALL_REQUIRES_HUMAN")
            try:
                returned = json.loads(json.loads(effect["result"])["content"])
            except (TypeError, ValueError, KeyError):
                raise Conflict("candidate does not have a valid known model result") from None
            if returned != {"source": context["previous_source"]}:
                raise Conflict("candidate differs from the known provider result")
            if db.execute("SELECT 1 FROM reservations WHERE tenant=? AND task_id=? AND state='reserved'",
                          (claim.tenant_id, claim.task_id)).fetchone():
                raise RepairStopped("UNRESOLVED_CALL_REQUIRES_HUMAN")
            applied = 0
            for old in db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='repair.applied'",
                                  (claim.tenant_id, claim.run_id)):
                data = json.loads(old[0])["payload"]
                applied += data.get("task_id") == claim.task_id and data.get("plan_digest") == spec["plan_digest"]
            if applied >= spec["repair"]["max_repairs"] or claim.epoch + 1 > spec["limits"]["max_attempts"]:
                raise RepairStopped("REPAIR_ATTEMPTS_EXHAUSTED")
            run = self.store._run(db, claim.tenant_id, claim.run_id)
            spent = db.execute("SELECT coalesce(sum(actual_cost),0),coalesce(sum(actual_tokens),0) FROM reservations WHERE tenant=? AND task_id=?",
                               (claim.tenant_id, claim.task_id)).fetchone()
            if (run["spent"] + run["reserved"] + max_cost > run["cost_limit"]
                    or run["tokens_spent"] + run["tokens_reserved"] + max_tokens > run["token_limit"]
                    or spent[0] + max_cost > spec["limits"]["cost_limit_microusd"]
                    or spent[1] + max_tokens > spec["limits"]["token_limit"]):
                raise BudgetExceeded("approved budget cannot admit another bounded repair")
            next_claim = Claim(claim.tenant_id, claim.run_id, claim.task_id, claim.worker_id,
                               claim.epoch + 1, self.store.now(db) + 90)
            db.execute("UPDATE tasks SET epoch=?,lease_until=? WHERE tenant=? AND run_id=? AND id=?",
                       (next_claim.epoch, next_claim.lease_until, claim.tenant_id, claim.run_id, claim.task_id))
            result = {"task_id": claim.task_id, "from_epoch": claim.epoch, "to_epoch": next_claim.epoch,
                      "primitive": "Rebind", "plan_digest": spec["plan_digest"], "policy": spec["repair"],
                      "candidate_event_hash": candidate_event_hash, "development_event_hash": development_event_hash,
                      "context_digest": digest(context), "claim": asdict(next_claim), "counts_as_success": False}
            classification = asdict(classify_failure("DEVELOPMENT_TESTS_FAILED", stage="public-development"))
            self.store._event(db, claim.tenant_id, claim.run_id, "failure.classified",
                              {**classification, "task_id": claim.task_id, "epoch": claim.epoch}, self.ACTOR)
            self.store._event(db, claim.tenant_id, claim.run_id, "repair.applied", result, self.ACTOR, key)
            self.store._event(db, claim.tenant_id, claim.run_id, "task.claimed",
                              {"task_id": claim.task_id, "worker": claim.worker_id, "epoch": next_claim.epoch,
                               "lease_until": next_claim.lease_until, "channel": row["channel"], "priority": row["priority"]}, self.ACTOR)
            return result

        return self.store._command(claim.tenant_id, key, "repair.rebind", args, apply)
