"""Execution state-machine tests with simulated model HTTP and simulated sandbox.

FakeSandbox never executes candidate source, uses Docker, or measures real
program correctness. These tests do not read credentials or contact a provider.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from loom.execution import FLASH_CONTEXT_LIMIT, LocalTaskRunner, cost_upper_microusd, freeze_tests
from loom.attempts import attempt_summaries
from loom.model_gateway import ModelGateway, ModelRoute
from loom.operator import OperatorService
from loom.policy import ArtifactSubmission
from loom.sandbox import CaseResult, FrozenTest, RUNNER_SHA256, SandboxResult, SandboxUnavailable
from loom.store import BudgetExceeded, Conflict, InvalidInput, StaleClaim, Store, canonical, digest
from loom.workflow import WorkflowService


PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
FAKE_SECRET = "simulation-only-credential-never-send-as-prompt"
SOURCE = 'import json,sys\nprint(json.dumps({"answer":2}))\n'
_DEFAULT_DIGEST = object()


class FakeSandbox:
    """Test double only: fabricated evidence must never count as real isolation."""

    def __init__(self, *, passed=True, preflight_error=False, execute_error=False, bad_binding=None):
        self.passed = passed
        self.preflight_error, self.execute_error, self.bad_binding = preflight_error, execute_error, bad_binding
        self.preflight_calls, self.execution_calls = 0, 0
        self.inputs = []

    def preflight(self):
        self.preflight_calls += 1
        if self.preflight_error:
            raise SandboxUnavailable("simulated daemon unavailable")
        return {"simulation": "no Docker daemon contacted"}

    def run_python(self, files, tests):
        self.execution_calls += 1
        self.inputs.append((dict(files), tests))
        if self.execute_error:
            raise SandboxUnavailable("simulated isolated executor failure")
        _, suite_digest = freeze_tests(tests)
        cases = tuple(CaseResult(case.name, self.passed, "passed" if self.passed else "mismatch",
                                 0, "SIMULATED OUTPUT", "", digest("simulated"), 0.0) for case in tests)
        return SandboxResult(self.passed, "simulated-image-no-container", digest(files), suite_digest,
                             RUNNER_SHA256, cases) if self.bad_binding is None else SandboxResult(
                                 self.passed, "simulated-image-no-container",
                                 "0" * 64 if self.bad_binding == "source" else digest(files),
                                 "0" * 64 if self.bad_binding == "tests" else suite_digest,
                                 RUNNER_SHA256, cases)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_pause_during_response_stays_paused_in_replay(self):
        claim, _ = self.make_claim()
        self.http_callback = lambda: OperatorService(self.store).pause("tenant", claim.run_id, "pause-response", "subject:operator", "Pause before next action")
        result = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertEqual(result["run_status"], "paused")
        self.assertEqual(self.sandbox.execution_calls, 0)
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "done")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "settled")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["status"], "paused")
        self.assertEqual(self.store.replay("tenant", claim.run_id)["status"], "paused")

    async def test_operator_terminate_during_response_fences_old_candidate(self):
        claim, _ = self.make_claim()
        self.http_callback = lambda: OperatorService(self.store).terminate("tenant", claim.run_id, "cancel-response", "subject:operator", "Cancel in-flight attempt")
        result = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertFalse(result["verified"])
        self.assertTrue(result["billing_uncertain"])
        self.assertEqual(self.sandbox.execution_calls, 0)
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "reserved")
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["status"], "cancelled")
        self.assertEqual(self.store.replay("tenant", claim.run_id)["status"], "cancelled")
        with self.assertRaises(StaleClaim):
            self.store.submit_output(claim, "late-output", {"verified": True})

    async def test_late_overrun_settlement_cannot_revive_cancelled_run(self):
        claim, _ = self.make_claim()
        self.store.reserve(claim, "late-bill", max_cost=10, max_tokens=10)
        OperatorService(self.store).terminate("tenant", claim.run_id, "cancel-late-bill", "subject:operator", "Stop task")
        self.store.settle("tenant", "late-bill", actual_cost=11, actual_tokens=12)
        self.assertEqual(self.store.snapshot("tenant", claim.run_id)["status"], "cancelled")
        self.assertEqual(self.store.replay("tenant", claim.run_id)["status"], "cancelled")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "execution.db")
        self.workflows = WorkflowService(self.store)
        self.suite = (FrozenTest("frozen-addition", {"value": 1}, {"answer": 2, "golden": "held-out-marker"}),)
        self.suite_digest = freeze_tests(self.suite)[1]
        self.sequence = 0
        self.http_requests = []
        self.response_content = json.dumps({"source": SOURCE})
        self.finish_reason = "stop"
        self.provider_error = None
        self.reported_model = "deepseek-flash"
        self.http_callback = None
        self.sandbox = FakeSandbox()
        self.gateway = ModelGateway([ModelRoute("deepseek-flash", timeout_seconds=1)],
                                    transport=httpx.MockTransport(self.http_handler),
                                    environ={"DEEPSEEK_API_KEY": FAKE_SECRET})
        self.addAsyncCleanup(self.gateway.aclose)
        self.runner = LocalTaskRunner(self.store, self.gateway, self.sandbox)

    def http_handler(self, request):
        self.http_requests.append(request)
        if self.http_callback:
            self.http_callback()
        if self.provider_error:
            raise self.provider_error(FAKE_SECRET + " simulated upstream failure", request=request)
        body = {"id": "simulated-completion", "object": "chat.completion", "created": 1792700000,
                "model": self.reported_model,
                "choices": [{"index": 0, "finish_reason": self.finish_reason,
                             "message": {"role": "assistant", "content": self.response_content}}],
                "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35,
                          "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 25}}
        return httpx.Response(200, json=body)

    def make_claim(self, *, role="developer", child=False, evaluation_digest=_DEFAULT_DIGEST,
                   node_cost=1_000_000, node_tokens=2_000_000, goal_cost=None, goal_tokens=None,
                   max_attempts=3, lease_seconds=300):
        self.sequence += 1
        created = self.workflows.create_goal(
            "tenant", "goal-" + str(self.sequence), title="Bounded simulated task",
            description="Return a program implementing the specified arithmetic operation.",
            acceptance=["tests.pass"], cost_limit=goal_cost or node_cost + (100 if child else 0),
            token_limit=goal_tokens or node_tokens + (100 if child else 0))
        definition = json.loads(PACK.read_text(encoding="utf-8"))
        node = {"id": "work", "title": "Implement bounded program", "instruction": "Add one to value.",
                "role": role, "depends": [], "priority": 10, "channel": "business",
                "limits": {"cost_limit_microusd": node_cost, "token_limit": node_tokens, "max_attempts": max_attempts},
                "evaluation_digest": self.suite_digest if evaluation_digest is _DEFAULT_DIGEST else evaluation_digest}
        definition["nodes"] = [node]
        if child:
            definition["nodes"].append({"id": "child", "title": "Dependent review", "instruction": "Inspect parent artifact.",
                                        "role": "reviewer", "depends": ["work"], "channel": "business",
                                        "limits": {"cost_limit_microusd": 100, "token_limit": 100, "max_attempts": 1}})
        saved = self.workflows.save_plan("tenant", created["run_id"], "save-" + str(self.sequence), definition, 0)
        approved = self.workflows.approve_plan("tenant", created["run_id"], "approve-" + str(self.sequence),
                                                saved["revision"], saved["digest"])
        claim = self.store.claim_next("tenant", created["run_id"], "simulated-worker", lease_seconds=lease_seconds)
        self.assertEqual(claim.task_id, approved["task_ids"]["work"])
        return claim, approved

    def rows(self, table, run_id):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE tenant=? AND run_id=?",
                                                   ("tenant", run_id))]

    def events(self, claim, kind):
        return [event for event in self.store.events("tenant", claim.run_id) if event["type"] == kind]

    async def test_simulated_pass_only_submits_without_dual_sign_or_child_release(self):
        claim, approved = self.make_claim(child=True)
        result = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["verified"])
        self.assertTrue(result["hard_criteria_passed"])
        self.assertEqual(result["verification"], "awaiting_independent_models")
        self.assertFalse(result["billed_currency_verified"])
        statuses = {row["id"]: row["status"] for row in self.rows("tasks", claim.run_id)}
        self.assertEqual(statuses[claim.task_id], "submitted")
        self.assertEqual(statuses[approved["task_ids"]["child"]], "pending")
        self.assertIsNone(self.store.claim_next("tenant", claim.run_id, "child-worker"))
        self.assertEqual(self.events(claim, "artifact.verified"), [])
        self.assertEqual(self.events(claim, "task.completed"), [])
        self.assertEqual(len(self.events(claim, "execution.awaiting_verification")), 1)
        evaluated = self.events(claim, "execution.evaluated")[0]["payload"]
        self.assertFalse(evaluated["billed_currency_verified"])
        self.assertEqual((evaluated["host_cases_total"], evaluated["host_cases_passed"]), (1, 1))
        self.assertEqual(result["usage_cost_upper_microusd"], cost_upper_microusd(25, 10))
        self.assertTrue(self.store.verify_events(self.store.events("tenant", claim.run_id)))

    async def test_simulated_golden_failure_remains_gate_v_rejection(self):
        self.sandbox.passed = False
        claim, _ = self.make_claim(child=True)
        result = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["reason"], "hard_criteria_failed")
        self.assertFalse(result["hard_criteria_passed"])
        self.assertFalse(result["verified"])
        self.assertEqual(len(self.events(claim, "gate.failed")), 1)
        self.assertEqual(self.events(claim, "artifact.verified"), [])
        self.assertTrue(all(row["status"] == "pending" for row in self.rows("tasks", claim.run_id)))

    async def test_failed_host_evaluation_exposes_only_bounded_diagnostic_counts(self):
        class TimedOutSandbox(FakeSandbox):
            def run_python(self, files, tests):
                _, suite_digest = freeze_tests(tests)
                case = CaseResult("hidden-case-name", False, "timeout", None,
                                  "private-stdout-marker", "private-stderr-marker", digest("fixture"), 5.0)
                return SandboxResult(False, "fixture-image", digest(files), suite_digest, RUNNER_SHA256, (case,))
        self.runner.sandbox = TimedOutSandbox()
        claim, _ = self.make_claim()
        result = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertFalse(result["verified"])
        event = self.events(claim, "execution.evaluated")[0]["payload"]
        expected = {"host_cases_total": 1, "host_cases_passed": 0, "host_cases_mismatch": 0,
                    "host_cases_timeout": 1, "host_cases_runtime_error": 0, "host_cases_other_failure": 0}
        self.assertEqual({k: event[k] for k in expected}, expected)
        summaries = attempt_summaries(self.store, "tenant", claim.run_id)
        evaluated = next(item for item in summaries if item["event_type"] == "execution.evaluated")
        self.assertEqual({k: evaluated[k] for k in expected}, expected)
        text = json.dumps(evaluated)
        for marker in ("hidden-case-name", "private-stdout-marker", "private-stderr-marker", "held-out-marker"):
            self.assertNotIn(marker, text)

    async def test_wrong_or_unbound_suite_is_rejected_before_dispatch(self):
        for expected in (None, "0" * 64):
            claim, _ = self.make_claim(evaluation_digest=expected)
            with self.subTest(expected=expected), self.assertRaises(Conflict):
                await self.runner.run_python(claim, self.suite)
            self.assertEqual(self.rows("reservations", claim.run_id), [])
        self.assertEqual(self.http_requests, [])
        self.assertEqual(self.sandbox.preflight_calls, 0)
        self.assertEqual(self.sandbox.execution_calls, 0)

    async def test_read_only_role_cannot_reach_generation_or_executor(self):
        claim, _ = self.make_claim(role="reviewer")
        with self.assertRaises(InvalidInput):
            await self.runner.run_python(claim, self.suite)
        self.assertEqual(self.http_requests, [])
        self.assertEqual(self.sandbox.preflight_calls, 0)
        self.assertEqual(self.rows("reservations", claim.run_id), [])

    async def test_daemon_preflight_happens_before_reservation_and_provider_request(self):
        self.sandbox.preflight_error = True
        claim, _ = self.make_claim()
        with self.assertRaises(SandboxUnavailable):
            await self.runner.run_python(claim, self.suite)
        self.assertEqual(self.sandbox.preflight_calls, 1)
        self.assertEqual(self.http_requests, [])
        self.assertEqual(self.rows("reservations", claim.run_id), [])
        self.assertEqual(self.rows("effects", claim.run_id), [])

    async def test_timeout_marks_effect_indeterminate_and_retains_reserved_budget(self):
        self.provider_error = httpx.ReadTimeout
        claim, _ = self.make_claim()
        result = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertTrue(result["billing_uncertain"])
        self.assertEqual(result["reason"], "MODEL_TIMEOUT")
        self.assertEqual(self.workflows.get_goal("tenant", claim.run_id)["status"], "awaiting_human")
        reservation = self.rows("reservations", claim.run_id)[0]
        self.assertEqual(reservation["state"], "reserved")
        self.assertEqual(reservation["tokens"], FLASH_CONTEXT_LIMIT + 128)
        self.assertEqual(reservation["cost"], cost_upper_microusd(FLASH_CONTEXT_LIMIT, 128))
        self.assertIsNone(reservation["actual_cost"])
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")
        self.assertEqual(self.sandbox.execution_calls, 0)
        self.assertEqual(len(self.http_requests), 1)
        self.assertNotIn(FAKE_SECRET, canonical(result))
        with self.assertRaises(Conflict):
            await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertEqual(len(self.http_requests), 1)

    async def test_truncated_or_invalid_json_candidate_cannot_complete(self):
        for content, finish, reason in ((json.dumps({"source": SOURCE}), "length", "MODEL_OUTPUT_TRUNCATED"),
                                        ("not JSON", "stop", "MODEL_SOURCE_INVALID"),
                                        ('{"source":"one","source":"two"}', "stop", "MODEL_SOURCE_INVALID")):
            self.response_content, self.finish_reason = content, finish
            claim, _ = self.make_claim()
            result = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["status"], "awaiting_human")
            self.assertFalse(result["verified"])
            self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "settled")
            self.assertEqual(self.events(claim, "artifact.verified"), [])
        self.assertEqual(self.sandbox.execution_calls, 0)

    async def test_bad_source_or_test_evidence_binding_cannot_submit(self):
        for wrong in ("source", "tests"):
            self.sandbox.bad_binding = wrong
            claim, _ = self.make_claim()
            result = await self.runner.run_python(claim, self.suite)
            self.assertEqual(result["reason"], "EXECUTION_EVIDENCE_BINDING_MISMATCH")
            self.assertEqual(result["status"], "awaiting_human")
            self.assertEqual(self.events(claim, "task.output_submitted"), [])
            self.assertEqual(self.events(claim, "artifact.verified"), [])

    async def test_sandbox_failure_after_known_model_usage_settles_but_does_not_complete(self):
        self.sandbox.execute_error = True
        claim, _ = self.make_claim()
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(result["reason"], "SANDBOX_UNAVAILABLE_OR_REJECTED")
        self.assertFalse(result["billing_uncertain"])
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "settled")
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "done")
        self.assertEqual(self.events(claim, "artifact.verified"), [])

    async def test_model_identity_mismatch_is_not_billed_as_known_or_executed(self):
        self.reported_model = "untrusted-model"
        claim, _ = self.make_claim()
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(result["reason"], "MODEL_IDENTITY_MISMATCH")
        self.assertTrue(result["billing_uncertain"])
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "reserved")
        self.assertEqual(self.sandbox.execution_calls, 0)

    async def test_node_cap_accumulates_over_multiple_reservations_and_attempts(self):
        claim, _ = self.make_claim(goal_cost=2_000_000, goal_tokens=4_000_000)
        self.store.reserve(claim, "manual-first", max_cost=600_000, max_tokens=1_200_000)
        self.store.settle("tenant", "manual-first", actual_cost=600_000, actual_tokens=1_200_000)
        for reservation_id, cost, tokens in (("cost-over", 400_001, 1), ("tokens-over", 1, 800_001)):
            with self.subTest(reservation_id=reservation_id), self.assertRaises(BudgetExceeded):
                self.store.reserve(claim, reservation_id, max_cost=cost, max_tokens=tokens)
        self.store.complete(claim, "reject-attempt", {}, verification=ArtifactSubmission(
            digest({}), "kernel-v1", f"{claim.task_id}:{claim.epoch}", "simulated://failure", True, False))
        next_claim = self.store.claim_next("tenant", claim.run_id, "next-worker", lease_seconds=300)
        self.assertEqual(next_claim.epoch, 2)
        with self.assertRaises(BudgetExceeded):
            await self.runner.run_python(next_claim, self.suite, max_output_tokens=128)
        self.assertEqual(self.http_requests, [])
        self.assertEqual(len(self.rows("reservations", claim.run_id)), 1)

    async def test_attempt_limit_stops_before_another_provider_request(self):
        self.sandbox.passed = False
        claim, _ = self.make_claim(max_attempts=1)
        rejected = await self.runner.run_python(claim, self.suite, max_output_tokens=128)
        self.assertEqual(rejected["status"], "pending")
        next_claim = self.store.claim_next("tenant", claim.run_id, "retry-worker", lease_seconds=300)
        self.assertEqual(next_claim.epoch, 2)
        result = await self.runner.run_python(next_claim, self.suite, max_output_tokens=128)
        self.assertEqual(result["reason"], "TASK_ATTEMPTS_EXHAUSTED")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(self.sandbox.preflight_calls, 1)

    async def test_credentials_and_golden_expectations_never_enter_prompt(self):
        claim, _ = self.make_claim()
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(len(self.http_requests), 1)
        request = self.http_requests[0]
        self.assertEqual(request.headers["authorization"], "Bearer " + FAKE_SECRET)
        payload = json.loads(request.content)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        prompt = payload["messages"]
        self.assertNotIn(FAKE_SECRET, canonical(prompt))
        self.assertNotIn("held-out-marker", canonical(prompt))
        self.assertNotIn(FAKE_SECRET, canonical(result))
        self.assertNotIn(FAKE_SECRET, canonical(self.store.events("tenant", claim.run_id)))
        self.assertNotIn(FAKE_SECRET, canonical(self.rows("effects", claim.run_id)))
        self.assertEqual(self.sandbox.inputs[0][0], {"main.py": SOURCE})

    async def test_default_thirty_second_claim_is_renewed_before_model_dispatch(self):
        claim, _ = self.make_claim(lease_seconds=30)

        def inspect_live_lease():
            task = self.rows("tasks", claim.run_id)[0]
            self.assertEqual(task["epoch"], claim.epoch)
            self.assertEqual(task["worker"], claim.worker_id)
            self.assertGreaterEqual(task["lease_until"], claim.lease_until + 59)

        self.http_callback = inspect_live_lease
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(result["status"], "submitted")
        renewed = self.events(claim, "task.lease_renewed")
        self.assertEqual(len(renewed), 1)
        self.assertEqual(renewed[0]["payload"]["epoch"], claim.epoch)

    async def test_expired_claim_cannot_renew_or_dispatch(self):
        claim, _ = self.make_claim()
        self.store.now = lambda db: claim.lease_until + 1
        with self.assertRaises(StaleClaim):
            self.store.renew_claim(claim, lease_seconds=90)
        with self.assertRaises(StaleClaim):
            await self.runner.run_python(claim, self.suite)
        self.assertEqual(self.events(claim, "task.lease_renewed"), [])
        self.assertEqual(self.http_requests, [])
        self.assertEqual(self.rows("reservations", claim.run_id), [])

    async def test_heartbeat_failure_marks_inflight_provider_effect_without_refund_or_redispatch(self):
        claim, _ = self.make_claim()
        dispatched = asyncio.Event()
        never_release = asyncio.Event()

        async def waiting_provider(request):
            self.http_requests.append(request)
            dispatched.set()
            await never_release.wait()
            self.fail("simulated cancelled HTTP should not finish")

        gateway = ModelGateway([ModelRoute("deepseek-flash")], transport=httpx.MockTransport(waiting_provider),
                               environ={"DEEPSEEK_API_KEY": FAKE_SECRET})
        self.addAsyncCleanup(gateway.aclose)
        runner = LocalTaskRunner(self.store, gateway, self.sandbox)
        original = self.store.renew_claim
        renewals = []

        def renewal(current, **kwargs):
            renewals.append(current.epoch)
            if len(renewals) > 1:
                raise StaleClaim("simulated heartbeat lost ownership")
            return original(current, **kwargs)

        async def wait_until_dispatched(_):
            await dispatched.wait()

        self.store.renew_claim = renewal
        with patch("loom.execution.asyncio.sleep", new=wait_until_dispatched):
            result = await asyncio.wait_for(runner.run_python(claim, self.suite), timeout=2)
        self.assertEqual(result["reason"], "EXECUTION_LEASE_LOST")
        self.assertTrue(result["billing_uncertain"])
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "reserved")
        self.assertEqual(self.workflows.get_goal("tenant", claim.run_id)["status"], "awaiting_human")
        self.assertEqual(self.sandbox.execution_calls, 0)
        with self.assertRaises(StaleClaim):
            await runner.run_python(claim, self.suite)
        self.assertEqual(len(self.http_requests), 1)

    async def test_old_attempt_does_not_pause_or_modify_new_owner_when_effect_is_unknown(self):
        claim, _ = self.make_claim()

        def replace_owner():
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET epoch=epoch+1,worker='replacement' WHERE tenant=? AND id=?",
                           (claim.tenant_id, claim.task_id))

        self.http_callback = replace_owner
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(result["reason"], "EXECUTION_LEASE_LOST")
        task = self.rows("tasks", claim.run_id)[0]
        self.assertEqual((task["status"], task["epoch"], task["worker"]), ("running", claim.epoch + 1, "replacement"))
        self.assertEqual(self.workflows.get_goal("tenant", claim.run_id)["status"], "open")
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "reserved")
        self.assertEqual(self.sandbox.execution_calls, 0)

    async def test_scheduler_expiry_before_late_response_still_freezes_run_and_reports_unknown_billing(self):
        claim, _ = self.make_claim()

        def scheduler_expires_inflight_attempt():
            # Simulate a process pause past the renewed lease. The normal
            # scheduler expiry path clears owner and changes intent first.
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET lease_until=0 WHERE tenant=? AND id=?",
                           (claim.tenant_id, claim.task_id))
            self.assertIsNone(self.store.claim_next(claim.tenant_id, claim.run_id, "other-scheduler"))
            task = self.rows("tasks", claim.run_id)[0]
            self.assertEqual((task["status"], task["epoch"], task["worker"]), ("indeterminate", claim.epoch, None))
            self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")

        self.http_callback = scheduler_expires_inflight_attempt
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(result["reason"], "EXECUTION_LEASE_LOST")
        self.assertTrue(result["billing_uncertain"])
        self.assertEqual(self.workflows.get_goal("tenant", claim.run_id)["status"], "awaiting_human")
        task = self.rows("tasks", claim.run_id)[0]
        self.assertEqual((task["status"], task["epoch"], task["worker"]), ("awaiting_human", claim.epoch, None))
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "reserved")
        self.assertEqual(len(self.events(claim, "tool.indeterminate")), 1)
        self.assertEqual(self.sandbox.execution_calls, 0)
        with self.assertRaises(StaleClaim):
            await self.runner.run_python(claim, self.suite)
        self.assertEqual(len(self.http_requests), 1)

    async def test_pause_during_model_response_settles_known_usage_without_starting_sandbox(self):
        claim, _ = self.make_claim()

        def pause_before_response():
            with self.store.transaction() as db:
                db.execute("UPDATE runs SET status='awaiting_human' WHERE tenant=? AND id=?",
                           (claim.tenant_id, claim.run_id))

        self.http_callback = pause_before_response
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["reason"], "EXECUTION_RUN_PAUSED")
        self.assertFalse(result["billing_uncertain"])
        self.assertEqual(self.sandbox.execution_calls, 0)
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "done")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "settled")
        self.assertEqual(self.events(claim, "task.output_submitted"), [])
        self.assertEqual(self.events(claim, "artifact.verified"), [])

    async def test_pause_after_effect_grant_prevents_http_and_retains_unknown_reservation(self):
        claim, _ = self.make_claim()
        original = self.store.reserve_and_begin_effect

        def grant_then_pause(current, *args, **kwargs):
            granted = original(current, *args, **kwargs)
            with self.store.transaction() as db:
                db.execute("UPDATE runs SET status='awaiting_human' WHERE tenant=? AND id=?",
                           (current.tenant_id, current.run_id))
            return granted

        self.store.reserve_and_begin_effect = grant_then_pause
        result = await self.runner.run_python(claim, self.suite)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(result["reason"], "EXECUTION_RUN_PAUSED")
        self.assertTrue(result["billing_uncertain"])
        self.assertEqual(self.http_requests, [])
        self.assertEqual(self.sandbox.execution_calls, 0)
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "reserved")
        self.assertEqual(self.events(claim, "task.output_submitted"), [])

    async def test_parent_cancellation_marks_inflight_billing_unknown_immediately(self):
        claim, _ = self.make_claim()
        dispatched = asyncio.Event()
        never_release = asyncio.Event()

        async def waiting_provider(request):
            self.http_requests.append(request)
            dispatched.set()
            await never_release.wait()
            self.fail("simulated cancelled HTTP should not finish")

        gateway = ModelGateway([ModelRoute("deepseek-flash")], transport=httpx.MockTransport(waiting_provider),
                               environ={"DEEPSEEK_API_KEY": FAKE_SECRET})
        self.addAsyncCleanup(gateway.aclose)
        runner = LocalTaskRunner(self.store, gateway, self.sandbox)
        work = asyncio.create_task(runner.run_python(claim, self.suite))
        await asyncio.wait_for(dispatched.wait(), timeout=2)
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertEqual(self.rows("effects", claim.run_id)[0]["state"], "indeterminate")
        self.assertEqual(self.rows("reservations", claim.run_id)[0]["state"], "reserved")
        self.assertEqual(self.workflows.get_goal("tenant", claim.run_id)["status"], "awaiting_human")
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(self.sandbox.execution_calls, 0)


class FrozenSuiteTests(unittest.TestCase):
    def test_freeze_copies_mutable_values_and_binds_expectations(self):
        incoming, expected = {"x": [1]}, {"y": [2]}
        frozen, original = freeze_tests([FrozenTest("case", incoming, expected)])
        incoming["x"].append(3)
        expected["y"].append(4)
        self.assertEqual(frozen[0].input, {"x": [1]})
        self.assertEqual(frozen[0].expected, {"y": [2]})
        self.assertNotEqual(original, freeze_tests([FrozenTest("case", incoming, expected)])[1])

    def test_unbounded_nonfinite_or_ambiguous_frozen_suite_is_rejected(self):
        for suite in ([], [FrozenTest("duplicate", 1, 2)] * 2, [FrozenTest("unsafe/name", 1, 2)],
                      [FrozenTest("nan", 1, float("nan"))], [FrozenTest("large", 1, "x" * 20000)]):
            with self.subTest(suite=suite), self.assertRaises(InvalidInput):
                freeze_tests(suite)
