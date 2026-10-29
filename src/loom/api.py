"""Loopback API with named local roles; production OIDC remains future work."""
from dataclasses import asdict
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .attempts import attempt_summaries
from .auth import AuthRegistry, LocalIdentity, Principal
from .artifacts import ArtifactService
from .development_suites import DevelopmentSuiteService
from .artifact_schemas import MAX_BODY_BYTES
from .role_summary import submitted_role_source
from .review_inputs import ReviewFindingsBody, ReviewInputService
from .review_decisions import Decision, ReviewDecisionService
from .progression import ProgressionService
from .dispatch import DispatchService
from .executor_status import ExecutorStatus
from .tool_registry import catalog as tool_catalog
from .tool_recovery import ToolRecoveryService
from .quotas import QuotaService
from .operator import OperatorService
from .local_console import allows_local_console, console_principal
from .store import Claim, Conflict, InvalidInput, LoomError, NotFound, Store, canonical, digest
from .workflow import WorkflowDefinition, WorkflowService


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class RunInput(Input):
    acceptance: list[str] = Field(min_length=1, max_length=100)
    cost_limit_microusd: int = Field(default=1_000_000, ge=0)
    token_limit: int = Field(default=10000, ge=0)
    ruleset: str = Field(default="kernel-v1", min_length=1, max_length=100)


class TaskInput(Input):
    task_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    priority: int = Field(default=0, ge=-1000, le=1000)
    channel: str = "business"


class GoalInput(Input):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=32000)
    acceptance: list[str] = Field(min_length=1, max_length=3)
    cost_limit_microusd: int = Field(default=1_000_000, ge=0, le=1_000_000_000_000)
    token_limit: int = Field(default=10000, ge=0, le=1_000_000_000)


class PlanInput(Input):
    definition: WorkflowDefinition
    expected_revision: int = Field(ge=0)


class PlanApprovalInput(Input):
    expected_revision: int = Field(ge=1)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SuiteFreezeInput(Input):
    expected_review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_case_names: list[str] | None = Field(default=None, min_length=1, max_length=32)


class ClaimInput(Input):
    worker_id: str = Field(min_length=1, max_length=128)
    channel: str = "business"
    lease_seconds: float = Field(default=30, gt=0, le=3600)


class ControlInput(Input):
    reason: str = Field(min_length=1, max_length=2000)


class ReconciliationEvidence(Input):
    reference: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=2000)


class ReconciliationInput(Input):
    decision: str = Field(pattern=r"^(completed|not_sent)$")
    evidence: ReconciliationEvidence
    actual_cost_microusd: int = Field(ge=0, le=1_000_000_000_000)
    actual_tokens: int = Field(ge=0, le=1_000_000_000)


class ToolReconciliationInput(Input):
    expected_epoch: int = Field(ge=1)
    expected_request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal['completed','not_executed']
    evidence: ReconciliationEvidence


class CompleteInput(Input):
    worker_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1)
    output: dict[str, Any]

    @field_validator("output")
    @classmethod
    def bounded_json(cls, value):
        if len(canonical(value).encode("utf-8")) > 262144:
            raise ValueError("local artifact body exceeds 256 KiB; use a content-addressed object adapter")
        return value



class CandidateArtifactInput(Input):
    worker_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1)
    kind: Literal["requirements", "design", "public-checks"]
    body: dict[str, Any]

    @field_validator("body")
    @classmethod
    def bounded_candidate(cls, value):
        if len(canonical(value).encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("candidate body exceeds 32 KiB")
        return value


class AuxiliaryInputApproval(Input):
    artifact_id: str = Field(min_length=1, max_length=200)
    expected_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_epoch: int = Field(ge=1)
    expected_target_epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=1)
    expected_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewDecisionInput(Input):
    expected_preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
    reason: str = Field(min_length=1, max_length=2000)
    target_task_id: str | None = Field(default=None, min_length=1, max_length=200)


class ExecutionRequestInput(Input):
    expected_dispatch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1,max_length=2000)


def create_app(store: Store, tenant_keys: dict[str, str], *, identities: tuple[LocalIdentity, ...] = (), local_console: bool = False, executor_config=None) -> FastAPI:
    registry = AuthRegistry(tenant_keys, identities)
    local_identity = console_principal(identities, local_console)
    app = FastAPI(title="Loom local kernel", version=__version__,
                  description="Single-host audited API. A separately configured private worker processes explicit model execution requests; independent verification remains pending.")
    workflows = WorkflowService(store)
    operators = OperatorService(store)
    artifacts = ArtifactService(store)
    suites = DevelopmentSuiteService(store)
    review_inputs = ReviewInputService(store)
    review_decisions = ReviewDecisionService(store)
    progression = ProgressionService(store)
    dispatch = DispatchService(store,config=executor_config,identities=identities)
    executor_status = ExecutorStatus(store,executor_config,identities)
    security = HTTPBearer(auto_error=False)

    def principal(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]) -> Principal:
        if credentials is not None:
            identity = registry.authenticate(credentials.credentials)
            if identity is not None:
                return identity
        if 'authorization' not in request.headers and local_identity is not None and allows_local_console(request):
            return local_identity
        raise HTTPException(401, "valid tenant bearer token required", headers={"WWW-Authenticate": "Bearer"})

    def permitted(action):
        def resolve(identity: Annotated[Principal, Depends(principal)]) -> Principal:
            if not registry.authorize(identity, action):
                raise HTTPException(403, "this identity cannot perform the requested action")
            return identity
        return resolve

    def tenant_for(action):
        def resolve(identity: Annotated[Principal, Depends(permitted(action))]) -> str:
            return identity.tenant_id
        return resolve

    def audit(identity):
        return {} if "developer" in identity.roles else {"actor": "subject:" + identity.subject_id}

    Tenant = Annotated[str, Depends(tenant_for("read"))]
    WriteTenant = Annotated[str, Depends(tenant_for("goal.write"))]
    Writer = Annotated[Principal, Depends(permitted("goal.write"))]
    Approver = Annotated[Principal, Depends(permitted("plan.approve"))]
    Controller = Annotated[Principal, Depends(permitted("control"))]
    SuiteApprover = Annotated[Principal, Depends(permitted("suite.approve"))]
    Claimer = Annotated[Principal, Depends(permitted("task.claim"))]
    Submitter = Annotated[Principal, Depends(permitted("task.submit"))]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]

    @app.middleware("http")
    async def response_policy(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/console"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    web = Path(__file__).with_name("web")
    app.mount("/console/assets", StaticFiles(directory=web, check_dir=False), name="console-assets")

    @app.get("/console", include_in_schema=False)
    @app.get("/console/", include_in_schema=False)
    def console():
        return FileResponse(web / "index.html")

    @app.get("/v1/me")
    def me(request: Request, identity: Annotated[Principal, Depends(principal)]):
        return {"tenant_id": identity.tenant_id, "subject_id": identity.subject_id,
                "roles": sorted(identity.roles), "capabilities": registry.capabilities(identity),
                "authentication": "public-session" if getattr(request.state, 'public_preview', False) else "local-console" if 'authorization' not in request.headers else "local-bearer", "production_identity": False}

    @app.exception_handler(LoomError)
    async def loom_error(_, exc: LoomError):
        status = 404 if isinstance(exc, NotFound) else 422 if isinstance(exc, InvalidInput) else 409 if isinstance(exc, Conflict) else 500
        return JSONResponse(status_code=status, content={"code": exc.code, "detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, exc: RequestValidationError):
        # Do not echo artifact bodies, secrets or non-finite numbers in errors.
        return JSONResponse(status_code=422, content={"code": "INVALID_INPUT", "detail": [
            {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]} for error in exc.errors()
        ]})

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "stage": "S1-local", "external_writes_enabled": False}

    @app.post("/v1/goals", status_code=201)
    def create_goal(body: GoalInput, identity: Writer, idempotency_key: Key):
        return workflows.create_goal(identity.tenant_id, idempotency_key, title=body.title, description=body.description,
                                     acceptance=body.acceptance, cost_limit=body.cost_limit_microusd,
                                     token_limit=body.token_limit, **audit(identity))

    @app.get("/v1/goals")
    def list_goals(tenant: Tenant):
        return {"goals": workflows.list_goals(tenant)}

    @app.get("/v1/executor-status")
    def read_executor_status(tenant: Tenant):
        return executor_status.read(tenant)

    @app.get("/v1/tools")
    def read_tools(tenant: Tenant):
        return tool_catalog(tenant, executor_config)

    @app.get('/v1/verification-capabilities')
    def read_verification_capabilities(tenant: Tenant):
        from .verification_signatures import capabilities
        return capabilities(tenant)

    @app.get('/v1/quotas')
    def read_quotas(tenant: Tenant):
        return QuotaService(store).read(tenant)

    @app.get("/v1/goals/{run_id}")
    def get_goal(run_id: str, tenant: Tenant):
        return workflows.get_goal(tenant, run_id)

    @app.get("/v1/goals/{run_id}/plan")
    def get_plan(run_id: str, tenant: Tenant):
        return workflows.get_plan(tenant, run_id)

    @app.put("/v1/goals/{run_id}/plan")
    def save_plan(run_id: str, body: PlanInput, identity: Writer, idempotency_key: Key):
        return workflows.save_plan(identity.tenant_id, run_id, idempotency_key, body.definition, body.expected_revision, **audit(identity))

    @app.post("/v1/goals/{run_id}/plan/approve")
    def approve_plan(run_id: str, body: PlanApprovalInput, identity: Approver, idempotency_key: Key):
        definition = workflows.get_plan(identity.tenant_id, run_id)["definition"] or {}
        if (any(node.get("development_suite") is not None or node.get("review_subject") is not None
                for node in definition.get("nodes", []))
                and not registry.authorize(identity, "suite.approve")):
            raise HTTPException(403, "reviewed-suite execution plans require a named operator")
        return workflows.approve_plan(identity.tenant_id, run_id, idempotency_key, body.expected_revision, body.expected_digest, **audit(identity))

    @app.get("/v1/interventions")
    def interventions(tenant: Tenant):
        return {"items": operators.list_interventions(tenant)}

    @app.get("/v1/runs/{run_id}/resume-preview")
    def resume_preview(run_id: str, tenant: Tenant):
        return operators.resume_preview(tenant, run_id)

    @app.get("/v1/runs/{run_id}/progression")
    def read_progression(run_id: str, tenant: Tenant):
        return progression.read(tenant, run_id)

    @app.get("/v1/runs/{run_id}/tasks/{task_id}/execution-preview")
    def execution_preview(run_id: str,task_id: str,tenant: Tenant):
        return dispatch.preview(tenant,run_id,task_id)

    @app.get("/v1/runs/{run_id}/tasks/{task_id}/execution-requests")
    def execution_history(run_id: str,task_id: str,tenant: Tenant):
        return dispatch.history(tenant,run_id,task_id)

    @app.post("/v1/runs/{run_id}/tasks/{task_id}/execution-requests",status_code=201)
    def request_execution(run_id: str,task_id: str,body: ExecutionRequestInput,identity: Controller,idempotency_key: Key):
        return dispatch.enqueue(identity.tenant_id,run_id,task_id,idempotency_key,actor='subject:'+identity.subject_id,**body.model_dump())

    @app.post("/v1/runs/{run_id}/execution-requests/{request_id}/cancel")
    def cancel_execution(run_id: str,request_id: str,body: ControlInput,identity: Controller,idempotency_key: Key):
        return dispatch.cancel(identity.tenant_id,run_id,request_id,idempotency_key,actor='subject:'+identity.subject_id,reason=body.reason)

    @app.post("/v1/runs/{run_id}/control/{action}")
    def control(run_id: str, action: str, body: ControlInput, identity: Controller, idempotency_key: Key):
        if action not in {"pause", "resume", "terminate"}:
            raise HTTPException(404, "unknown control action")
        return getattr(operators, action)(identity.tenant_id, run_id, idempotency_key, "subject:" + identity.subject_id, body.reason)

    @app.post("/v1/runs/{run_id}/effects/{call_id}/reconcile")
    def reconcile(run_id: str, call_id: str, body: ReconciliationInput, identity: Controller, idempotency_key: Key):
        return operators.reconcile_effect(identity.tenant_id, run_id, call_id, idempotency_key,
            "subject:" + identity.subject_id, body.decision, body.evidence.model_dump(), body.actual_cost_microusd, body.actual_tokens)

    @app.post('/v1/runs/{run_id}/tool-effects/{call_id}/reconcile')
    def reconcile_tool(run_id: str,call_id: str,body: ToolReconciliationInput,identity: Controller,idempotency_key: Key):
        return ToolRecoveryService(store).reconcile(identity,run_id,call_id,idempotency_key,**body.model_dump())

    @app.post("/v1/runs", status_code=201)
    def create_run(body: RunInput, tenant: WriteTenant, idempotency_key: Key):
        return store.create_run(tenant, idempotency_key, acceptance=body.acceptance,
                                cost_limit=body.cost_limit_microusd, token_limit=body.token_limit, ruleset=body.ruleset)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, tenant: Tenant):
        return store.snapshot(tenant, run_id)

    @app.get("/v1/runs/{run_id}/review")
    def review_run(run_id: str, tenant: Tenant):
        # Display submitted data separately from a trusted executor event.
        # A worker's own tests.passed flag is never trusted evaluation evidence.
        with store.connect() as db:
            run = dict(store._run(db, tenant, run_id))
            evaluations = {}
            for event in db.execute("SELECT envelope FROM events WHERE tenant=? AND run_id=? AND json_extract(envelope,'$.type')='execution.evaluated' ORDER BY seq", (tenant, run_id)):
                data = json.loads(event[0])["payload"]
                evaluations[(data["task_id"], data["epoch"], data["content_hash"])] = data
            tasks = []
            for row in db.execute("SELECT * FROM tasks WHERE tenant=? AND run_id=? ORDER BY rowid", (tenant, run_id)):
                item = {key: row[key] for key in ("id", "title", "status", "channel", "priority", "epoch")}
                spec = None
                contract = db.execute("SELECT contract FROM workflow_tasks WHERE tenant=? AND run_id=? AND task_id=?", (tenant, run_id, row["id"])).fetchone()
                if contract:
                    spec = workflows.get_task_spec(tenant, run_id, row["id"])
                    item.update({key: spec[key] for key in ("role", "model_alias", "evaluation_digest", "limits")})
                    item["input_artifacts"] = spec.get("input_artifacts") or []
                    if spec.get("role_output") is not None:
                        item["role_output"] = spec["role_output"]
                if row["output"] is not None:
                    output = json.loads(row["output"])
                    identity = digest(output)
                    evidence = evaluations.get((row["id"], row["epoch"], identity))
                    item["artifact"] = {"content_hash": identity, "kind": output.get("kind", "submitted-data"),
                        "trusted_evaluation": evidence is not None,
                        "hard_criteria_passed": evidence.get("hard_criteria_passed") if evidence else None,
                        "verification": "awaiting_independent_models" if row["status"] == "submitted" else row["status"],
                        "cases": []}
                    if output.get("kind") == "role-artifact":
                        item["artifact"].update(submitted_role_source(artifacts, tenant, run_id, row, spec, output))
                    tests = output.get("tests", {})
                    if evidence and isinstance(tests, dict) and isinstance(tests.get("cases"), list):
                        item["artifact"]["cases"] = [{key: case.get(key) for key in ("name", "passed", "status")} for case in tests["cases"][:32] if isinstance(case, dict)]
                tasks.append(item)
            effects = []
            now = store.now(db)
            tool_ids=store._tool_call_ids(db,tenant)
            for row in db.execute("SELECT id,task_id,epoch,request_hash,state,result FROM effects WHERE tenant=? AND run_id=? ORDER BY rowid", (tenant, run_id)):
                model_generation = row["id"] == digest({"tenant_id": tenant, "run_id": run_id, "task_id": row["task_id"], "operation_id": f"model-generation:{row['epoch']}"})
                needs_manual_accounting = bool(model_generation and row["state"] == "done" and row["result"] is not None
                    and db.execute("""SELECT 1 FROM reservations b WHERE b.tenant=? AND b.run_id=? AND b.task_id=?
                        AND b.epoch=? AND b.id=? AND b.state='reserved' AND NOT EXISTS
                        (SELECT 1 FROM tasks owner WHERE owner.tenant=b.tenant AND owner.run_id=b.run_id
                         AND owner.id=b.task_id AND owner.epoch=b.epoch AND owner.status='running' AND owner.lease_until>?)""",
                        (tenant, run_id, row["task_id"], row["epoch"], f"python:{row['task_id']}:{row['epoch']}", now)).fetchone())
                effects.append({"call_id": row["id"], "task_id": row["task_id"], "epoch": row["epoch"], "state": row["state"],
                    "registered_tool": row['id'] in tool_ids,"request_hash": row['request_hash'],
                    "not_sent_conflicts_with_evidence": row["state"] == "partial" or row["result"] is not None,
                    "model_generation": model_generation, "needs_manual_accounting": needs_manual_accounting})
            reservations = [{"reservation_id": row["id"], **{key: row[key] for key in ("task_id", "epoch", "state", "cost", "tokens", "actual_cost", "actual_tokens")}} for row in db.execute("SELECT * FROM reservations WHERE tenant=? AND run_id=? ORDER BY rowid", (tenant, run_id))]
            return {"run": run, "tasks": tasks, "effects": effects, "reservations": reservations,
                    "attempts": attempt_summaries(store, tenant, run_id),
                    "verification": "pending_configuration", "counts_as_success": False}

    @app.get("/v1/runs/{run_id}/artifacts")
    def list_artifacts(run_id: str, tenant: Tenant, task_id: str | None = None):
        return {"items": artifacts.list_artifacts(tenant, run_id, task_id=task_id),
                "producer_verified": False, "counts_as_success": False}

    @app.get("/v1/runs/{run_id}/tasks/{task_id}/review-input")
    def preview_review_input(run_id: str, task_id: str, tenant: Tenant, include_source: bool = False):
        return review_inputs.preview(tenant, run_id, task_id, include_source=include_source)

    @app.post("/v1/runs/{run_id}/tasks/{task_id}/review-findings/validate")
    def validate_review_findings(run_id: str, task_id: str, body: ReviewFindingsBody, tenant: Tenant):
        # Pure validation: no candidate persistence, operator approval or dispatch.
        return review_inputs.validate_findings(tenant, run_id, task_id, body.model_dump(mode="json"))

    @app.get("/v1/runs/{run_id}/artifacts/{artifact_id}")
    def get_artifact(run_id: str, artifact_id: str, tenant: Tenant):
        return artifacts.get_artifact(tenant, run_id, artifact_id)

    @app.get("/v1/runs/{run_id}/artifacts/{artifact_id}/review-decision-preview")
    def preview_review_decision(run_id: str, artifact_id: str, tenant: Tenant):
        return review_decisions.preview(tenant, run_id, artifact_id)

    @app.get("/v1/runs/{run_id}/artifacts/{artifact_id}/review-decisions")
    def review_decision_history(run_id: str, artifact_id: str, tenant: Tenant):
        return review_decisions.history(tenant, run_id, artifact_id)

    @app.post("/v1/runs/{run_id}/artifacts/{artifact_id}/review-decisions", status_code=201)
    def record_review_decision(run_id: str, artifact_id: str, body: ReviewDecisionInput,
                               identity: Controller, idempotency_key: Key):
        return review_decisions.decide(identity.tenant_id, run_id, artifact_id, idempotency_key,
            actor="subject:" + identity.subject_id, **body.model_dump())

    @app.post("/v1/runs/{run_id}/tasks/{task_id}/artifacts", status_code=201)
    def save_candidate(run_id: str, task_id: str, body: CandidateArtifactInput, identity: Submitter, idempotency_key: Key):
        if "worker" not in identity.roles or body.worker_id != identity.subject_id:
            raise HTTPException(403, "candidate publication requires the authenticated named worker")
        claim = Claim(identity.tenant_id, run_id, task_id, identity.subject_id, body.epoch, 0)
        return artifacts.save_candidate(claim, idempotency_key, body.kind, body.body,
                                        actor="subject:" + identity.subject_id)

    @app.post("/v1/runs/{run_id}/tasks/{task_id}/auxiliary-inputs")
    def approve_auxiliary_input(run_id: str, task_id: str, body: AuxiliaryInputApproval,
                               identity: Controller, idempotency_key: Key):
        return artifacts.approve_input(identity.tenant_id, run_id, task_id, body.artifact_id, idempotency_key,
            actor="subject:" + identity.subject_id, expected_plan_digest=body.expected_plan_digest,
            expected_source_epoch=body.expected_source_epoch, expected_revision=body.expected_revision,
            expected_content_hash=body.expected_content_hash, expected_target_epoch=body.expected_target_epoch)

    @app.get("/v1/runs/{run_id}/tasks/{task_id}/context-preview")
    def preview_context(run_id: str, task_id: str, tenant: Tenant):
        return artifacts.preview_context(tenant, run_id, task_id)

    @app.get("/v1/runs/{run_id}/artifacts/{artifact_id}/development-preview")
    def preview_development_suite(run_id: str, artifact_id: str, tenant: Tenant,
                                  case: Annotated[list[str] | None, Query(max_length=32)] = None):
        return suites.preview(tenant, run_id, artifact_id, selected_case_names=case)

    @app.post("/v1/runs/{run_id}/artifacts/{artifact_id}/development-suites", status_code=201)
    def freeze_development_suite(run_id: str, artifact_id: str, body: SuiteFreezeInput,
                                 identity: SuiteApprover, idempotency_key: Key):
        return suites.freeze(identity.tenant_id, run_id, artifact_id, idempotency_key,
            actor="subject:" + identity.subject_id, expected_review_digest=body.expected_review_digest,
            selected_case_names=body.selected_case_names)

    @app.get("/v1/runs/{run_id}/development-suites/{suite_id}")
    def get_development_suite(run_id: str, suite_id: str, tenant: Tenant, include_body: bool = False):
        return suites.get_suite(tenant, run_id, suite_id, include_body=include_body)

    @app.get("/v1/runs/{run_id}/attempts")
    def read_attempts(run_id: str, tenant: Tenant):
        return {"items": attempt_summaries(store, tenant, run_id),
                "verification": "pending_configuration", "counts_as_success": False}

    @app.post("/v1/runs/{run_id}/tasks", status_code=201)
    def add_task(run_id: str, body: TaskInput, tenant: WriteTenant, idempotency_key: Key):
        return store.add_task(tenant, run_id, idempotency_key, **body.model_dump())

    @app.post("/v1/runs/{run_id}/claim")
    def claim(run_id: str, body: ClaimInput, identity: Claimer):
        if "developer" not in identity.roles and body.worker_id != identity.subject_id:
            raise HTTPException(403, "worker identity must match the authenticated subject")
        result = store.claim_next(identity.tenant_id, run_id, body.worker_id, channel=body.channel, lease_seconds=body.lease_seconds)
        return {"claim": asdict(result) if result else None}

    @app.post("/v1/runs/{run_id}/tasks/{task_id}/submit")
    def submit(run_id: str, task_id: str, body: CompleteInput, identity: Submitter, idempotency_key: Key):
        if "developer" not in identity.roles and body.worker_id != identity.subject_id:
            raise HTTPException(403, "worker identity must match the authenticated subject")
        current = Claim(identity.tenant_id, run_id, task_id, body.worker_id, body.epoch, 0)
        return store.submit_output(current, idempotency_key, body.output)

    @app.get("/v1/runs/{run_id}/events")
    def events(run_id: str, tenant: Tenant):
        return {"events": store.events(tenant, run_id)}

    @app.get("/v1/runs/{run_id}/replay")
    def replay(run_id: str, tenant: Tenant):
        return store.replay(tenant, run_id)

    @app.get("/v1/runs/{run_id}/integrity")
    def integrity(run_id: str, tenant: Tenant):
        chain = store.events(tenant, run_id)
        return {"valid": store.verify_events(chain), "event_count": len(chain),
                "head": chain[-1]["hash"] if chain else "0" * 64,
                "external_checkpoint": "not_configured"}

    return app
