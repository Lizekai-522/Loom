"""Audited local operator controls; no task approval or model signatures.

Authenticated callers supply the actor. This service is a trusted adapter, not a
replacement for authentication, separation of duties, or provider billing proof.
Unknown effects and their reservations survive pause, cancellation and restart.
"""
from __future__ import annotations

import re

from .store import Conflict, InvalidInput, NotFound, Store, digest
from .workflow import WorkflowService


_UNKNOWN = ("intent", "indeterminate", "partial")
_CONTROLLABLE = {"open", "paused", "awaiting_human"}


def _text(value, maximum, label):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\x00" in value):
        raise InvalidInput(f"invalid {label}")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InvalidInput(f"invalid {label}") from None


def _usage(cost, tokens):
    if (type(cost) is not int or not 0 <= cost <= 1_000_000_000_000
            or type(tokens) is not int or not 0 <= tokens <= 1_000_000_000):
        raise InvalidInput("invalid manually reconciled usage")


class OperatorService:
    def __init__(self, store: Store):
        self.store = store
        self.workflows = WorkflowService(store)

    @staticmethod
    def _command_args(run_id, actor, reason):
        _text(run_id, 200, "run identifier")
        _text(actor, 200, "authenticated actor")
        _text(reason, 2000, "operator reason")
        return {"run_id": run_id, "actor": actor, "reason": reason}

    @staticmethod
    def _identity(tenant, key):
        _text(tenant, 200, "tenant")
        _text(key, 200, "idempotency key")

    def list_interventions(self, tenant: str) -> list[dict]:
        """Return tenant-owned summaries, never prompts, output, or effect bodies."""
        _text(tenant, 200, "tenant")
        with self.store.connect() as db:
            now = self.store.now(db)
            runs = db.execute("""SELECT * FROM runs r WHERE tenant=? AND (
                status IN ('paused','awaiting_human') OR EXISTS (
                    SELECT 1 FROM effects e WHERE e.tenant=r.tenant AND e.run_id=r.id
                    AND (e.state IN ('indeterminate','partial') OR (e.state='intent' AND NOT EXISTS (
                        SELECT 1 FROM tasks owner WHERE owner.tenant=e.tenant AND owner.id=e.task_id
                        AND owner.epoch=e.epoch AND owner.status='running' AND owner.lease_until>?)))) OR (
                    r.status NOT IN ('cancelled','succeeded') AND EXISTS (
                        SELECT 1 FROM tasks t WHERE t.tenant=r.tenant AND t.run_id=r.id
                        AND t.status='submitted'))) ORDER BY r.rowid DESC""", (tenant, now)).fetchall()
            result = []
            for run in runs:
                tasks = db.execute("SELECT status,count(*) AS n FROM tasks WHERE tenant=? AND run_id=? GROUP BY status",
                                   (tenant, run["id"])).fetchall()
                unknown = [dict(row) for row in db.execute(
                    "SELECT id AS call_id,task_id,epoch,state FROM effects WHERE tenant=? AND run_id=? AND state IN ('intent','indeterminate','partial') ORDER BY rowid",
                    (tenant, run["id"]))]
                counts = {row["status"]: row["n"] for row in tasks}
                result.append({"run_id": run["id"], "status": run["status"],
                               "task_counts": counts, "unknown_effects": unknown,
                               "awaiting_independent_verification": counts.get("submitted", 0),
                               "reserved_cost_microusd": run["reserved"],
                               "reserved_tokens": run["tokens_reserved"],
                               "counts_as_success": False})
            return result

    def pause(self, tenant: str, run_id: str, key: str, actor: str, reason: str) -> dict:
        self._identity(tenant, key)
        args = self._command_args(run_id, actor, reason)

        def apply(db):
            run = self.store._run(db, tenant, run_id)
            if run["status"] not in {"open", "awaiting_human"}:
                raise Conflict("run cannot be paused from its current state")
            db.execute("UPDATE runs SET status='paused' WHERE tenant=? AND id=?", (tenant, run_id))
            result = {**args, "status": "paused", "task_updates": [], "counts_as_success": False}
            self.store._event(db, tenant, run_id, "run.paused", result, actor, key)
            return result

        return self.store._command(tenant, key, "operator.pause", args, apply)

    def _task_limits(self, db, tenant, run_id, task):
        """Use approved execution fields, not mutable cached attempt counters."""
        if self.store._workflow_plan(db, tenant, run_id) is None:
            # The old fixture contract has no reviewed retry limit. A new Goal
            # is required rather than silently granting unlimited new attempts.
            return None
        spec = self.workflows.get_task_spec(tenant, run_id, task["id"])
        usage = db.execute("""SELECT coalesce(sum(actual_cost),0),coalesce(sum(actual_tokens),0)
                              FROM reservations WHERE tenant=? AND run_id=? AND task_id=?""",
                           (tenant, run_id, task["id"])).fetchone()
        if usage[0] > spec["limits"]["cost_limit_microusd"] or usage[1] > spec["limits"]["token_limit"]:
            raise Conflict("task usage exceeds its approved budget")
        return spec["limits"]

    def resume(self, tenant: str, run_id: str, key: str, actor: str, reason: str) -> dict:
        self._identity(tenant, key)
        args = self._command_args(run_id, actor, reason)

        def apply(db):
            run = self.store._run(db, tenant, run_id)
            if run["status"] not in {"paused", "awaiting_human"}:
                raise Conflict("only paused or awaiting-human runs can resume")
            plan = self.store._workflow_plan(db, tenant, run_id)
            if plan is not None and plan["plan_status"] != "approved":
                raise Conflict("workflow plan has not been approved")
            if db.execute("SELECT 1 FROM effects WHERE tenant=? AND run_id=? AND state IN ('intent','indeterminate','partial')",
                          (tenant, run_id)).fetchone():
                raise Conflict("unknown effects require reconciliation before resume")
            if db.execute("SELECT 1 FROM reservations WHERE tenant=? AND run_id=? AND state='reserved'", (tenant, run_id)).fetchone():
                raise Conflict("outstanding reservations require reconciliation before resume")
            if run["spent"] > run["cost_limit"] or run["tokens_spent"] > run["token_limit"]:
                raise Conflict("run usage exceeds its approved budget")
            now = self.store.now(db)
            tasks = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? ORDER BY rowid", (tenant, run_id)).fetchall()
            updates = []
            for task in tasks:
                limits = self._task_limits(db, tenant, run_id, task)
                retry = task["status"] in {"awaiting_human", "indeterminate"} or (
                    task["status"] == "running" and task["lease_until"] is not None and task["lease_until"] <= now)
                if not retry:
                    continue
                if task["lease_until"] is not None and task["lease_until"] > now:
                    raise Conflict("active task owner must stop before retry authorization")
                if limits is None:
                    raise Conflict("retry requires an approved task attempt contract")
                if task["epoch"] + 1 > limits["max_attempts"]:
                    raise Conflict("approved task attempts are exhausted")
                updates.append({"task_id": task["id"], "status": "pending", "epoch": task["epoch"]})
            # Validate all candidate retries first, then change every projection.
            for update in updates:
                db.execute("UPDATE tasks SET status='pending',worker=NULL,lease_until=NULL WHERE tenant=? AND id=?",
                           (tenant, update["task_id"]))
            db.execute("UPDATE runs SET status='open' WHERE tenant=? AND id=?", (tenant, run_id))
            result = {**args, "status": "open", "task_updates": updates, "counts_as_success": False}
            self.store._event(db, tenant, run_id, "run.resumed", result, actor, key)
            return result

        return self.store._command(tenant, key, "operator.resume", args, apply)

    def terminate(self, tenant: str, run_id: str, key: str, actor: str, reason: str) -> dict:
        self._identity(tenant, key)
        args = self._command_args(run_id, actor, reason)

        def apply(db):
            run = self.store._run(db, tenant, run_id)
            if run["status"] not in _CONTROLLABLE | {"draft"}:
                raise Conflict("run cannot be cancelled from its current state")
            for effect in db.execute("SELECT id FROM effects WHERE tenant=? AND run_id=? AND state='intent'", (tenant, run_id)).fetchall():
                db.execute("UPDATE effects SET state='indeterminate' WHERE tenant=? AND id=?", (tenant, effect["id"]))
                self.store._event(db, tenant, run_id, "tool.indeterminate",
                                  {"call_id": effect["id"], "state": "indeterminate", "reason": "OPERATOR_CANCELLED"}, actor)
            tasks = db.execute("SELECT id,epoch FROM tasks WHERE tenant=? AND run_id=? AND status NOT IN ('done','cancelled') ORDER BY rowid",
                               (tenant, run_id)).fetchall()
            updates = [{"task_id": task["id"], "status": "cancelled", "epoch": task["epoch"] + 1} for task in tasks]
            for update in updates:
                db.execute("UPDATE tasks SET status='cancelled',epoch=?,worker=NULL,lease_until=NULL WHERE tenant=? AND id=?",
                           (update["epoch"], tenant, update["task_id"]))
            db.execute("UPDATE runs SET status='cancelled' WHERE tenant=? AND id=?", (tenant, run_id))
            result = {**args, "status": "cancelled", "task_updates": updates, "counts_as_success": False}
            self.store._event(db, tenant, run_id, "run.cancelled", result, actor, key)
            return result

        return self.store._command(tenant, key, "operator.terminate", args, apply)

    def reconcile_effect(self, tenant: str, run_id: str, call_id: str, key: str, actor: str,
                         decision: str, evidence: dict, actual_cost: int, actual_tokens: int) -> dict:
        self._identity(tenant, key)
        _text(run_id, 200, "run identifier")
        _text(actor, 200, "authenticated actor")
        if not isinstance(call_id, str) or re.fullmatch(r"[0-9a-f]{64}", call_id) is None:
            raise InvalidInput("invalid effect identifier")
        if not isinstance(decision, str) or decision not in {"completed", "not_sent"}:
            raise InvalidInput("invalid reconciliation decision")
        if not isinstance(evidence, dict) or set(evidence) != {"reference", "summary"}:
            raise InvalidInput("invalid reconciliation evidence")
        _text(evidence["reference"], 500, "evidence reference")
        _text(evidence["summary"], 2000, "evidence summary")
        _usage(actual_cost, actual_tokens)
        if decision == "not_sent" and (actual_cost or actual_tokens):
            raise InvalidInput("not-sent evidence requires zero usage")
        args = {"run_id": run_id, "call_id": call_id, "actor": actor, "decision": decision,
                "evidence": dict(evidence), "actual_cost": actual_cost, "actual_tokens": actual_tokens}

        def apply(db):
            run = self.store._run(db, tenant, run_id)
            if run["status"] not in _CONTROLLABLE | {"cancelled"}:
                raise Conflict("run is not in a reconcilable state")
            effect = db.execute("SELECT * FROM effects WHERE tenant=? AND run_id=? AND id=?", (tenant, run_id, call_id)).fetchone()
            if effect is None:
                raise NotFound("effect not found in tenant/run")
            if effect["state"] not in _UNKNOWN:
                raise Conflict("effect is already known or reconciled")
            task = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?", (tenant, run_id, effect["task_id"])).fetchone()
            if (task["status"] == "running" and task["lease_until"] is not None
                    and task["lease_until"] > self.store.now(db)):
                raise Conflict("active task owner must stop before effect reconciliation")
            expected_call_id = digest({"tenant_id": tenant, "run_id": run_id, "task_id": effect["task_id"],
                                       "operation_id": f"model-generation:{effect['epoch']}"})
            if call_id != expected_call_id:
                raise Conflict("this effect has no supported reconciliation contract")
            reservation_id = f"python:{effect['task_id']}:{effect['epoch']}"
            reserved = db.execute("SELECT * FROM reservations WHERE tenant=? AND run_id=? AND task_id=? AND epoch=? AND id=?",
                                  (tenant, run_id, effect["task_id"], effect["epoch"], reservation_id)).fetchone()
            if reserved is None:
                raise Conflict("model reservation is unavailable for reconciliation")
            overrun = actual_cost > reserved["cost"] or actual_tokens > reserved["tokens"]
            budget_state = "overrun" if overrun else "settled"
            # Accounting evidence never resumes work. In particular a scheduler
            # may have expired a worker while leaving its run open; require a
            # separate audited resume after manual resolution. Respect an
            # explicit operator pause/cancellation even when usage overran.
            run_status = "awaiting_human" if run["status"] == "open" else run["status"]
            if reserved["state"] == "reserved":
                db.execute("UPDATE reservations SET state=?,actual_cost=?,actual_tokens=? WHERE tenant=? AND id=?",
                           (budget_state, actual_cost, actual_tokens, tenant, reservation_id))
                db.execute("UPDATE runs SET reserved=reserved-?,tokens_reserved=tokens_reserved-?,spent=spent+?,tokens_spent=tokens_spent+?,status=? WHERE tenant=? AND id=?",
                           (reserved["cost"], reserved["tokens"], actual_cost, actual_tokens, run_status, tenant, run_id))
                self.store._event(db, tenant, run_id, "budget." + budget_state,
                                  {"reservation_id": reservation_id, "state": budget_state,
                                   "actual_cost": actual_cost, "actual_tokens": actual_tokens, "run_status": run_status}, actor)
            elif (reserved["state"] not in {"settled", "overrun"}
                    or (reserved["actual_cost"], reserved["actual_tokens"]) != (actual_cost, actual_tokens)):
                raise Conflict("recorded usage cannot be rewritten during reconciliation")
            state = "not_sent" if decision == "not_sent" else "reconciled"
            result = {**args, "task_id": effect["task_id"], "epoch": effect["epoch"], "state": state,
                      "reservation_id": reservation_id, "budget_state": budget_state,
                      "run_status": run_status, "counts_as_success": False, "verified": False}
            # Preserve any partial provider body exactly. Human accounting is
            # appended as a separate immutable event, never installed as a
            # fabricated model response or as replacement execution evidence.
            db.execute("UPDATE effects SET state=? WHERE tenant=? AND id=?", (state, tenant, call_id))
            db.execute("UPDATE runs SET status=? WHERE tenant=? AND id=?", (run_status, tenant, run_id))
            self.store._event(db, tenant, run_id, "effect.reconciled", result, actor, key)
            return result

        return self.store._command(tenant, key, "operator.reconcile_effect", args, apply)
