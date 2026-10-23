"""Read-only audit summaries without candidate source or test inputs/answers."""
from __future__ import annotations

import re

from .store import Store


_KINDS = frozenset({"execution.candidate", "development.evaluated", "execution.evaluated",
                    "execution.blocked", "failure.classified", "repair.applied", "repair.exhausted",
                    "execution.context_bound"})
_FIELDS = ("task_id", "epoch", "from_epoch", "to_epoch", "primitive", "family", "code", "reason",
           "stage", "source_sha256", "plan_digest", "development_digest", "evaluation_digest",
           "hard_criteria_passed", "passed", "billing_uncertain", "auto_repair_allowed")


def attempt_summaries(store: Store, tenant: str, run_id: str) -> list[dict]:
    with store.connect() as db:
        store._run(db, tenant, run_id)
    summaries = []
    for event in store.events(tenant, run_id):
        if event["type"] not in _KINDS:
            continue
        if event["type"] == "execution.context_bound" and event["actor"] != "local-executor":
            continue
        payload = event["payload"]
        summary = {"event_type": event["type"], "event_hash": event["hash"], "seq": event["seq"]}
        # Never forward nested model/worker data, even if a future event adds
        # structured data under a formerly scalar field name.
        summary.update({key: payload[key] for key in _FIELDS if key in payload
                        and (payload[key] is None or type(payload[key]) in (str, int, bool))})
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
