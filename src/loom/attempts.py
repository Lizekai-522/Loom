"""Read-only audit summaries without candidate source or test inputs/answers."""
from __future__ import annotations

from .store import Store


_KINDS = frozenset({"execution.candidate", "development.evaluated", "execution.evaluated",
                    "execution.blocked", "failure.classified", "repair.applied", "repair.exhausted"})
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
        payload = event["payload"]
        summary = {"event_type": event["type"], "event_hash": event["hash"], "seq": event["seq"]}
        # Never forward nested model/worker data, even if a future event adds
        # structured data under a formerly scalar field name.
        summary.update({key: payload[key] for key in _FIELDS if key in payload
                        and (payload[key] is None or type(payload[key]) in (str, int, bool))})
        summaries.append(summary)
    return summaries
