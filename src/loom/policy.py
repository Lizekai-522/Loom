"""Deterministic policy decisions for Loom's first kernel.

These functions perform no IO, model calls, database writes, or cryptographic
verification. Evidence and signature authentication flags must be supplied by
trusted adapters, never copied from model-produced fields. Authorization at the
actual tool/state mutation boundary remains the service's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable


@dataclass(frozen=True, slots=True)
class GateRules:
    version: str = "loom/v1.0"
    schema_version: int = 1
    require_dual_sign: bool = True
    require_trusted_evidence: bool = True


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    reason: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskContract:
    task_id: str
    run_id: str
    scope: tuple[str, ...]
    goal_id: str
    output_criteria: tuple[str, ...]
    anchor_ref: str
    schema_version: int = 1


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def gate_p(
    contract: TaskContract,
    *,
    rules: GateRules = GateRules(),
    allowed_scope: frozenset[str] | None = None,
    known_anchors: frozenset[str] | None = None,
    known_criteria: frozenset[str] | None = None,
) -> GateResult:
    """Validate a typed task book; task-title wording never affects admission.

    ``goal_id`` names exactly one child objective. Output criteria are stable
    registered predicate IDs, not natural-language instructions. Callers pass
    the authorized scope and resolvable anchor IDs from trusted registries.
    """
    errors: list[str] = []
    for field in ("task_id", "run_id", "goal_id", "anchor_ref"):
        if not _nonempty(getattr(contract, field)):
            errors.append(f"invalid_{field}")
    if type(contract.schema_version) is not int or contract.schema_version != rules.schema_version:
        errors.append("unsupported_schema_version")
    valid_collections: dict[str, bool] = {}
    for field in ("scope", "output_criteria"):
        values = getattr(contract, field)
        valid = isinstance(values, tuple) and bool(values) and all(_nonempty(v) for v in values)
        valid_collections[field] = valid
        if not valid:
            errors.append(f"invalid_{field}")
        elif len(set(values)) != len(values):
            errors.append(f"duplicate_{field}")
    if allowed_scope is None:
        errors.append("scope_authority_missing")
    elif valid_collections["scope"] and not set(contract.scope).issubset(allowed_scope):
        errors.append("scope_denied")
    if known_anchors is None:
        errors.append("anchor_authority_missing")
    elif _nonempty(contract.anchor_ref) and contract.anchor_ref not in known_anchors:
        errors.append("anchor_unresolvable")
    if known_criteria is None:
        errors.append("criterion_authority_missing")
    elif valid_collections["output_criteria"] and not set(contract.output_criteria).issubset(known_criteria):
        errors.append("criterion_unregistered")
    if errors:
        return GateResult(False, errors[0], tuple(errors))
    return GateResult(True, "ok")


@dataclass(frozen=True, slots=True)
class VerificationSignature:
    verifier_id: str
    model_id: str
    artifact_hash: str
    ruleset_version: str
    evidence_revision: str
    passed: bool
    authenticated: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactSubmission:
    content_hash: str
    ruleset_version: str
    evidence_revision: str
    evidence_ref: str
    evidence_trusted: bool
    hard_criteria_passed: bool
    dual_sign: str = "pending"
    signatures: tuple[VerificationSignature, ...] = ()
    verification_required: bool = True


def gate_v(artifact: ArtifactSubmission, *, rules: GateRules = GateRules()) -> GateResult:
    """Release only the exact content/evidence/rules version that was reviewed."""
    for field in ("content_hash", "ruleset_version", "evidence_revision", "evidence_ref"):
        if not _nonempty(getattr(artifact, field)):
            return GateResult(False, f"invalid_{field}")
    if artifact.ruleset_version != rules.version:
        return GateResult(False, "ruleset_version_mismatch")
    if rules.require_trusted_evidence and artifact.evidence_trusted is not True:
        return GateResult(False, "untrusted_evidence")
    if artifact.hard_criteria_passed is not True:
        return GateResult(False, "hard_criteria_failed")
    required = rules.require_dual_sign or artifact.verification_required
    if required and artifact.dual_sign != "signed":
        return GateResult(False, "dual_sign_not_signed")
    if artifact.dual_sign not in {"signed", "single"}:
        return GateResult(False, "invalid_verification_state")
    if not isinstance(artifact.signatures, tuple):
        return GateResult(False, "invalid_signatures")
    if len(artifact.signatures) < (2 if required else 1):
        return GateResult(False, "insufficient_signatures")
    verifier_ids: set[str] = set()
    model_ids: set[str] = set()
    for signature in artifact.signatures:
        if not _nonempty(signature.verifier_id) or not _nonempty(signature.model_id):
            return GateResult(False, "invalid_verifier_identity")
        if signature.verifier_id in verifier_ids:
            return GateResult(False, "duplicate_verifier")
        verifier_ids.add(signature.verifier_id)
        model_ids.add(signature.model_id)
        if signature.authenticated is not True:
            return GateResult(False, "unauthenticated_signature")
        if signature.passed is not True:
            return GateResult(False, "verification_rejected")
        if signature.artifact_hash != artifact.content_hash:
            return GateResult(False, "signature_artifact_mismatch")
        if signature.ruleset_version != artifact.ruleset_version:
            return GateResult(False, "signature_ruleset_mismatch")
        if signature.evidence_revision != artifact.evidence_revision:
            return GateResult(False, "signature_evidence_mismatch")
    if required and len(model_ids) < 2:
        return GateResult(False, "independent_model_required")
    return GateResult(True, "ok")


@dataclass(frozen=True, slots=True)
class AcceptanceDecision:
    state: str
    counts_as_success: bool
    reason: str


def manual_accept(
    gate_result: GateResult,
    *,
    requester_id: str,
    reviewer_id: str,
    reason: str,
    allow_exception: bool = False,
    second_reviewer_id: str | None = None,
    high_risk: bool = False,
) -> AcceptanceDecision:
    """An explicit exception preserves failure and never becomes normal success."""
    if not all(_nonempty(v) for v in (requester_id, reviewer_id, reason)):
        return AcceptanceDecision("rejected", False, "missing_review_identity_or_reason")
    if requester_id == reviewer_id:
        return AcceptanceDecision("rejected", False, "self_approval_denied")
    if high_risk and (
        not _nonempty(second_reviewer_id)
        or second_reviewer_id in {requester_id, reviewer_id}
    ):
        return AcceptanceDecision("rejected", False, "independent_second_review_required")
    if gate_result.passed is True:
        return AcceptanceDecision("verified", True, "hard_criteria_passed")
    if allow_exception:
        return AcceptanceDecision("accepted_with_exception", False, gate_result.reason)
    return AcceptanceDecision("rejected", False, gate_result.reason)


@dataclass(frozen=True, slots=True)
class ProgressDecision:
    state: str
    counts_as_success: bool
    reason: str


def classify_progress(
    *,
    acceptance_satisfied: bool,
    idle_rounds: int,
    max_idle_rounds: int,
    budget_exhausted: bool = False,
) -> ProgressDecision:
    if type(idle_rounds) is not int or idle_rounds < 0:
        raise ValueError("idle_rounds must be a nonnegative integer")
    if type(max_idle_rounds) is not int or max_idle_rounds < 1:
        raise ValueError("max_idle_rounds must be a positive integer")
    if acceptance_satisfied is True:
        return ProgressDecision("succeeded", True, "acceptance_satisfied")
    if budget_exhausted:
        return ProgressDecision("budget_exhausted", False, "budget_exhausted")
    if idle_rounds >= max_idle_rounds:
        return ProgressDecision("stalled", False, "no_verified_progress")
    return ProgressDecision("running", False, "still_progressing")


@dataclass(frozen=True, slots=True)
class SchedulableTask:
    task_id: str
    priority: int
    sequence: int
    ready: bool = True
    wait_ticks: int = 0


def select_task(tasks: Iterable[SchedulableTask], *, ageing_interval: int = 10) -> SchedulableTask | None:
    """Choose a ready task by priority plus deterministic waiting-time ageing."""
    if type(ageing_interval) is not int or ageing_interval < 1:
        raise ValueError("ageing_interval must be a positive integer")
    candidates = [t for t in tasks if t.ready]
    for task in candidates:
        if not _nonempty(task.task_id):
            raise ValueError("task_id must be a nonempty string")
        if any(type(v) is not int for v in (task.priority, task.sequence, task.wait_ticks)):
            raise ValueError("priority, sequence, and wait_ticks must be integers")
        if task.sequence < 0 or task.wait_ticks < 0:
            raise ValueError("sequence and wait_ticks must be nonnegative")
    return min(
        candidates,
        key=lambda t: (-(t.priority + t.wait_ticks // ageing_interval), t.sequence, t.task_id),
        default=None,
    )


@dataclass(frozen=True, slots=True)
class AgentCandidate:
    agent_id: str
    skills: frozenset[str]
    capacity: int
    active_load: int
    success_rate: float = 0.5
    latency_ms: float = 0.0
    cost_rate: float = 0.0
    failure_streak: int = 0


@dataclass(frozen=True, slots=True)
class AgentWeights:
    load: float = 0.35
    success: float = 0.35
    latency: float = 0.1
    cost: float = 0.1
    failures: float = 0.1


@dataclass(frozen=True, slots=True)
class CandidateScore:
    agent_id: str
    score: float
    parts: tuple[tuple[str, float], ...]


def score_agent(agent: AgentCandidate, *, weights: AgentWeights = AgentWeights()) -> CandidateScore:
    if not _nonempty(agent.agent_id):
        raise ValueError("agent_id must be a nonempty string")
    if type(agent.capacity) is not int or type(agent.active_load) is not int:
        raise ValueError("capacity and active_load must be integers")
    if agent.capacity < 1 or not 0 <= agent.active_load < agent.capacity:
        raise ValueError("agent has no available capacity")
    if not 0 <= agent.success_rate <= 1 or not isfinite(agent.success_rate):
        raise ValueError("success_rate must be finite and between zero and one")
    if any(not isfinite(v) or v < 0 for v in (agent.latency_ms, agent.cost_rate)):
        raise ValueError("latency and cost must be finite and nonnegative")
    if type(agent.failure_streak) is not int or agent.failure_streak < 0:
        raise ValueError("failure_streak must be a nonnegative integer")
    weight_values = (weights.load, weights.success, weights.latency, weights.cost, weights.failures)
    if any(not isfinite(v) or v < 0 for v in weight_values) or sum(weight_values) <= 0:
        raise ValueError("weights must be finite, nonnegative, and not all zero")
    parts = (
        ("load", weights.load * (1 - agent.active_load / agent.capacity)),
        ("success", weights.success * agent.success_rate),
        ("latency", weights.latency / (1 + agent.latency_ms / 1000)),
        ("cost", weights.cost / (1 + agent.cost_rate)),
        ("failures", -weights.failures * agent.failure_streak),
    )
    return CandidateScore(agent.agent_id, sum(value for _, value in parts), parts)


def select_agent(
    agents: Iterable[AgentCandidate],
    *,
    required_skills: frozenset[str] = frozenset(),
    weights: AgentWeights = AgentWeights(),
) -> CandidateScore | None:
    """Select an eligible agent after selecting the task; priority is not a score term."""
    scores = [
        score_agent(agent, weights=weights)
        for agent in agents
        if required_skills.issubset(agent.skills) and agent.active_load < agent.capacity
    ]
    return min(scores, key=lambda item: (-item.score, item.agent_id), default=None)


@dataclass(frozen=True, slots=True)
class ContextItem:
    source_ref: str
    token_count: int
    required: bool = False
    relevance: float = 0.0


@dataclass(frozen=True, slots=True)
class ContextBundle:
    retained: tuple[ContextItem, ...]
    dropped: tuple[ContextItem, ...]
    token_count: int


class ContextOverflow(ValueError):
    def __init__(self, required_tokens: int, token_budget: int) -> None:
        self.required_tokens = required_tokens
        self.token_budget = token_budget
        super().__init__(f"required context uses {required_tokens} tokens; budget is {token_budget}")


def assemble_context(items: Iterable[ContextItem], token_budget: int) -> ContextBundle:
    """Keep required references, add ranked optional items, and report all drops.

    Token counts are supplied by the selected model's tokenizer adapter. The
    kernel does not silently truncate a task book, frozen criteria, or an Anchor.
    """
    if type(token_budget) is not int or token_budget < 0:
        raise ValueError("token_budget must be a nonnegative integer")
    candidates = tuple(items)
    refs: set[str] = set()
    for item in candidates:
        if not _nonempty(item.source_ref) or item.source_ref in refs:
            raise ValueError("source_ref must be nonempty and unique")
        refs.add(item.source_ref)
        if type(item.token_count) is not int or item.token_count < 0:
            raise ValueError("token_count must be a nonnegative integer")
        if not isfinite(item.relevance):
            raise ValueError("relevance must be finite")
    required = tuple(item for item in candidates if item.required)
    total = sum(item.token_count for item in required)
    if total > token_budget:
        raise ContextOverflow(total, token_budget)
    retained = list(required)
    dropped: list[ContextItem] = []
    optional = sorted(
        (item for item in candidates if not item.required),
        key=lambda item: (-item.relevance, item.source_ref),
    )
    for item in optional:
        if total + item.token_count <= token_budget:
            retained.append(item)
            total += item.token_count
        else:
            dropped.append(item)
    return ContextBundle(tuple(retained), tuple(dropped), total)
