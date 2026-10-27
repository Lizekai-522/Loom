from dataclasses import FrozenInstanceError, replace
import unittest

from loom.policy import (
    AgentCandidate,
    ArtifactSubmission,
    ContextItem,
    ContextOverflow,
    GateResult,
    GateRules,
    SchedulableTask,
    TaskContract,
    VerificationSignature,
    assemble_context,
    classify_progress,
    gate_p,
    gate_v,
    manual_accept,
    select_agent,
    select_task,
)


def good_artifact():
    signatures = tuple(
        VerificationSignature(
            verifier_id=f"verifier-{i}",
            model_id=f"model-{i}",
            artifact_hash="sha256:content",
            ruleset_version="loom/v1.0",
            evidence_revision="evidence-v1",
            passed=True,
            authenticated=True,
        )
        for i in range(2)
    )
    return ArtifactSubmission(
        content_hash="sha256:content",
        ruleset_version="loom/v1.0",
        evidence_revision="evidence-v1",
        evidence_ref="evidence:test-result",
        evidence_trusted=True,
        hard_criteria_passed=True,
        dual_sign="signed",
        signatures=signatures,
    )


class TaskBookTests(unittest.TestCase):
    def setUp(self):
        self.contract = TaskContract("task-1", "run-1", ("repo:demo",), "goal-1", ("tests.pass",), "anchor-1")

    def check_gate(self, contract):
        return gate_p(contract, allowed_scope=frozenset({"repo:demo"}), known_anchors=frozenset({"anchor-1"}), known_criteria=frozenset({"tests.pass"}))

    def test_valid_task_has_one_explicit_goal_and_registered_anchor(self):
        self.assertTrue(self.check_gate(self.contract).passed)

    def test_missing_required_field_rejected(self):
        for field in ("task_id", "run_id", "goal_id", "anchor_ref"):
            with self.subTest(field=field):
                self.assertFalse(self.check_gate(replace(self.contract, **{field: " "})).passed)

    def test_missing_scope_or_predicate_rejected(self):
        for field in ("scope", "output_criteria"):
            with self.subTest(field=field):
                self.assertFalse(self.check_gate(replace(self.contract, **{field: ()})).passed)

    def test_schema_and_scope_authority_enforced(self):
        self.assertEqual(self.check_gate(replace(self.contract, schema_version=2)).reason, "unsupported_schema_version")
        self.assertEqual(self.check_gate(replace(self.contract, scope=("repo:other",))).reason, "scope_denied")
        self.assertEqual(gate_p(self.contract).reason, "scope_authority_missing")

    def test_unknown_anchor_and_duplicate_predicates_rejected(self):
        self.assertEqual(self.check_gate(replace(self.contract, anchor_ref="other")).reason, "anchor_unresolvable")
        self.assertEqual(self.check_gate(replace(self.contract, output_criteria=("tests.pass", "tests.pass"))).reason, "duplicate_output_criteria")

    def test_unregistered_predicate_is_rejected(self):
        self.assertEqual(self.check_gate(replace(self.contract, output_criteria=("model.says.pass",))).reason, "criterion_unregistered")

    def test_malformed_collection_is_rejected_without_evaluating_it(self):
        self.assertEqual(self.check_gate(replace(self.contract, scope=(["not-a-string"],))).reason, "invalid_scope")

    def test_rules_are_immutable(self):
        rules = GateRules()
        with self.assertRaises(FrozenInstanceError):
            rules.require_dual_sign = False


class VerificationTests(unittest.TestCase):
    def test_two_bound_authenticated_independent_signatures_pass(self):
        self.assertTrue(gate_v(good_artifact()).passed)

    def test_review_r01_all_non_signed_states_are_rejected(self):
        for state in ("pending", "disputed", "blocked", "single", "", "unexpected"):
            with self.subTest(state=state):
                self.assertFalse(gate_v(replace(good_artifact(), dual_sign=state)).passed)

    def test_signed_label_without_two_signatures_is_not_enough(self):
        a = good_artifact()
        self.assertEqual(gate_v(replace(a, signatures=a.signatures[:1])).reason, "insufficient_signatures")

    def test_changed_content_rules_or_evidence_invalidates_old_signatures(self):
        a = good_artifact()
        for field, value, expected in (
            ("content_hash", "sha256:changed", "signature_artifact_mismatch"),
            ("evidence_revision", "evidence-v2", "signature_evidence_mismatch"),
            ("ruleset_version", "loom/v2.0", "ruleset_version_mismatch"),
        ):
            with self.subTest(field=field):
                self.assertEqual(gate_v(replace(a, **{field: value})).reason, expected)

    def test_duplicate_verifier_and_same_model_cannot_satisfy_independence(self):
        a = good_artifact()
        duplicate = replace(a.signatures[1], verifier_id=a.signatures[0].verifier_id)
        self.assertEqual(gate_v(replace(a, signatures=(a.signatures[0], duplicate))).reason, "duplicate_verifier")
        same_model = replace(a.signatures[1], model_id=a.signatures[0].model_id)
        self.assertEqual(gate_v(replace(a, signatures=(a.signatures[0], same_model))).reason, "independent_model_required")

    def test_untrusted_evidence_unauthenticated_signature_and_failed_judge_rejected(self):
        a = good_artifact()
        self.assertEqual(gate_v(replace(a, evidence_trusted=False)).reason, "untrusted_evidence")
        self.assertEqual(gate_v(replace(a, hard_criteria_passed=False)).reason, "hard_criteria_failed")
        bad = replace(a.signatures[0], authenticated=False)
        self.assertEqual(gate_v(replace(a, signatures=(bad, a.signatures[1]))).reason, "unauthenticated_signature")
        rejected = replace(a.signatures[0], passed=False)
        self.assertEqual(gate_v(replace(a, signatures=(rejected, a.signatures[1]))).reason, "verification_rejected")

    def test_signature_ruleset_binding_is_checked(self):
        a = good_artifact()
        stale = replace(a.signatures[0], ruleset_version="old")
        self.assertEqual(gate_v(replace(a, signatures=(stale, a.signatures[1]))).reason, "signature_ruleset_mismatch")

    def test_low_risk_single_signature_requires_explicit_policy(self):
        a = good_artifact()
        single = replace(a, verification_required=False, dual_sign="single", signatures=a.signatures[:1])
        self.assertFalse(gate_v(single).passed)
        self.assertTrue(gate_v(single, rules=GateRules(require_dual_sign=False)).passed)


class AcceptanceAndProgressTests(unittest.TestCase):
    def test_review_r02_failed_hard_gate_never_becomes_normal_success(self):
        failed = GateResult(False, "hard_criteria_failed")
        decision = manual_accept(failed, requester_id="author", reviewer_id="reviewer", reason="Risk accepted", allow_exception=True)
        self.assertEqual(decision.state, "accepted_with_exception")
        self.assertFalse(decision.counts_as_success)
        refused = manual_accept(failed, requester_id="author", reviewer_id="reviewer", reason="Please accept")
        self.assertEqual(refused.state, "rejected")

    def test_self_approval_and_high_risk_single_review_rejected(self):
        passed = GateResult(True, "ok")
        self.assertEqual(manual_accept(passed, requester_id="a", reviewer_id="a", reason="ok").reason, "self_approval_denied")
        self.assertEqual(manual_accept(passed, requester_id="a", reviewer_id="b", reason="ok", high_risk=True).reason, "independent_second_review_required")
        accepted = manual_accept(passed, requester_id="a", reviewer_id="b", second_reviewer_id="c", reason="Checked", high_risk=True)
        self.assertEqual(accepted.state, "verified")
        self.assertTrue(accepted.counts_as_success)

    def test_truthy_nonboolean_gate_result_does_not_count_as_success(self):
        malformed = GateResult("false", "malformed")
        decision = manual_accept(malformed, requester_id="author", reviewer_id="reviewer", reason="Review")
        self.assertFalse(decision.counts_as_success)
        self.assertEqual(decision.state, "rejected")

    def test_review_r13_idle_unsatisfied_task_is_stalled_not_succeeded(self):
        decision = classify_progress(acceptance_satisfied=False, idle_rounds=2, max_idle_rounds=2)
        self.assertEqual(decision.state, "stalled")
        self.assertFalse(decision.counts_as_success)

    def test_only_acceptance_satisfaction_yields_success(self):
        self.assertEqual(classify_progress(acceptance_satisfied=False, idle_rounds=0, max_idle_rounds=2).state, "running")
        self.assertEqual(classify_progress(acceptance_satisfied=False, idle_rounds=0, max_idle_rounds=2, budget_exhausted=True).state, "budget_exhausted")
        self.assertTrue(classify_progress(acceptance_satisfied=True, idle_rounds=2, max_idle_rounds=2).counts_as_success)

    def test_invalid_idle_limits_rejected(self):
        with self.assertRaises(ValueError):
            classify_progress(acceptance_satisfied=False, idle_rounds=0, max_idle_rounds=0)


class SchedulingTests(unittest.TestCase):
    def test_priority_applies_to_task_choice_and_dependency_readiness(self):
        low = SchedulableTask("low", priority=1, sequence=0)
        high = SchedulableTask("high", priority=9, sequence=1)
        blocked = SchedulableTask("blocked", priority=99, sequence=2, ready=False)
        self.assertEqual(select_task([low, blocked, high]), high)
        self.assertIsNone(select_task([blocked]))

    def test_waiting_time_ageing_can_prevent_static_priority_starvation(self):
        old = SchedulableTask("old", priority=0, sequence=0, wait_ticks=100)
        new = SchedulableTask("new", priority=5, sequence=1)
        self.assertEqual(select_task([new, old]), old)

    def test_agent_filtering_capacity_skills_and_deterministic_tie(self):
        eligible = AgentCandidate("b", frozenset({"code"}), 2, 0)
        tied = replace(eligible, agent_id="a")
        full = replace(eligible, agent_id="full", active_load=2)
        wrong = replace(eligible, agent_id="wrong", skills=frozenset({"test"}))
        selected = select_agent([full, wrong, eligible, tied], required_skills=frozenset({"code"}))
        self.assertEqual(selected.agent_id, "a")
        self.assertAlmostEqual(selected.score, sum(v for _, v in selected.parts))


class ContextTests(unittest.TestCase):
    def test_review_r20_required_overflow_is_explicit(self):
        with self.assertRaises(ContextOverflow) as caught:
            assemble_context([ContextItem("anchor", 101, required=True)], 100)
        self.assertEqual(caught.exception.required_tokens, 101)
        self.assertEqual(caught.exception.token_budget, 100)

    def test_required_items_survive_and_all_drops_are_returned(self):
        anchor = ContextItem("anchor", 30, required=True)
        high = ContextItem("high", 50, relevance=0.9)
        low = ContextItem("low", 40, relevance=0.1)
        bundle = assemble_context([low, anchor, high], 100)
        self.assertEqual(bundle.retained, (anchor, high))
        self.assertEqual(bundle.dropped, (low,))
        self.assertEqual(bundle.token_count, 80)

    def test_duplicate_evidence_ref_is_rejected(self):
        with self.assertRaises(ValueError):
            assemble_context([ContextItem("same", 10), ContextItem("same", 10)], 100)


if __name__ == "__main__":
    unittest.main()
