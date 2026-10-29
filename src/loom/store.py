"""Durable single-host adapter. PostgreSQL/multi-host guarantees are separate work.

All commands serialize through BEGIN IMMEDIATE, atomically append full-envelope
events, receipts and outbox records. SQLite is a local reference implementation,
not an implementation of PostgreSQL RLS or a distributed scheduler.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
from uuid import uuid4

from .policy import ArtifactSubmission, GateRules, TaskContract, gate_p, gate_v


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


class LoomError(Exception):
    code = "LOOM_ERROR"


class NotFound(LoomError):
    code = "NOT_FOUND"


class Conflict(LoomError):
    code = "CONFLICT"


class PlanBindingConflict(Conflict):
    """The persisted scheduler projection no longer matches its approved plan."""


class StaleClaim(Conflict):
    code = "STALE_CLAIM"


class BudgetExceeded(Conflict):
    code = "BUDGET_EXCEEDED"


class InvalidInput(LoomError):
    code = "INVALID_INPUT"


@dataclass(frozen=True)
class Claim:
    tenant_id: str
    run_id: str
    task_id: str
    worker_id: str
    epoch: int
    lease_until: float


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 tenant TEXT NOT NULL, id TEXT NOT NULL, anchor TEXT NOT NULL,
 ruleset TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', revision INTEGER NOT NULL DEFAULT 1,
 cost_limit INTEGER NOT NULL CHECK(cost_limit>=0), token_limit INTEGER NOT NULL CHECK(token_limit>=0),
 spent INTEGER NOT NULL DEFAULT 0, tokens_spent INTEGER NOT NULL DEFAULT 0,
 reserved INTEGER NOT NULL DEFAULT 0, tokens_reserved INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,id));
CREATE TABLE IF NOT EXISTS receipts (
 tenant TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(tenant,key));
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
 envelope TEXT NOT NULL, hash TEXT NOT NULL,
 UNIQUE(tenant,run_id,seq), FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TABLE IF NOT EXISTS outbox (
 event_id TEXT PRIMARY KEY REFERENCES events(id), published INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS inbox (
 consumer TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(consumer,event_id));
CREATE TABLE IF NOT EXISTS tasks (
 tenant TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL, intent_id TEXT NOT NULL,
 title TEXT NOT NULL, depends TEXT NOT NULL, priority INTEGER NOT NULL,
 channel TEXT NOT NULL CHECK(channel IN ('business','verification')), status TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0, worker TEXT, lease_until REAL, output TEXT,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TABLE IF NOT EXISTS reservations (
 tenant TEXT NOT NULL,id TEXT NOT NULL,run_id TEXT NOT NULL,task_id TEXT NOT NULL,
 epoch INTEGER NOT NULL,cost INTEGER NOT NULL,tokens INTEGER NOT NULL,state TEXT NOT NULL,
 actual_cost INTEGER,actual_tokens INTEGER,
 PRIMARY KEY(tenant,id),FOREIGN KEY(tenant,task_id) REFERENCES tasks(tenant,id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TABLE IF NOT EXISTS effects (
 tenant TEXT NOT NULL,id TEXT NOT NULL,run_id TEXT NOT NULL,task_id TEXT NOT NULL,
 epoch INTEGER NOT NULL,request_hash TEXT NOT NULL,state TEXT NOT NULL,result TEXT,
 PRIMARY KEY(tenant,id),FOREIGN KEY(tenant,task_id) REFERENCES tasks(tenant,id),
 FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
"""


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    CRITERIA = frozenset({"schema.valid", "tests.pass", "report.traceable"})
    def __init__(self, path: str | Path, max_workers: int = 4, verify_reserve: int = 1):
        if not 1 <= verify_reserve < max_workers:
            raise ValueError("require 1 <= verify_reserve < max_workers")
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("use a file database so multiple connections share durable state")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.max_workers, self.verify_reserve = max_workers, verify_reserve
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        with self.transaction() as db:
            for key, value in (("max_workers", max_workers), ("verify_reserve", verify_reserve)):
                old = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                if old is not None and old[0] != value:
                    raise ValueError("all adapters sharing a database must use identical capacity settings")
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None, factory=_Connection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def now(db: sqlite3.Connection) -> float:
        return db.execute("SELECT (julianday('now')-2440587.5)*86400.0").fetchone()[0]

    def _run(self, db: sqlite3.Connection, tenant: str, run_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM runs WHERE tenant=? AND id=?", (tenant, run_id)).fetchone()
        if row is None:
            raise NotFound("run not found in tenant")
        return row

    @staticmethod
    def _workflow_plan(db: sqlite3.Connection, tenant: str, run_id: str):
        """Optional managed-plan projection; legacy runs keep their contract."""
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_goals'").fetchone():
            plan = db.execute("SELECT plan_status FROM workflow_goals WHERE tenant=? AND run_id=?", (tenant, run_id)).fetchone()
            if plan is not None:
                return plan
        # A lost managed projection must never silently enable the legacy task
        # API. The original event already identifies managed runs without a
        # migration or a new mutable flag.
        origin = db.execute("SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? AND seq=1", (tenant, run_id)).fetchone()
        try:
            if origin is None:
                raise ValueError
            envelope = json.loads(origin["envelope"])
            if (not isinstance(envelope, dict) or digest(envelope) != origin["hash"]
                    or not isinstance(envelope.get("payload"), dict)):
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            raise PlanBindingConflict("run origin evidence is missing or invalid") from None
        if envelope["payload"].get("managed_workflow") is True:
            raise PlanBindingConflict("managed workflow projection is missing")
        return None

    def _event(self, db: sqlite3.Connection, tenant: str, run_id: str, kind: str,
               payload: dict, actor: str, command_key: str | None = None) -> dict:
        self._run(db, tenant, run_id)
        prior = db.execute("SELECT seq,hash FROM events WHERE tenant=? AND run_id=? ORDER BY seq DESC LIMIT 1",
                           (tenant, run_id)).fetchone()
        envelope = {"schema_version": 1, "event_id": str(uuid4()), "tenant_id": tenant,
                    "run_id": run_id, "chain_id": run_id, "seq": prior["seq"] + 1 if prior else 1,
                    "ts": self.now(db), "type": kind, "actor": actor, "payload": payload,
                    "submission_key": command_key, "prev_hash": prior["hash"] if prior else "0" * 64}
        event_hash = digest(envelope)
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                   (envelope["event_id"], tenant, run_id, envelope["seq"], canonical(envelope), event_hash))
        db.execute("INSERT INTO outbox(event_id) VALUES (?)", (envelope["event_id"],))
        return {**envelope, "hash": event_hash}

    def _command(self, tenant: str, key: str, operation: str, args: dict,
                 apply: Callable[[sqlite3.Connection], dict]) -> dict:
        if not tenant or not key or len(key) > 200:
            raise InvalidInput("tenant and idempotency key required (key <= 200 characters)")
        request_hash = digest({"operation": operation, "args": args})
        with self.transaction() as db:
            old = db.execute("SELECT * FROM receipts WHERE tenant=? AND key=?", (tenant, key)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Conflict("idempotency key reused for different operation or arguments")
                return json.loads(old["result"])
            result = apply(db)
            db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (tenant, key, request_hash, canonical(result)))
            return result

    def create_run(self, tenant: str, key: str, *, acceptance: list[str], cost_limit: int = 1_000_000,
                   token_limit: int = 10000, ruleset: str = "kernel-v1") -> dict:
        if not acceptance or any(not isinstance(x, str) or not x.strip() for x in acceptance):
            raise InvalidInput("acceptance requires nonempty criterion IDs")
        if len(acceptance) != len(set(acceptance)) or not set(acceptance) <= self.CRITERIA:
            raise InvalidInput("acceptance must use unique registered local criterion IDs: " + ", ".join(sorted(self.CRITERIA)))
        if type(cost_limit) is not int or type(token_limit) is not int or cost_limit < 0 or token_limit < 0 or ruleset != "kernel-v1":
            raise InvalidInput("invalid budget or ruleset")
        args = dict(acceptance=acceptance, cost_limit=cost_limit, token_limit=token_limit, ruleset=ruleset)

        def apply(db):
            run_id = str(uuid4())
            goal_id, intent_id = str(uuid4()), str(uuid4())
            anchor = digest({"acceptance": acceptance})
            db.execute("INSERT INTO runs(tenant,id,anchor,ruleset,cost_limit,token_limit) VALUES (?,?,?,?,?,?)",
                       (tenant, run_id, anchor, ruleset, cost_limit, token_limit))
            result = {"run_id": run_id, "goal_id": goal_id, "intent_id": intent_id,
                      "anchor_digest": anchor, "ruleset": ruleset, "status": "open"}
            self._event(db, tenant, run_id, "run.created", {**result, **args}, "human:local", key)
            self._event(db, tenant, run_id, "goal.created", {"goal_id": goal_id, "anchor": anchor}, "system")
            self._event(db, tenant, run_id, "intent.created", {"intent_id": intent_id, "goal_id": goal_id,
                        "from_facts": [{"kind": "anchor", "id": anchor}]}, "system")
            return result
        return self._command(tenant, key, "create_run", args, apply)

    def add_task(self, tenant: str, run_id: str, key: str, *, task_id: str, title: str,
                 depends_on: list[str] | None = None, priority: int = 0, channel: str = "business") -> dict:
        depends = depends_on or []
        if not task_id or not title.strip() or channel not in {"business", "verification"}:
            raise InvalidInput("task ID, title and valid channel required")
        if task_id in depends or len(depends) != len(set(depends)):
            raise InvalidInput("invalid dependency list")
        args = dict(run_id=run_id, task_id=task_id, title=title, depends_on=depends, priority=priority, channel=channel)

        def apply(db):
            run = self._run(db, tenant, run_id)
            if self._workflow_plan(db, tenant, run_id) is not None:
                raise Conflict("managed tasks are created only by atomic plan approval")
            if run["status"] != "open":
                raise Conflict("run is not open")
            for dependency in depends:
                row = db.execute("SELECT run_id FROM tasks WHERE tenant=? AND id=?", (tenant, dependency)).fetchone()
                if row is None or row["run_id"] != run_id:
                    raise InvalidInput("dependency must already exist in the same tenant and run")
            intent_event = db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='intent.created' ORDER BY seq LIMIT 1",
                                      (tenant, run_id)).fetchone()
            intent_id = json.loads(intent_event[0])["payload"]["intent_id"]
            root_event = db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? AND seq=1", (tenant, run_id)).fetchone()
            root = json.loads(root_event[0])["payload"]
            contract = TaskContract(task_id, run_id, ("local-fixture",), root["goal_id"], tuple(root["acceptance"]), run["anchor"])
            admission = gate_p(contract, rules=GateRules(version=run["ruleset"]), allowed_scope=frozenset({"local-fixture"}),
                               known_anchors=frozenset({run["anchor"]}), known_criteria=self.CRITERIA)
            if not admission.passed:
                raise InvalidInput("Gate-P: " + admission.reason)
            try:
                db.execute("INSERT INTO tasks(tenant,id,run_id,intent_id,title,depends,priority,channel,status) VALUES (?,?,?,?,?,?,?,?,?)",
                           (tenant, task_id, run_id, intent_id, title, canonical(depends), priority, channel, "pending"))
            except sqlite3.IntegrityError as exc:
                raise Conflict("task ID already exists in tenant") from exc
            payload = {**args, "intent_id": intent_id, "anchor_digest": run["anchor"], "status": "pending"}
            self._event(db, tenant, run_id, "task.created", payload, "system", key)
            return payload
        return self._command(tenant, key, "add_task", args, apply)

    def _expire(self, db: sqlite3.Connection) -> None:
        now = self.now(db)
        for task in db.execute("SELECT * FROM tasks WHERE status='running' AND lease_until<=?", (now,)).fetchall():
            uncertain = db.execute("SELECT 1 FROM effects WHERE tenant=? AND task_id=? AND state IN ('intent','indeterminate','partial')",
                                   (task["tenant"], task["id"])).fetchone()
            unsettled = db.execute("SELECT 1 FROM reservations WHERE tenant=? AND run_id=? AND task_id=? AND state='reserved'",
                                   (task["tenant"], task["run_id"], task["id"])).fetchone()
            state = "indeterminate" if uncertain or unsettled else "pending"
            db.execute("UPDATE tasks SET status=?,worker=NULL,lease_until=NULL WHERE tenant=? AND id=?",
                       (state, task["tenant"], task["id"]))
            if uncertain:
                effects = db.execute("SELECT id FROM effects WHERE tenant=? AND task_id=? AND state='intent'",
                                     (task["tenant"], task["id"])).fetchall()
                db.execute("UPDATE effects SET state='indeterminate' WHERE tenant=? AND task_id=? AND state='intent'",
                           (task["tenant"], task["id"]))
                for effect in effects:
                    self._event(db, task["tenant"], task["run_id"], "tool.indeterminate", {"call_id": effect["id"], "state": "indeterminate"}, "system")
            self._event(db, task["tenant"], task["run_id"], "task.lease_expired", {"task_id": task["id"], "status": state}, "system")

    def claim_next(self, tenant: str, run_id: str, worker: str, *, channel: str = "business", lease_seconds: float = 30) -> Claim | None:
        if not worker or channel not in {"business", "verification"} or not 0 < lease_seconds <= 3600:
            raise InvalidInput("invalid claim parameters")
        with self.transaction() as db:
            run = self._run(db, tenant, run_id)
            if run["status"] != "open":
                raise Conflict("run is not open")
            managed = self._workflow_plan(db, tenant, run_id)
            if managed is not None and managed["plan_status"] != "approved":
                raise Conflict("workflow plan has not been approved")
            self._expire(db)
            total = db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
            business = db.execute("SELECT count(*) FROM tasks WHERE status='running' AND channel='business'").fetchone()[0]
            if total >= self.max_workers or (channel == "business" and business >= self.max_workers - self.verify_reserve):
                return None
            rows = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? AND channel=? AND status='pending' ORDER BY priority DESC,rowid ASC",
                              (tenant, run_id, channel)).fetchall()
            if managed is not None:
                from .workflow import WorkflowService
                for row in rows:
                    WorkflowService._task_spec_in(db, self, tenant, run_id, row["id"])
            done = {r[0] for r in db.execute("SELECT id FROM tasks WHERE tenant=? AND run_id=? AND status='done'", (tenant, run_id))}
            task = next((r for r in rows if set(json.loads(r["depends"])) <= done), None)
            if task is None:
                return None
            epoch, until = task["epoch"] + 1, self.now(db) + lease_seconds
            db.execute("UPDATE tasks SET status='running',worker=?,epoch=?,lease_until=? WHERE tenant=? AND id=?",
                       (worker, epoch, until, tenant, task["id"]))
            claim = Claim(tenant, run_id, task["id"], worker, epoch, until)
            self._event(db, tenant, run_id, "task.claimed", {"task_id": task["id"], "worker": worker, "epoch": epoch,
                        "lease_until": until, "channel": channel, "priority": task["priority"]}, "system")
            return claim

    def _assert_claim(self, db: sqlite3.Connection, claim: Claim, *, allow_paused: bool = False) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?",
                         (claim.tenant_id, claim.run_id, claim.task_id)).fetchone()
        if row is None:
            raise NotFound("task not found in tenant/run")
        if (row["status"] != "running" or row["worker"] != claim.worker_id or row["epoch"] != claim.epoch
                or row["lease_until"] is None or row["lease_until"] <= self.now(db)):
            raise StaleClaim("claim expired, replaced or invalidated")
        if self._workflow_plan(db, claim.tenant_id, claim.run_id) is not None:
            from .workflow import WorkflowService
            WorkflowService._task_spec_in(db, self, claim.tenant_id, claim.run_id, claim.task_id)
        if not allow_paused and self._run(db, claim.tenant_id, claim.run_id)["status"] != "open":
            raise Conflict("run is paused; no new execution or verified completion allowed")
        return row

    def submit_output(self, claim: Claim, key: str, output: dict) -> dict:
        """Untrusted worker submission releases capacity but never opens dependencies."""
        args = {"run_id": claim.run_id, "task_id": claim.task_id, "epoch": claim.epoch, "worker": claim.worker_id, "output": output}
        def apply(db):
            return self._submit_output(db, claim, key, output)
        return self._command(claim.tenant_id, key, "submit_output", args, apply)

    def _submit_output(self, db, claim: Claim, key: str, output: dict) -> dict:
        """Standard submission within a trusted caller's authority transaction."""
        self._assert_claim(db, claim)
        unknown = db.execute("SELECT 1 FROM effects WHERE tenant=? AND task_id=? AND state IN ('intent','indeterminate','partial')",
                             (claim.tenant_id, claim.task_id)).fetchone()
        if unknown:
            raise Conflict("resolve pending external effects before submitting output")
        db.execute("UPDATE tasks SET status='submitted',output=?,lease_until=NULL WHERE tenant=? AND id=?",
                   (canonical(output), claim.tenant_id, claim.task_id))
        result = {"task_id": claim.task_id, "status": "submitted", "content_hash": digest(output)}
        self._event(db, claim.tenant_id, claim.run_id, "task.output_submitted", {**result, "output": output}, "worker:" + claim.worker_id, key)
        return result

    def renew_claim(self, claim: Claim, *, lease_seconds: float = 90) -> Claim:
        """A live owner may renew its own epoch; expired owners cannot revive it."""
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not 0 < lease_seconds <= 3600:
            raise InvalidInput("invalid renewal lease")
        with self.transaction() as db:
            self._assert_claim(db, claim)
            until = max(self.now(db) + lease_seconds,
                        db.execute("SELECT lease_until FROM tasks WHERE tenant=? AND id=?", (claim.tenant_id, claim.task_id)).fetchone()[0])
            db.execute("UPDATE tasks SET lease_until=? WHERE tenant=? AND run_id=? AND id=? AND epoch=? AND worker=?",
                       (until, claim.tenant_id, claim.run_id, claim.task_id, claim.epoch, claim.worker_id))
            self._event(db, claim.tenant_id, claim.run_id, "task.lease_renewed",
                        {"task_id": claim.task_id, "epoch": claim.epoch, "lease_until": until}, "worker:" + claim.worker_id)
            return Claim(claim.tenant_id, claim.run_id, claim.task_id, claim.worker_id, claim.epoch, until)

    def complete(self, claim: Claim, key: str, output: dict, *, verification: ArtifactSubmission) -> dict:
        """Trusted verification-engine entry; never exposed as a worker HTTP endpoint.

        The caller must authenticate signatures and obtain evidence from a
        trusted executor. Policy flags are an adapter boundary, not cryptography.
        """
        args = {"run_id": claim.run_id, "task_id": claim.task_id, "epoch": claim.epoch,
                "worker": claim.worker_id, "output": output, "verification": asdict(verification)}
        def apply(db):
            row = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?", (claim.tenant_id, claim.run_id, claim.task_id)).fetchone()
            if row is not None and row["status"] == "submitted":
                if row["epoch"] != claim.epoch or row["worker"] != claim.worker_id or row["output"] != canonical(output):
                    raise StaleClaim("submitted artifact has different epoch, owner or content")
            else:
                self._assert_claim(db, claim)
            run = self._run(db, claim.tenant_id, claim.run_id)
            if run["status"] != "open":
                raise Conflict("run is paused")
            if verification.content_hash != digest(output) or verification.evidence_revision != f"{claim.task_id}:{claim.epoch}":
                raise Conflict("verification is not bound to task content and attempt")
            gate = gate_v(verification, rules=GateRules(version=run["ruleset"]))
            if not gate.passed:
                result = {"task_id": claim.task_id, "status": "pending", "verification": "rejected", "reason": gate.reason}
                db.execute("UPDATE tasks SET status='pending',worker=NULL,lease_until=NULL,output=NULL WHERE tenant=? AND id=?", (claim.tenant_id, claim.task_id))
                self._event(db, claim.tenant_id, claim.run_id, "gate.failed", {"gate": "V", "task_id": claim.task_id, "reason": gate.reason,
                            "content_hash": verification.content_hash}, "verification-engine", key)
                self._event(db, claim.tenant_id, claim.run_id, "task.verification_failed", result, "verification-engine")
                return result
            unknown = db.execute("SELECT 1 FROM effects WHERE tenant=? AND task_id=? AND state IN ('intent','indeterminate','partial')",
                                 (claim.tenant_id, claim.task_id)).fetchone()
            if unknown:
                raise Conflict("unresolved external effect prevents completion")
            db.execute("UPDATE tasks SET status='done',output=?,worker=NULL,lease_until=NULL WHERE tenant=? AND id=?",
                       (canonical(output), claim.tenant_id, claim.task_id))
            result = {"task_id": claim.task_id, "status": "done", "output": output}
            self._event(db, claim.tenant_id, claim.run_id, "artifact.verified", {"task_id": claim.task_id, "content_hash": verification.content_hash,
                        "ruleset": verification.ruleset_version, "evidence_revision": verification.evidence_revision, "dual_sign": verification.dual_sign,
                        "verification": asdict(verification)}, "verification-engine")
            self._event(db, claim.tenant_id, claim.run_id, "task.completed", result, "worker:" + claim.worker_id, key)
            return result
        return self._command(claim.tenant_id, key, "complete_task", args, apply)

    def reserve(self, claim: Claim, reservation_id: str, *, max_cost: int, max_tokens: int) -> dict:
        with self.transaction() as db:
            return self._reserve(db, claim, reservation_id, max_cost=max_cost, max_tokens=max_tokens)

    def _reserve(self, db, claim: Claim, reservation_id: str, *, max_cost: int, max_tokens: int) -> dict:
        if not reservation_id or min(max_cost, max_tokens) < 0:
            raise InvalidInput("reservation ID and nonnegative maximums required")
        self._assert_claim(db, claim, allow_paused=True)
        run = self._run(db, claim.tenant_id, claim.run_id)
        old = db.execute("SELECT * FROM reservations WHERE tenant=? AND id=?", (claim.tenant_id, reservation_id)).fetchone()
        if old:
            if (old["task_id"], old["run_id"], old["epoch"], old["cost"], old["tokens"]) != (claim.task_id, claim.run_id, claim.epoch, max_cost, max_tokens):
                raise Conflict("reservation ID reused")
            return dict(old)
        if self._workflow_plan(db, claim.tenant_id, claim.run_id) is not None:
            contract = db.execute("SELECT contract FROM workflow_tasks WHERE tenant=? AND run_id=? AND task_id=?",
                                  (claim.tenant_id, claim.run_id, claim.task_id)).fetchone()
            if contract is None:
                raise Conflict("managed task has no frozen execution contract")
            limits = json.loads(contract[0])["limits"]
            used = db.execute("""SELECT
                coalesce(sum(CASE WHEN state='reserved' THEN cost ELSE actual_cost END),0),
                coalesce(sum(CASE WHEN state='reserved' THEN tokens ELSE actual_tokens END),0)
                FROM reservations WHERE tenant=? AND run_id=? AND task_id=?""",
                (claim.tenant_id, claim.run_id, claim.task_id)).fetchone()
            if (used[0] + max_cost > limits["cost_limit_microusd"]
                    or used[1] + max_tokens > limits["token_limit"]):
                raise BudgetExceeded("logical task has insufficient budget across attempts")
        if run["status"] != "open" or run["spent"] + run["reserved"] + max_cost > run["cost_limit"] or run["tokens_spent"] + run["tokens_reserved"] + max_tokens > run["token_limit"]:
            raise BudgetExceeded("run has insufficient unreserved budget")
        db.execute("UPDATE runs SET reserved=reserved+?,tokens_reserved=tokens_reserved+? WHERE tenant=? AND id=?",
                   (max_cost, max_tokens, claim.tenant_id, claim.run_id))
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (claim.tenant_id, reservation_id, claim.run_id, claim.task_id, claim.epoch, max_cost, max_tokens, "reserved", None, None))
        result = {"reservation_id": reservation_id, "cost": max_cost, "tokens": max_tokens, "state": "reserved"}
        self._event(db, claim.tenant_id, claim.run_id, "budget.reserved", result, "gateway")
        return result

    def settle(self, tenant: str, reservation_id: str, *, actual_cost: int, actual_tokens: int) -> dict:
        if min(actual_cost, actual_tokens) < 0:
            raise InvalidInput("negative usage")
        with self.transaction() as db:
            r = db.execute("SELECT * FROM reservations WHERE tenant=? AND id=?", (tenant, reservation_id)).fetchone()
            if r is None:
                raise NotFound("reservation not found")
            if r["state"] != "reserved":
                if (actual_cost, actual_tokens) != (r["actual_cost"], r["actual_tokens"]):
                    raise Conflict("settlement already recorded with different usage")
                return {"reservation_id": reservation_id, "state": r["state"]}
            overrun = actual_cost > r["cost"] or actual_tokens > r["tokens"]
            state = "overrun" if overrun else "settled"
            db.execute("UPDATE reservations SET state=?,actual_cost=?,actual_tokens=? WHERE tenant=? AND id=?",
                       (state, actual_cost, actual_tokens, tenant, reservation_id))
            db.execute("UPDATE runs SET reserved=reserved-?,tokens_reserved=tokens_reserved-?,spent=spent+?,tokens_spent=tokens_spent+?,status=CASE WHEN ? AND status NOT IN ('cancelled','succeeded') THEN 'awaiting_human' ELSE status END WHERE tenant=? AND id=?",
                       (r["cost"], r["tokens"], actual_cost, actual_tokens, overrun, tenant, r["run_id"]))
            result = {"reservation_id": reservation_id, "state": state, "actual_cost": actual_cost, "actual_tokens": actual_tokens}
            self._event(db, tenant, r["run_id"], "budget." + state,
                        {**result, "run_status": self._run(db, tenant, r["run_id"])["status"]}, "gateway")
            return result

    def begin_effect(self, claim: Claim, operation_id: str, request: dict) -> dict:
        """Trusted intent ledger. Only the first stable operation may dispatch."""
        with self.transaction() as db:
            return self._begin_effect(db, claim, operation_id, request)

    def _begin_effect(self, db, claim: Claim, operation_id: str, request: dict) -> dict:
        if not operation_id:
            raise InvalidInput("stable operation ID required")
        call_id = digest({"tenant_id": claim.tenant_id, "run_id": claim.run_id, "task_id": claim.task_id, "operation_id": operation_id})
        self._assert_claim(db, claim)
        old = db.execute("SELECT * FROM effects WHERE tenant=? AND id=?", (claim.tenant_id, call_id)).fetchone()
        if old:
            if old["request_hash"] != digest(request) or old["run_id"] != claim.run_id:
                raise Conflict("logical operation reused with different request")
            return {"call_id": call_id, "state": old["state"], "dispatch": False}
        db.execute("INSERT INTO effects VALUES (?,?,?,?,?,?,?,?)",
                   (claim.tenant_id, call_id, claim.run_id, claim.task_id, claim.epoch, digest(request), "intent", None))
        result = {"call_id": call_id, "task_id": claim.task_id, "request_hash": digest(request), "state": "intent"}
        self._event(db, claim.tenant_id, claim.run_id, "tool.intent", result, "gateway")
        return {**result, "dispatch": True}

    def reserve_and_begin_effect(self, claim: Claim, reservation_id: str, *, max_cost: int,
                                 max_tokens: int, operation_id: str, request: dict) -> dict:
        """Atomically reserve budget and record intent before any dispatch.

        Pause/termination can occur before or after this transaction, never
        leave a committed new reservation without its corresponding intent.
        This operation does not perform network I/O or retry an existing grant.
        """
        with self.transaction() as db:
            return self._reserve_and_begin_effect(db, claim, reservation_id, max_cost=max_cost,
                max_tokens=max_tokens, operation_id=operation_id, request=request)

    def _reserve_and_begin_effect(self, db, claim: Claim, reservation_id: str, *, max_cost: int,
                                  max_tokens: int, operation_id: str, request: dict) -> dict:
        self._assert_claim(db, claim)
        old = db.execute("SELECT 1 FROM reservations WHERE tenant=? AND id=?",
                         (claim.tenant_id, reservation_id)).fetchone()
        reserved = self._reserve(db, claim, reservation_id, max_cost=max_cost, max_tokens=max_tokens)
        grant = self._begin_effect(db, claim, operation_id, request)
        if not grant["dispatch"] and old is None:
            raise Conflict("existing effect cannot acquire a different reservation")
        if grant["dispatch"] and reserved["state"] != "reserved":
            raise Conflict("settled budget cannot authorize a new effect")
        if grant['dispatch'] and operation_id.startswith('model-generation:'):
            from .quotas import QuotaService
            QuotaService(self).admit_model_in(db,claim,grant['call_id'],request)
        return grant

    def finish_effect(self, claim: Claim, call_id: str, result: dict) -> dict:
        with self.transaction() as db:
            self._assert_claim(db, claim, allow_paused=True)
            row = db.execute("SELECT * FROM effects WHERE tenant=? AND id=? AND task_id=? AND epoch=?",
                             (claim.tenant_id, call_id, claim.task_id, claim.epoch)).fetchone()
            if row is None:
                raise NotFound("effect not found for claim")
            if row["state"] == "done" and row["result"] == canonical(result):
                return {"call_id": call_id, "state": "done"}
            if row["state"] != "intent":
                raise Conflict("effect cannot be automatically resolved")
            db.execute("UPDATE effects SET state='done',result=? WHERE tenant=? AND id=?", (canonical(result), claim.tenant_id, call_id))
            payload = {"call_id": call_id, "state": "done", "result": result}
            self._event(db, claim.tenant_id, claim.run_id, "tool.done", payload, "gateway")
            return payload

    def invalidate(self, tenant: str, run_id: str, task_id: str, reason: str) -> list[str]:
        if not reason.strip():
            raise InvalidInput("invalidation requires reason")
        with self.transaction() as db:
            self._run(db, tenant, run_id)
            tasks = db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=?", (tenant, run_id)).fetchall()
            if not any(t["id"] == task_id for t in tasks):
                raise NotFound("task not found")
            stale = {task_id}
            while True:
                more = {t["id"] for t in tasks if stale.intersection(json.loads(t["depends"]))}
                if more <= stale:
                    break
                stale |= more
            db.execute("UPDATE runs SET revision=revision+1 WHERE tenant=? AND id=?", (tenant, run_id))
            for current in sorted(stale):
                db.execute("UPDATE tasks SET status='stale',epoch=epoch+1,worker=NULL,lease_until=NULL WHERE tenant=? AND id=?", (tenant, current))
                epoch = db.execute("SELECT epoch FROM tasks WHERE tenant=? AND id=?", (tenant, current)).fetchone()[0]
                self._event(db, tenant, run_id, "task.invalidated", {"task_id": current, "status": "stale", "epoch": epoch, "reason": reason}, "human:local")
                for effect in db.execute("SELECT id,state FROM effects WHERE tenant=? AND task_id=?", (tenant, current)).fetchall():
                    self._event(db, tenant, run_id, "tool.compensation_required", {"call_id": effect["id"], "external_state": effect["state"], "reason": reason}, "system")
            return sorted(stale)

    def record_repair(self, tenant: str, run_id: str, task_id: str, *, primitive: str, reason: str) -> None:
        if primitive not in {"Rebind", "Substitute"} or not reason:
            raise InvalidInput("local milestone supports Rebind/Substitute only, with a reason")
        with self.transaction() as db:
            self._run(db, tenant, run_id)
            task = db.execute("SELECT status FROM tasks WHERE tenant=? AND run_id=? AND id=?", (tenant, run_id, task_id)).fetchone()
            if task is None or task["status"] != "pending":
                raise Conflict("repair requires a pending task after verification failure")
            self._event(db, tenant, run_id, "failure.classified", {"task_id": task_id, "family": "other", "detail": "assertion_failure"}, "fixture-planner")
            self._event(db, tenant, run_id, "repair.applied", {"task_id": task_id, "primitive": primitive, "reason": reason}, "fixture-planner")

    def finish_run(self, tenant: str, run_id: str, key: str, criterion_results: dict[str, bool]) -> dict:
        """Trusted acceptance-engine entry, not exposed to worker HTTP clients."""
        def apply(db):
            run = self._run(db, tenant, run_id)
            if run["status"] != "open":
                raise Conflict("run is not open")
            root = json.loads(db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? AND seq=1", (tenant, run_id)).fetchone()[0])["payload"]
            tasks = db.execute("SELECT status FROM tasks WHERE tenant=? AND run_id=?", (tenant, run_id)).fetchall()
            if not tasks or any(t["status"] != "done" for t in tasks):
                raise Conflict("all tasks must pass verification before goal acceptance")
            if set(criterion_results) != set(root["acceptance"]) or any(v is not True for v in criterion_results.values()):
                raise Conflict("frozen goal acceptance is not satisfied")
            if run["reserved"] or run["tokens_reserved"]:
                raise Conflict("outstanding billing reservations require reconciliation")
            db.execute("UPDATE runs SET status='succeeded' WHERE tenant=? AND id=?", (tenant, run_id))
            result = {"run_id": run_id, "status": "succeeded", "criteria": criterion_results, "verified_tasks": len(tasks)}
            self._event(db, tenant, run_id, "goal.achieved", result, "acceptance-engine", key)
            self._event(db, tenant, run_id, "run.completed", result, "system")
            return result
        return self._command(tenant, key, "finish_run", {"run_id": run_id, "criteria": criterion_results}, apply)

    def events(self, tenant: str, run_id: str) -> list[dict]:
        with self.connect() as db:
            self._run(db, tenant, run_id)
            return [{**json.loads(r["envelope"]), "hash": r["hash"]} for r in db.execute("SELECT envelope,hash FROM events WHERE tenant=? AND run_id=? ORDER BY seq", (tenant, run_id))]

    @staticmethod
    def verify_events(events: list[dict], expected_head: str | None = None, expected_count: int | None = None) -> bool:
        prev = "0" * 64
        chain = None
        for seq, event in enumerate(events, 1):
            body = {k: v for k, v in event.items() if k != "hash"}
            identity = (body["tenant_id"], body["run_id"], body["chain_id"])
            if chain is None:
                chain = identity
            if identity != chain or body["seq"] != seq or body["prev_hash"] != prev or digest(body) != event["hash"]:
                return False
            prev = event["hash"]
        return (expected_head is None or expected_head == prev) and (expected_count is None or expected_count == len(events))

    def replay(self, tenant: str, run_id: str) -> dict:
        events = self.events(tenant, run_id)
        if not self.verify_events(events):
            raise Conflict("event integrity check failed")
        state: dict = {"run_id": run_id, "tasks": {}, "effects": {}, "reservations": {}, "last_seq": 0,
                       "spent": 0, "reserved": 0, "tokens_spent": 0, "tokens_reserved": 0, "revision": 1}
        for event in events:
            payload, kind = event["payload"], event["type"]
            if kind == "run.created":
                state.update({"status": payload.get("status", "open"), "anchor_digest": payload["anchor_digest"], "ruleset": payload["ruleset"]})
            elif kind == "workflow.plan_approved":
                state["status"] = "open"
            elif kind == "execution.blocked":
                state["status"] = payload.get("run_status", "awaiting_human")
                state["tasks"][payload["task_id"]]["status"] = "awaiting_human"
            elif kind in {"run.paused", "run.resumed", "run.cancelled"}:
                state["status"] = payload["status"]
                for task in payload.get("task_updates", []):
                    state["tasks"][task["task_id"]].update(status=task["status"], epoch=task["epoch"])
            elif kind == "task.created":
                state["tasks"][payload["task_id"]] = {"status": "pending", "depends_on": payload["depends_on"], "priority": payload["priority"], "channel": payload["channel"], "epoch": 0}
            elif kind in {"task.claimed", "task.completed", "task.output_submitted", "task.invalidated", "task.lease_expired", "task.verification_failed"}:
                state["tasks"][payload["task_id"]]["status"] = "running" if kind == "task.claimed" else payload["status"]
                if "output" in payload:
                    state["tasks"][payload["task_id"]]["output"] = payload["output"]
                if "epoch" in payload:
                    state["tasks"][payload["task_id"]]["epoch"] = payload["epoch"]
            elif kind in {"tool.intent", "tool.done", "tool.indeterminate", "effect.reconciled"}:
                state["effects"][payload["call_id"]] = payload["state"]
                if kind == "effect.reconciled" and "run_status" in payload:
                    state["status"] = payload["run_status"]
            elif kind == "budget.reserved":
                state["reservations"][payload["reservation_id"]] = dict(payload)
                state["reserved"] += payload["cost"]
                state["tokens_reserved"] += payload["tokens"]
            elif kind in {"budget.settled", "budget.overrun"}:
                reservation = state["reservations"][payload["reservation_id"]]
                state["reserved"] -= reservation["cost"]
                state["tokens_reserved"] -= reservation["tokens"]
                state["spent"] += payload["actual_cost"]
                state["tokens_spent"] += payload["actual_tokens"]
                reservation.update(payload)
                if kind == "budget.overrun":
                    state["status"] = payload.get("run_status", "awaiting_human")
            elif kind == "run.completed":
                state["status"] = payload["status"]
            state["last_seq"] = event["seq"]
        return state

    def snapshot(self, tenant: str, run_id: str) -> dict:
        with self.connect() as db:
            run = dict(self._run(db, tenant, run_id))
            run["tasks"] = [dict(r) for r in db.execute("SELECT id,title,status,channel,priority,epoch FROM tasks WHERE tenant=? AND run_id=? ORDER BY rowid", (tenant, run_id))]
            return run

    def drain_outbox(self, publish: Callable[[dict], None], limit: int = 100) -> int:
        """publish must return only after durable broker acknowledgement.

        Crash after broker ack but before marking published may redeliver. The
        receiver must use event_id as inbox key inside its own business txn.
        """
        with self.connect() as db:
            pending = db.execute("SELECT e.envelope,e.hash,e.id FROM outbox o JOIN events e ON e.id=o.event_id WHERE o.published=0 ORDER BY e.rowid LIMIT ?", (limit,)).fetchall()
        sent = 0
        for item in pending:
            publish({**json.loads(item["envelope"]), "hash": item["hash"]})
            with self.transaction() as db:
                db.execute("UPDATE outbox SET published=1 WHERE event_id=?", (item["id"],))
            sent += 1
        return sent

    def consume(self, consumer: str, event_id: str, apply: Callable[[sqlite3.Connection], None]) -> bool:
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM inbox WHERE consumer=? AND event_id=?", (consumer, event_id)).fetchone():
                return False
            apply(db)
            db.execute("INSERT INTO inbox VALUES (?,?)", (consumer, event_id))
            return True
