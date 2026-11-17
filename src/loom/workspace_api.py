"""Authenticated user workspace endpoints and lifespan-bound schedule dispatcher."""
import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import is_human
from .workspace import Workspace


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ProjectBody(Body):
    name: str = Field(min_length=1, max_length=120)


class PreferencesBody(Body):
    appearance: str | None = None
    font: str | None = None
    font_size: int | None = None
    shortcuts: str | None = None
    confirmation_mode: str | None = None
    preferred_model: str | None = None
    selected_project_id: str | None = None


class AutomationBody(Body):
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)
    project_id: str | None = None
    model: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)
    due_at: str | float | None = None
    mode: Literal['off', 'observe', 'auto'] = 'observe'


class AutomationPatch(Body):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    project_id: str | None = None
    model: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)
    due_at: str | float | None = None
    mode: Literal['off', 'observe', 'auto'] | None = None


class WorktreeBody(Body):
    run_id: str = Field(min_length=1, max_length=80)
    title: str = Field(default='task', min_length=1, max_length=200)


def install_workspace_api(app, store, *, Reader, Writer, executor_config=None,
                          executor_config_path=None, scheduler=True):
    """Use the parent API's annotated authentication dependencies unchanged."""
    workspace = Workspace(store, executor_config=executor_config, executor_config_path=executor_config_path)
    app.state.workspace = workspace
    app.state.workspace_scheduler_error = None

    def human(identity):
        if not is_human(identity):
            raise HTTPException(403, 'workspace changes require a named human user')
        return identity

    @app.get('/v1/workspace/models')
    def models(identity: Reader):
        return workspace.models()

    @app.get('/v1/workspace/preferences')
    def preferences(identity: Reader):
        return workspace.preferences(identity)

    @app.patch('/v1/workspace/preferences')
    def update_preferences(body: PreferencesBody, identity: Writer):
        return workspace.update_preferences(human(identity), body.model_dump(exclude_unset=True))

    @app.get('/v1/workspace/projects')
    def projects(identity: Reader):
        return dict(items=workspace.projects(identity))

    @app.post('/v1/workspace/projects', status_code=201)
    def add_project(body: ProjectBody, identity: Writer):
        return workspace.add_project(human(identity), body.name)

    @app.get('/v1/workspace/projects/{project_id}')
    def project(project_id: str, identity: Reader):
        return workspace.project(identity, project_id)

    @app.patch('/v1/workspace/projects/{project_id}')
    def rename_project(project_id: str, body: ProjectBody, identity: Writer):
        return workspace.update_project(human(identity), project_id, body.name)

    @app.delete('/v1/workspace/projects/{project_id}')
    def delete_project(project_id: str, identity: Writer):
        return workspace.delete_project(human(identity), project_id)

    @app.get('/v1/workspace/automations')
    def automations(identity: Reader):
        return dict(items=workspace.automations(identity))

    @app.post('/v1/workspace/automations', status_code=201)
    def add_automation(body: AutomationBody, identity: Writer):
        return workspace.add_automation(human(identity), body.model_dump())

    @app.get('/v1/workspace/automations/{automation_id}')
    def automation(automation_id: str, identity: Reader):
        return workspace.automation(identity, automation_id)

    @app.patch('/v1/workspace/automations/{automation_id}')
    def update_automation(automation_id: str, body: AutomationPatch, identity: Writer):
        return workspace.update_automation(human(identity), automation_id, body.model_dump(exclude_unset=True))

    @app.delete('/v1/workspace/automations/{automation_id}')
    def delete_automation(automation_id: str, identity: Writer):
        return workspace.delete_automation(human(identity), automation_id)

    @app.post('/v1/workspace/automations/{automation_id}/enable')
    def enable(automation_id: str, identity: Writer):
        return workspace.enable_automation(human(identity), automation_id, True)

    @app.post('/v1/workspace/automations/{automation_id}/disable')
    def disable(automation_id: str, identity: Writer):
        return workspace.enable_automation(human(identity), automation_id, False)

    @app.post('/v1/workspace/automations/{automation_id}/run-now', status_code=201)
    def run_now(automation_id: str, identity: Writer,
                idempotency_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=200)):
        return workspace.run_automation(human(identity), automation_id, idempotency_key)

    @app.get('/v1/workspace/automations/{automation_id}/runs')
    def runs(automation_id: str, identity: Reader):
        return dict(items=workspace.automation_runs(identity, automation_id))

    @app.get('/v1/workspace/worktrees')
    def list_worktrees(identity: Reader, project_id: str | None = None):
        return dict(items=workspace.worktrees(identity, project_id))

    @app.post('/v1/workspace/projects/{project_id}/worktrees', status_code=201)
    def add_worktree(project_id: str, body: WorktreeBody, identity: Writer):
        return workspace.ensure_worktree(human(identity), project_id, body.run_id, title=body.title, is_human_request=True)

    @app.get('/v1/workspace/worktrees/{worktree_id}')
    def one_worktree(worktree_id: str, identity: Reader):
        return workspace.worktree(identity, worktree_id)

    @app.get('/v1/workspace/worktrees/{worktree_id}/diff')
    def worktree_diff(worktree_id: str, identity: Reader):
        return workspace.worktree_diff(identity, worktree_id)

    @app.post('/v1/workspace/worktrees/{worktree_id}/apply')
    def apply_worktree(worktree_id: str, identity: Writer):
        return workspace.apply_worktree(human(identity), worktree_id)

    @app.delete('/v1/workspace/worktrees/{worktree_id}')
    def delete_worktree(worktree_id: str, identity: Writer):
        return workspace.remove_worktree(human(identity), worktree_id)

    if scheduler:
        previous_lifespan = app.router.lifespan_context

        async def dispatch_loop():
            while True:
                try:
                    await asyncio.to_thread(workspace.dispatch_due)
                    app.state.workspace_scheduler_error = None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Keep a visible health code without logging prompts, paths,
                    # SQLite parameters, credentials or user-controlled text.
                    app.state.workspace_scheduler_error = 'AUTOMATION_DISPATCH_FAILED'
                    logging.getLogger('loom.workspace').warning('AUTOMATION_DISPATCH_FAILED')
                await asyncio.sleep(0.5)

        @asynccontextmanager
        async def lifespan(application):
            async with previous_lifespan(application) as state:
                task = asyncio.create_task(dispatch_loop())
                try:
                    yield state
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        app.router.lifespan_context = lifespan
    return workspace
