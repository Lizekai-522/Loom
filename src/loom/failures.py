"""Static failure families; transport problems never count as model ability."""
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureClassification:
    family: str
    code: str
    stage: str
    billing_uncertain: bool
    auto_repair_allowed: bool
    model_capability_error: bool


def classify_failure(code: str, *, stage: str, status_code: int | None = None,
                     billing_uncertain: bool = False) -> FailureClassification:
    """Classify platform codes/status only; never parse or retain provider prose."""
    if code == "MODEL_HTTP_ERROR" and status_code in {401, 403}:
        family = "auth"
    elif code == 'TASK_CONTEXT_LIMIT_EXCEEDED' or code == "MODEL_HTTP_ERROR" and status_code in {402, 429}:
        family = "quota"
    elif code in {"MODEL_TIMEOUT", "MODEL_TRANSPORT_ERROR"} or (
            code == "MODEL_HTTP_ERROR" and type(status_code) is int and 500 <= status_code <= 599):
        family = "network"
    elif code in {"EXECUTION_RUN_PAUSED", "OPERATOR_CANCELLED", "EXECUTION_CANCELLED"}:
        family = "canceled"
    elif code in {"EXECUTION_LEASE_LOST", "WORKER_CRASHED"}:
        family = "crash"
    elif code in {"MODEL_IDENTITY_MISMATCH", "EXECUTION_EVIDENCE_BINDING_MISMATCH", "SCOPE_DENIED"}:
        family = "scope_denied"
    elif code in {"MODEL_KEY_MISSING_OR_INVALID", "MODEL_KEY_MISSING"}:
        family = "auth"
    else:
        family = "other"
    allowed = code == "DEVELOPMENT_TESTS_FAILED" and not billing_uncertain
    return FailureClassification(family, code, stage, billing_uncertain, allowed,
                                 code in {"DEVELOPMENT_TESTS_FAILED", "MODEL_SOURCE_INVALID"})
