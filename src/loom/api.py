"""Tenant-key authenticated LOCAL API; OIDC and worker role separation are future work."""
from dataclasses import asdict
from hmac import compare_digest
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .store import Claim, Conflict, InvalidInput, LoomError, NotFound, Store, canonical
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


class ClaimInput(Input):
    worker_id: str = Field(min_length=1, max_length=100)
    channel: str = "business"
    lease_seconds: float = Field(default=30, gt=0, le=3600)


class CompleteInput(Input):
    worker_id: str = Field(min_length=1, max_length=100)
    epoch: int = Field(ge=1)
    output: dict[str, Any]

    @field_validator("output")
    @classmethod
    def bounded_json(cls, value):
        if len(canonical(value).encode("utf-8")) > 262144:
            raise ValueError("local artifact body exceeds 256 KiB; use a content-addressed object adapter")
        return value


def create_app(store: Store, tenant_keys: dict[str, str]) -> FastAPI:
    if not tenant_keys or any(not t or len(k) < 16 for t, k in tenant_keys.items()):
        raise ValueError("configure tenant keys with at least 16 characters")
    if len(set(tenant_keys.values())) != len(tenant_keys):
        raise ValueError("tenant keys must be unique")
    app = FastAPI(title="Loom local kernel", version=__version__,
                  description="Local S1 development API. No real model or external write execution.")
    workflows = WorkflowService(store)
    security = HTTPBearer(auto_error=False)

    def principal(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]) -> str:
        if credentials is not None:
            for tenant, token in tenant_keys.items():
                if compare_digest(credentials.credentials.encode("utf-8"), token.encode("utf-8")):
                    return tenant
        raise HTTPException(401, "valid tenant bearer token required", headers={"WWW-Authenticate": "Bearer"})

    Tenant = Annotated[str, Depends(principal)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]

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
    def create_goal(body: GoalInput, tenant: Tenant, idempotency_key: Key):
        return workflows.create_goal(tenant, idempotency_key, title=body.title, description=body.description,
                                     acceptance=body.acceptance, cost_limit=body.cost_limit_microusd,
                                     token_limit=body.token_limit)

    @app.get("/v1/goals")
    def list_goals(tenant: Tenant):
        return {"goals": workflows.list_goals(tenant)}

    @app.get("/v1/goals/{run_id}")
    def get_goal(run_id: str, tenant: Tenant):
        return workflows.get_goal(tenant, run_id)

    @app.get("/v1/goals/{run_id}/plan")
    def get_plan(run_id: str, tenant: Tenant):
        return workflows.get_plan(tenant, run_id)

    @app.put("/v1/goals/{run_id}/plan")
    def save_plan(run_id: str, body: PlanInput, tenant: Tenant, idempotency_key: Key):
        return workflows.save_plan(tenant, run_id, idempotency_key, body.definition, body.expected_revision)

    @app.post("/v1/goals/{run_id}/plan/approve")
    def approve_plan(run_id: str, body: PlanApprovalInput, tenant: Tenant, idempotency_key: Key):
        return workflows.approve_plan(tenant, run_id, idempotency_key, body.expected_revision, body.expected_digest)

    @app.post("/v1/runs", status_code=201)
    def create_run(body: RunInput, tenant: Tenant, idempotency_key: Key):
        return store.create_run(tenant, idempotency_key, acceptance=body.acceptance,
                                cost_limit=body.cost_limit_microusd, token_limit=body.token_limit, ruleset=body.ruleset)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, tenant: Tenant):
        return store.snapshot(tenant, run_id)

    @app.post("/v1/runs/{run_id}/tasks", status_code=201)
    def add_task(run_id: str, body: TaskInput, tenant: Tenant, idempotency_key: Key):
        return store.add_task(tenant, run_id, idempotency_key, **body.model_dump())

    @app.post("/v1/runs/{run_id}/claim")
    def claim(run_id: str, body: ClaimInput, tenant: Tenant):
        result = store.claim_next(tenant, run_id, body.worker_id, channel=body.channel, lease_seconds=body.lease_seconds)
        return {"claim": asdict(result) if result else None}

    @app.post("/v1/runs/{run_id}/tasks/{task_id}/submit")
    def submit(run_id: str, task_id: str, body: CompleteInput, tenant: Tenant, idempotency_key: Key):
        current = Claim(tenant, run_id, task_id, body.worker_id, body.epoch, 0)
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
