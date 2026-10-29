"""Read-only audit summaries without candidate source or test inputs/answers."""
from __future__ import annotations

import re

from .artifacts import ArtifactService
from .role_summary import model_source_summary
from .store import Conflict, NotFound, Store


_KINDS = frozenset({"execution.candidate", "development.evaluated", "execution.evaluated",
                    "execution.blocked", "failure.classified", "repair.applied", "repair.exhausted",
                    "execution.context_bound", "role.candidate_generated"})
_FIELDS = ("task_id", "epoch", "from_epoch", "to_epoch", "primitive", "family", "code", "reason",
           "stage", "source_sha256", "plan_digest", "development_digest", "evaluation_digest",
           "hard_criteria_passed", "passed", "billing_uncertain", "auto_repair_allowed")
_HOST_COUNTS = ("host_cases_total", "host_cases_passed", "host_cases_mismatch", "host_cases_timeout",
                "host_cases_runtime_error", "host_cases_other_failure")


def attempt_summaries(store: Store, tenant: str, run_id: str) -> list[dict]:
    with store.connect() as db:
        store._run(db, tenant, run_id)
    summaries = []
    artifacts = None
    for event in store.events(tenant, run_id):
        if event["type"] not in _KINDS:
            continue
        if event["type"] == "execution.context_bound" and event["actor"] != "local-executor":
            continue
        payload = event["payload"]
        if event["type"] == "role.candidate_generated":
            if event["actor"] != "local-executor":
                continue
            artifact_id = payload.get("artifact_id")
            if type(artifact_id) is not str or len(artifact_id) > 200:
                continue
            if artifacts is None:
                artifacts = ArtifactService(store)
            try:
                candidate = artifacts.get_artifact(tenant, run_id, artifact_id, include_body=False)
            except (Conflict, NotFound):
                continue
            source = model_source_summary(candidate)
            if (source is None or candidate["source_event_hash"] != event["hash"]
                    or candidate["source_task_id"] != payload.get("task_id")
                    or candidate["source_epoch"] != payload.get("epoch")):
                continue
            summaries.append({"event_type": event["type"], "event_hash": event["hash"], "seq": event["seq"],
                "task_id": candidate["source_task_id"], "epoch": candidate["source_epoch"], **source})
            continue
        summary = {"event_type": event["type"], "event_hash": event["hash"], "seq": event["seq"]}
        # Never forward nested model/worker data, even if a future event adds
        # structured data under a formerly scalar field name.
        summary.update({key: payload[key] for key in _FIELDS if key in payload
                        and (payload[key] is None or type(payload[key]) in (str, int, bool))})
        if event["type"] == "execution.evaluated" and event["actor"] == "local-executor":
            values = [payload.get(key) for key in _HOST_COUNTS]
            if (all(type(value) is int and 0 <= value <= 32 for value in values)
                    and 1 <= values[0] <= 32 and values[0] == sum(values[1:])
                    and type(payload.get("hard_criteria_passed")) is bool
                    and (values[1] == values[0]) is payload["hard_criteria_passed"]):
                summary.update(dict(zip(_HOST_COUNTS, values)))
        if event["type"] == "execution.context_bound":
            value = payload.get("context_digest")
            if type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value):
                summary["context_digest"] = value
            for key, maximum in (("context_bytes", 65536), ("context_item_count", 8)):
                value = payload.get(key)
                if type(value) is int and 0 <= value <= maximum:
                    summary[key] = value
            for key, expected in (("untrusted_data", True), ("producer_verified", False), ("counts_as_success", False)):
                if payload.get(key) is expected:
                    summary[key] = expected
        summaries.append(summary)
    return summaries
