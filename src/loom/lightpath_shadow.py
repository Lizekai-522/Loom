"""Offline shadow assessment; never authorizes or changes execution."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Assessment:
    recommendation: str
    reason: str
    enforcement: bool = False


def assess(*, trusted_policy: bool, frozen_suite: bool, complete_spec: bool,
           side_effect_free: bool, semantic_review_required: bool,
           budget_ok: bool, unknown_state: bool) -> Assessment:
    """Evaluate trusted metadata only; the result is advisory, not a grant."""
    checks = (trusted_policy, frozen_suite, complete_spec, side_effect_free,
              semantic_review_required, budget_ok, unknown_state)
    if any(type(value) is not bool for value in checks):
        return Assessment('L2', 'invalid_or_unknown_metadata')
    if unknown_state:
        return Assessment('L2', 'unknown_state')
    if not trusted_policy:
        return Assessment('L2', 'policy_not_authorized')
    if not side_effect_free or semantic_review_required:
        return Assessment('L2', 'risk_requires_full_review')
    if not frozen_suite or not complete_spec:
        return Assessment('L2', 'insufficient_independent_specification')
    if not budget_ok:
        return Assessment('L2', 'budget_not_available')
    return Assessment('L1_candidate', 'shadow_only_not_authorized')
