"""Versioned, bounded DAG plans for the local software-development workflow.

Goal and instruction text are untrusted task data. Capabilities are admitted
against the code-owned role registry; a plan cannot grant arbitrary host access.
All writes share Store's authority transaction, event chain and command receipt.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .artifact_schemas import ARTIFACT_ROLES, ArtifactInput, DevelopmentSuiteReference, RoleOutput, ReviewSubjectReference
from .policy import GateRules, TaskContract, gate_p
from .store import BudgetExceeded, Conflict, InvalidInput, NotFound, PlanBindingConflict, Store, canonical, digest


RoleId = Literal["analyst", "architect", "tester", "developer", "reviewer", "fixer", "validator"]
Identifier = Annotated[str, Field(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9_-]*$")]
ToolId = Literal["workspace.read", "workspace.write", "tests.run", "artifacts.read", "artifacts.write"]
ROLE_SKILLS = {
    "analyst": frozenset({"requirements.analysis"}),
    "architect": frozenset({"architecture.design"}),
    "tester": frozenset({"tests.design"}),
    "developer": frozenset({"code.implement"}),
    "reviewer": frozenset({"code.review"}),
    "fixer": frozenset({"code.fix"}),
    "validator": frozenset({"acceptance.verify"}),
}
_READ = frozenset({"workspace.read", "artifacts.read", "artifacts.write"})
ROLE_TOOLS = {
    "analyst": _READ, "architect": _READ, "reviewer": _READ,
    "tester": _READ | {"tests.run"}, "validator": _READ | {"tests.run"},
    "developer": _READ | {"workspace.write", "tests.run"},
    "fixer": _READ | {"workspace.write", "tests.run"},
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False,
                              frozen=True, revalidate_instances="always")


class AgentRole(StrictModel):
    id: RoleId
    skills: list[str] = Field(min_length=1, max_length=8)
    tools: list[ToolId] = Field(max_length=8)
    model_alias: Identifier

    @model_validator(mode="after")
    def authorized_capabilities(self):
        if len(self.skills) != len(set(self.skills)) or not set(self.skills) <= ROLE_SKILLS[self.id]:
            raise ValueError("role skills exceed the registered capability set")
        if len(self.tools) != len(set(self.tools)) or not set(self.tools) <= ROLE_TOOLS[self.id]:
            raise ValueError("role tools exceed the registered capability set")
        return self


class TaskLimits(StrictModel):
    # These caps cover the whole logical task, including its permitted attempts.
    cost_limit_microusd: int = Field(ge=0, le=1_000_000_000_000)
    token_limit: int = Field(ge=1, le=10_000_000)
    max_attempts: int = Field(default=3, ge=1, le=10)


class RebindPolicy(StrictModel):
    schema_version: Literal["loom.rebind/v1"]
    development_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_repairs: int = Field(ge=1, le=2)
    stop_on_duplicate_source: Literal[True] = True

    @field_validator("stop_on_duplicate_source", mode="before")
    @classmethod
    def duplicate_stop_is_mandatory(cls, value):
        if value is not True:
            raise ValueError("duplicate source stopping cannot be disabled")
        return value


class TaskNode(StrictModel):
    id: Identifier
    title: str = Field(min_length=1, max_length=500)
    instruction: str = Field(min_length=1, max_length=16000)
    role: RoleId
    depends: list[Identifier] = Field(default_factory=list, max_length=100)
    priority: int = Field(default=0, ge=-1000, le=1000)
    channel: Literal["business", "verification"] = "business"
    limits: TaskLimits
    evaluation_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Omit only the new optional field. Existing evaluation_digest:null remains
    # in old canonical plans, so their approved digests are not rewritten.
    repair: RebindPolicy | None = Field(default=None, exclude_if=lambda value: value is None)
    # Auxiliary data permission only: scheduling still uses depends exclusively.
    input_artifacts: list[ArtifactInput] | None = Field(default=None, max_length=8,
                                                      exclude_if=lambda value: value is None)
    role_output: RoleOutput | None = Field(default=None, exclude_if=lambda value: value is None)
    development_suite: DevelopmentSuiteReference | None = Field(default=None, exclude_if=lambda value: value is None)
    review_subject: ReviewSubjectReference | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("title", "instruction")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("task text cannot be blank")
        return value

    @model_validator(mode="after")
    def local_contract(self):
        if self.id in self.depends or len(self.depends) != len(set(self.depends)):
            raise ValueError("self or duplicate dependencies are not allowed")
        expected = "verification" if self.role == "validator" else "business"
        if self.channel != expected:
            raise ValueError("task channel does not match its authorized role")
        if self.repair is not None:
            if self.role not in {"developer", "fixer"} or self.evaluation_digest is None:
                raise ValueError("Rebind requires an executable role and frozen acceptance tests")
            if self.repair.max_repairs + 1 > self.limits.max_attempts:
                raise ValueError("repair policy exceeds the approved attempt limit")
        if self.role_output is not None:
            if self.role != ARTIFACT_ROLES[self.role_output.kind]:
                raise ValueError("explicit role output does not match the source role")
            if self.evaluation_digest is not None or self.repair is not None:
                raise ValueError("read-only role output does not authorize code evaluation or repair")
        if self.development_suite is not None:
            if (self.repair is None or self.role not in {"developer", "fixer"}
                    or self.repair.development_digest != self.development_suite.development_digest
                    or self.evaluation_digest == self.development_suite.development_digest):
                raise ValueError("reviewed development suite requires distinct frozen acceptance and matching Rebind policy")
        if self.role_output is not None and self.role_output.kind == "review-findings" and self.review_subject is None:
            raise ValueError("review findings require an exact implementation reference")
        if self.review_subject is not None:
            if self.role == "reviewer":
                if self.role_output is None or self.role_output.kind != "review-findings":
                    raise ValueError("reviewer reference requires explicit findings output")
            elif self.role == "fixer":
                if (self.evaluation_digest != self.review_subject.evaluation_digest
                        or self.repair is not None or self.development_suite is not None
                        or self.limits.max_attempts != 1
                        or not any(i.kind == "review-findings" and i.required for i in self.input_artifacts or [])):
                    raise ValueError("review fixer requires one explicitly approved attempt and the original acceptance digest")
            else:
                raise ValueError("implementation review references are restricted to reviewer and fixer")
        return self


class WorkflowDefinition(StrictModel):
    schema_version: Literal["loom.workflow/v1"]
    pack: Literal["software-dev"]
    scope: Literal["local-workspace"]
    roles: list[AgentRole] = Field(min_length=7, max_length=7)
    nodes: list[TaskNode] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def complete_dag(self):
        if {role.id for role in self.roles} != set(ROLE_SKILLS):
            raise ValueError("the seven registered roles must be configured exactly once")
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("duplicate task node ID")
        for node in self.nodes:
            if node.review_subject is not None:
                role = next(role for role in self.roles if role.id == node.role)
                if "artifacts.read" not in role.tools:
                    raise ValueError("review subject requires artifact read capability")
            if node.development_suite is not None:
                role = next(role for role in self.roles if role.id == node.role)
                if not {"artifacts.read", "workspace.write", "tests.run"} <= set(role.tools):
                    raise ValueError("reviewed suite execution requires read, write and test capabilities")
            if node.role_output is not None:
                role = next(role for role in self.roles if role.id == node.role)
                if "artifacts.write" not in role.tools:
                    raise ValueError("explicit role output requires artifact write capability")
            selectors = node.input_artifacts or []
            keys = [(item.source_node, item.kind) for item in selectors]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate artifact input selector")
            for item in selectors:
                source = nodes.get(item.source_node)
                if source is None or source.id == node.id or source.role != ARTIFACT_ROLES[item.kind]:
                    raise ValueError("artifact input source does not match its registered type")
                if item.kind == "review-findings" and (node.role != "fixer" or node.review_subject is None
                        or source.review_subject != node.review_subject):
                    raise ValueError("review input must bind the same implementation as its fixer")
        remaining = {node.id: set(node.depends) for node in self.nodes}
        if any(not dependencies <= nodes.keys() for dependencies in remaining.values()):
            raise ValueError("dependencies must refer to nodes in this workflow")
        completed = set()
        while remaining:
            ready = {name for name, dependencies in remaining.items() if dependencies <= completed}
            if not ready:
                raise ValueError("workflow must be an acyclic graph")
            completed |= ready
            remaining = {name: dependencies for name, dependencies in remaining.items() if name not in ready}
        return self

    @property
    def execution_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_goals (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, goal_id TEXT NOT NULL, intent_id TEXT NOT NULL,
 title TEXT NOT NULL, description TEXT NOT NULL, acceptance TEXT NOT NULL,
 scope TEXT NOT NULL, plan_revision INTEGER NOT NULL DEFAULT 0,
 plan_digest TEXT, plan_status TEXT NOT NULL DEFAULT 'draft', definition TEXT,
 PRIMARY KEY(tenant,run_id), FOREIGN KEY(tenant,run_id) REFERENCES runs(tenant,id));
CREATE TABLE IF NOT EXISTS workflow_tasks (
 tenant TEXT NOT NULL, run_id TEXT NOT NULL, node_id TEXT NOT NULL, task_id TEXT NOT NULL,
 contract TEXT NOT NULL, plan_digest TEXT NOT NULL, evaluation_digest TEXT,
 PRIMARY KEY(tenant,run_id,node_id), UNIQUE(tenant,task_id),
 FOREIGN KEY(tenant,run_id) REFERENCES workflow_goals(tenant,run_id),
 FOREIGN KEY(tenant,task_id) REFERENCES tasks(tenant,id));
"""


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidInput(f"{name} requires nonempty text within its length limit")


def _revision(value):
    if type(value) is not int or value < 0:
        raise InvalidInput("expected_revision must be a nonnegative integer")


class WorkflowService:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript(SCHEMA)
        with store.transaction() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(workflow_tasks)")}
            if "evaluation_digest" not in columns:
                # Existing local drafts/contracts are unbound until explicitly
                # reviewed again; NULL never authorizes executable evaluation.
                db.execute("ALTER TABLE workflow_tasks ADD COLUMN evaluation_digest TEXT")

    def _suite_validator(self, definition):
        if definition is not None and any(node.development_suite is not None for node in definition.nodes):
            from .development_suites import DevelopmentSuiteService
            return DevelopmentSuiteService(self.store)
        return None

    def _validate_suites_in(self, db, suites, tenant, run_id, definition, *, actor=None, approval=False):
        nodes = [node for node in definition.nodes if node.development_suite is not None]
        if nodes and approval:
            from .artifacts import _subject
            _subject(actor)  # Authentication/role enforcement belongs to the adapter.
        if nodes and suites is None:
            raise Conflict("suite references changed before plan approval")
        for node in nodes:
            suites.resolve_reference_in(db, tenant, run_id, node.development_suite)
        reviews = [node for node in definition.nodes if node.review_subject is not None]
        if reviews:
            from .review_inputs import ReviewInputService
            from .artifacts import _subject
            if approval:
                _subject(actor)
            for node in reviews:
                if node.review_subject.run_id == run_id:
                    raise Conflict("review subjects must refer to an existing separate implementation run")
                ReviewInputService(self.store).resolve_reference_in(
                    db, tenant, node.review_subject)

    @staticmethod
    def _goal(db, tenant, run_id):
        row = db.execute("SELECT * FROM workflow_goals WHERE tenant=? AND run_id=?", (tenant, run_id)).fetchone()
        if row is None:
            raise NotFound("workflow goal not found in tenant")
        return row

    @staticmethod
    def _plan(row, task_ids=None):
        return {"run_id": row["run_id"], "status": row["plan_status"], "revision": row["plan_revision"],
                "digest": row["plan_digest"], "definition": json.loads(row["definition"]) if row["definition"] else None,
                "task_ids": task_ids or {}}

    @staticmethod
    def _contract(definition, node, goal, run):
        """Rebuild execution fields from the reviewed definition, not a cache."""
        run_id = goal["run_id"]
        role = next(role for role in definition.roles if role.id == node.role)
        return {**node.model_dump(mode="json"), "node_id": node.id, "task_id": run_id + ":" + node.id,
                "run_id": run_id, "depends_on": [run_id + ":" + dependency for dependency in node.depends],
                "intent_id": goal["intent_id"], "goal_id": goal["goal_id"], "scope": definition.scope,
                "model_alias": role.model_alias, "skills": role.skills, "tools": role.tools,
                "anchor_digest": run["anchor"], "plan_digest": goal["plan_digest"], "status": "pending"}

    def create_goal(self, tenant, key, *, title, description, acceptance,
                    cost_limit=1_000_000, token_limit=10000, actor=None):
        _text(tenant, "tenant", 200)
        _text(key, "idempotency key", 200)
        _text(title, "title", 500)
        _text(description, "description", 32000)
        if (not isinstance(acceptance, list) or not acceptance
                or any(not isinstance(item, str) for item in acceptance)
                or len(acceptance) != len(set(acceptance)) or not set(acceptance) <= self.store.CRITERIA):
            raise InvalidInput("acceptance must contain unique registered criterion IDs")
        if (type(cost_limit) is not int or type(token_limit) is not int
                or not 0 <= cost_limit <= 1_000_000_000_000 or not 0 <= token_limit <= 1_000_000_000):
            raise InvalidInput("goal budgets must be bounded nonnegative integers")
        args = dict(title=title, description=description, acceptance=acceptance,
                    cost_limit=cost_limit, token_limit=token_limit, ruleset="kernel-v1", scope="local-workspace")
        if actor is not None:
            _text(actor, "actor", 200)
            args["actor"] = actor
        audit_actor = actor or "operator:local"

        def apply(db):
            run_id, goal_id, intent_id = str(uuid4()), str(uuid4()), str(uuid4())
            anchor = digest({"acceptance": acceptance})
            db.execute("INSERT INTO runs(tenant,id,anchor,ruleset,status,cost_limit,token_limit) VALUES (?,?,?,?,?,?,?)",
                       (tenant, run_id, anchor, "kernel-v1", "draft", cost_limit, token_limit))
            db.execute("INSERT INTO workflow_goals(tenant,run_id,goal_id,intent_id,title,description,acceptance,scope) VALUES (?,?,?,?,?,?,?,?)",
                       (tenant, run_id, goal_id, intent_id, title, description, canonical(acceptance), "local-workspace"))
            result = {"run_id": run_id, "goal_id": goal_id, "intent_id": intent_id, "anchor_digest": anchor,
                      "ruleset": "kernel-v1", "status": "draft", "plan_revision": 0}
            self.store._event(db, tenant, run_id, "run.created", {**result, **args, "managed_workflow": True}, audit_actor, key)
            self.store._event(db, tenant, run_id, "goal.created", {"goal_id": goal_id, "anchor": anchor,
                              "title": title, "description": description}, audit_actor)
            self.store._event(db, tenant, run_id, "intent.created", {"intent_id": intent_id, "goal_id": goal_id,
                              "from_facts": [{"kind": "anchor", "id": anchor}]}, "system")
            return result
        return self.store._command(tenant, key, "workflow.create_goal", args, apply)

    def save_plan(self, tenant, run_id, key, definition, expected_revision, *, actor=None):
        _text(tenant, "tenant", 200)
        _text(run_id, "run ID", 100)
        _text(key, "idempotency key", 200)
        _revision(expected_revision)
        try:
            # Dump and revalidate even existing model instances, whose lists can be mutated.
            source = definition.model_dump(mode="json") if isinstance(definition, WorkflowDefinition) else definition
            definition = WorkflowDefinition.model_validate(source)
        except (ValidationError, TypeError, ValueError):
            raise InvalidInput("invalid workflow definition") from None
        body, plan_digest = definition.model_dump(mode="json"), definition.execution_digest
        suites = self._suite_validator(definition)
        args = dict(run_id=run_id, definition=body, expected_revision=expected_revision)
        if actor is not None:
            _text(actor, "actor", 200)
            args["actor"] = actor

        def apply(db):
            goal = self._goal(db, tenant, run_id)
            if goal["plan_status"] != "draft" or self.store._run(db, tenant, run_id)["status"] != "draft":
                raise Conflict("approved workflow plans are immutable")
            if goal["plan_revision"] != expected_revision:
                raise Conflict("plan revision changed")
            self._validate_suites_in(db, suites, tenant, run_id, definition)
            revision = expected_revision + 1
            db.execute("UPDATE workflow_goals SET plan_revision=?,plan_digest=?,definition=? WHERE tenant=? AND run_id=?",
                       (revision, plan_digest, canonical(body), tenant, run_id))
            result = {"run_id": run_id, "status": "draft", "revision": revision, "digest": plan_digest,
                      "definition": body, "task_ids": {}}
            self.store._event(db, tenant, run_id, "workflow.plan_saved", result, actor or "operator:local", key)
            return result
        return self.store._command(tenant, key, "workflow.save_plan", args, apply)

    def approve_plan(self, tenant, run_id, key, expected_revision, expected_digest, *, actor=None):
        _text(tenant, "tenant", 200)
        _text(run_id, "run ID", 100)
        _text(key, "idempotency key", 200)
        _revision(expected_revision)
        if not isinstance(expected_digest, str) or len(expected_digest) != 64:
            raise InvalidInput("expected_digest must identify the reviewed plan")
        args = dict(run_id=run_id, expected_revision=expected_revision, expected_digest=expected_digest)
        if actor is not None:
            _text(actor, "actor", 200)
            args["actor"] = actor

        # Initialize read adapters before the authority transaction. Approval
        # reconstructs/rechecks the current definition in that transaction.
        with self.store.connect() as db:
            try:
                preliminary = WorkflowDefinition.model_validate(json.loads(self._goal(db, tenant, run_id)["definition"]))
            except (ValidationError, TypeError, ValueError):
                preliminary = None
        suites = self._suite_validator(preliminary)

        def apply(db):
            goal, run = self._goal(db, tenant, run_id), self.store._run(db, tenant, run_id)
            if goal["plan_status"] != "draft" or run["status"] != "draft":
                raise Conflict("workflow plan is already approved or no longer draft")
            if goal["plan_revision"] != expected_revision or goal["plan_digest"] != expected_digest or goal["definition"] is None:
                raise Conflict("approval does not match the current reviewed plan")
            try:
                definition = WorkflowDefinition.model_validate(json.loads(goal["definition"]))
            except (ValidationError, TypeError, ValueError):
                raise Conflict("persisted workflow definition is invalid") from None
            if definition.execution_digest != expected_digest:
                raise Conflict("persisted plan digest does not match its execution fields")
            self._validate_suites_in(db, suites, tenant, run_id, definition, actor=actor, approval=True)
            if (sum(node.limits.cost_limit_microusd for node in definition.nodes) > run["cost_limit"]
                    or sum(node.limits.token_limit for node in definition.nodes) > run["token_limit"]):
                raise BudgetExceeded("task budget caps exceed the frozen goal budget")
            if db.execute("SELECT 1 FROM tasks WHERE tenant=? AND run_id=?", (tenant, run_id)).fetchone():
                raise Conflict("draft workflow contains tasks outside its approved plan")
            ids = {node.id: run_id + ":" + node.id for node in definition.nodes}
            tasks = []
            for node in definition.nodes:
                task_id = ids[node.id]
                admission = gate_p(TaskContract(task_id, run_id, (definition.scope,), goal["goal_id"],
                                   tuple(json.loads(goal["acceptance"])), run["anchor"]),
                                   rules=GateRules(version=run["ruleset"]), allowed_scope=frozenset({"local-workspace"}),
                                   known_anchors=frozenset({run["anchor"]}), known_criteria=self.store.CRITERIA)
                if not admission.passed:
                    raise InvalidInput("Gate-P rejected the workflow contract")
                depends = [ids[dependency] for dependency in node.depends]
                db.execute("INSERT INTO tasks(tenant,id,run_id,intent_id,title,depends,priority,channel,status) VALUES (?,?,?,?,?,?,?,?,?)",
                           (tenant, task_id, run_id, goal["intent_id"], node.title, canonical(depends), node.priority, node.channel, "pending"))
                contract = self._contract(definition, node, goal, run)
                db.execute("INSERT INTO workflow_tasks(tenant,run_id,node_id,task_id,contract,plan_digest,evaluation_digest) VALUES (?,?,?,?,?,?,?)",
                           (tenant, run_id, node.id, task_id, canonical(contract), expected_digest, node.evaluation_digest))
                self.store._event(db, tenant, run_id, "task.created", contract, "system", key)
                tasks.append(contract)
            db.execute("UPDATE workflow_goals SET plan_status='approved' WHERE tenant=? AND run_id=?", (tenant, run_id))
            db.execute("UPDATE runs SET status='open' WHERE tenant=? AND id=?", (tenant, run_id))
            result = {"run_id": run_id, "status": "open", "revision": expected_revision, "digest": expected_digest,
                      "tasks": tasks, "task_ids": ids}
            self.store._event(db, tenant, run_id, "workflow.plan_approved", result, actor or "operator:local", key)
            return result
        return self.store._command(tenant, key, "workflow.approve_plan", args, apply)

    def get_goal(self, tenant, run_id):
        with self.store.connect() as db:
            goal, run = self._goal(db, tenant, run_id), self.store._run(db, tenant, run_id)
            return {"run_id": run_id, "goal_id": goal["goal_id"], "intent_id": goal["intent_id"],
                    "title": goal["title"], "description": goal["description"], "acceptance": json.loads(goal["acceptance"]),
                    "scope": goal["scope"], "status": run["status"], "plan_status": goal["plan_status"],
                    "plan_revision": goal["plan_revision"], "plan_digest": goal["plan_digest"],
                    "cost_limit_microusd": run["cost_limit"], "token_limit": run["token_limit"],
                    "spent_microusd": run["spent"], "tokens_spent": run["tokens_spent"]}

    def get_plan(self, tenant, run_id):
        with self.store.connect() as db:
            goal = self._goal(db, tenant, run_id)
            ids = {row["node_id"]: row["task_id"] for row in db.execute(
                "SELECT node_id,task_id FROM workflow_tasks WHERE tenant=? AND run_id=? ORDER BY rowid", (tenant, run_id))}
            return self._plan(goal, ids)

    def list_goals(self, tenant):
        with self.store.connect() as db:
            ids = [row[0] for row in db.execute("SELECT run_id FROM workflow_goals WHERE tenant=? ORDER BY rowid DESC", (tenant,))]
        return [self.get_goal(tenant, run_id) for run_id in ids]

    @staticmethod
    def _task_spec_in(db, store, tenant, run_id, task_id):
        """Validate frozen contract and scheduler projection in one DB snapshot."""
        goal, run = WorkflowService._goal(db, tenant, run_id), store._run(db, tenant, run_id)
        row = db.execute("SELECT node_id,contract,evaluation_digest,plan_digest FROM workflow_tasks WHERE tenant=? AND run_id=? AND task_id=?",
                         (tenant, run_id, task_id)).fetchone()
        if row is None:
            raise NotFound("workflow task not found in tenant/run")
        if goal["plan_status"] != "approved" or row["plan_digest"] != goal["plan_digest"]:
            raise PlanBindingConflict("workflow contract does not belong to the approved plan")
        try:
            definition = WorkflowDefinition.model_validate(json.loads(goal["definition"]))
            contract = json.loads(row["contract"])
            if definition.execution_digest != goal["plan_digest"] or not isinstance(contract, dict):
                raise ValueError
            node = next((node for node in definition.nodes if node.id == row["node_id"]), None)
            if node is None or run_id + ":" + node.id != task_id:
                raise ValueError
            expected = WorkflowService._contract(definition, node, goal, run)
            actual_fields = canonical({key: value for key, value in contract.items() if key != "status"})
            expected_fields = canonical({key: value for key, value in expected.items() if key != "status"})
            task = db.execute("SELECT intent_id,title,depends,priority,channel FROM tasks WHERE tenant=? AND run_id=? AND id=?",
                              (tenant, run_id, task_id)).fetchone()
            if (task is None or expected["evaluation_digest"] != row["evaluation_digest"]
                    or actual_fields != expected_fields
                    or task["intent_id"] != expected["intent_id"] or task["title"] != expected["title"]
                    or task["priority"] != expected["priority"] or task["channel"] != expected["channel"]):
                raise ValueError
            if json.loads(task["depends"]) != expected["depends_on"]:
                raise PlanBindingConflict("hard dependencies differ from the approved workflow")
        except (ValidationError, TypeError, ValueError, AttributeError, RecursionError):
            raise PlanBindingConflict("approved workflow and scheduler projection differ") from None
        return expected

    def get_task_spec(self, tenant, run_id, task_id):
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self._task_spec_in(db, self.store, tenant, run_id, task_id)
