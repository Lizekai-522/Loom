"""Re-execute an already accepted frozen program; never generate or edit code."""
import asyncio

from .execution import LocalTaskRunner, freeze_tests, _host_case_counts
from .sandbox import SandboxError
from .store import Conflict, InvalidInput, digest
from .tool_registry import PYTHON_TESTS


class ValidatorTaskRunner:
    def __init__(self,store,sandbox):
        self.runner=LocalTaskRunner(store,None,sandbox)
        self.store=store

    async def run(self,claim,tests):
        return await self.runner.run_owned(claim,lambda:self._run(claim,tests))

    async def _run(self,claim,tests):
        runner=self.runner;spec=runner.workflows.get_task_spec(claim.tenant_id,claim.run_id,claim.task_id)
        frozen,evaluation_digest=freeze_tests(tests)
        if (spec['role']!='validator' or not spec.get('validation_source') or spec['evaluation_digest']!=evaluation_digest
                or not {'workspace.read','tests.run','artifacts.read','artifacts.write'}<=set(spec['tools'])
                or claim.epoch!=1 or spec['limits']['max_attempts']!=1):
            raise InvalidInput('validator requires one frozen, read-only program validation attempt')
        snapshot=runner.artifacts.capture_execution_context(claim)
        if snapshot is None or not snapshot.get('validation_binding'):raise Conflict('VALIDATION_SOURCE_REQUIRED')
        view=snapshot['review_input'];source=view['files']['main.py']
        try:await asyncio.to_thread(runner.sandbox.preflight)
        except SandboxError:return runner._block(claim,'SANDBOX_UNAVAILABLE_OR_REJECTED')
        def execute():
            runner._assert_execution(claim);runner._assert_context(claim,snapshot)
            return runner._execute_tool(claim,spec,snapshot,{'main.py':source},frozen,phase='acceptance')
        try:evaluated=await asyncio.to_thread(execute)
        except SandboxError:return runner._block(claim,'SANDBOX_UNAVAILABLE_OR_REJECTED')
        runner._assert_execution(claim);runner._assert_context(claim,snapshot)
        if evaluated.source_sha256!=view['subject']['source_sha256'] or evaluated.tests_sha256!=evaluation_digest:
            return runner._block(claim,'EXECUTION_EVIDENCE_BINDING_MISMATCH')
        output=dict(kind='python-json-program',source=source,evaluation_digest=evaluation_digest,
            source_sha256=evaluated.source_sha256,plan_digest=spec['plan_digest'],tests=evaluated.to_dict(),
            validation_source=snapshot['validation_binding'],generation_model_used=False,**runner._context_fields(snapshot))
        with self.store.transaction() as db:
            self.store._assert_claim(db,claim);runner.artifacts.assert_execution_context_in(db,claim,snapshot)
            self.store._event(db,claim.tenant_id,claim.run_id,'execution.evaluated',dict(task_id=claim.task_id,epoch=claim.epoch,
                content_hash=digest(output),tool_id=PYTHON_TESTS.tool_id,tool_version=PYTHON_TESTS.version,
                evaluation_digest=evaluation_digest,hard_criteria_passed=evaluated.passed,**_host_case_counts(evaluated),
                generation_model_used=False,usage_cost_upper_microusd=0,**runner._context_fields(snapshot)),'local-executor')
            if not evaluated.passed:
                self.store._event(db,claim.tenant_id,claim.run_id,'execution.acceptance_failed',dict(task_id=claim.task_id,
                    epoch=claim.epoch,content_hash=digest(output),acceptance_report=evaluated.to_dict()),'local-executor')
                return {**runner._block_in(db,claim,'ACCEPTANCE_TESTS_FAILED'),
                    'hard_criteria_passed':False,'generation_model_used':False,'usage_cost_upper_microusd':0}
        submitted=runner._submit_output(claim,'validator-submit:'+claim.task_id+':1',output,snapshot)
        with self.store.transaction() as db:
            self.store._event(db,claim.tenant_id,claim.run_id,'execution.awaiting_verification',
                dict(task_id=claim.task_id,epoch=claim.epoch,reason='INDEPENDENT_MODEL_REVIEW_REQUIRED'),'local-executor')
        return {**submitted,'verified':False,'hard_criteria_passed':True,'generation_model_used':False,
                'usage_cost_upper_microusd':0,'verification':'awaiting_independent_models'}
