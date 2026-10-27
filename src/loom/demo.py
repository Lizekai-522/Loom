"""Runnable fixture workflow, deliberately separate from real model evaluation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

from .policy import ArtifactSubmission, VerificationSignature, classify_progress
from .store import Store, digest


def fixture_verification(claim, output, passed=True):
    """Fixture trust assertions ONLY; not a cryptographic or real-model signature."""
    content, revision = digest(output), f"{claim.task_id}:{claim.epoch}"
    signatures = tuple(VerificationSignature(f"fixture-verifier-{i}", f"fixture-model-{i}", content,
                         "kernel-v1", revision, passed, authenticated=True) for i in range(2))
    return ArtifactSubmission(content, "kernel-v1", revision, "fixture://evidence/" + content,
                              True, passed, "signed", signatures)


def clamp_tests(implementation):
    # Fixed, reviewed local callables. No user code or model text is executed.
    candidate = (lambda value, low, high: min(value, high)) if implementation == "clamp-v1" else (lambda value, low, high: min(max(value, low), high))
    cases = [(-2, 0, 10, 0), (5, 0, 10, 5), (20, 0, 10, 10), (0, 0, 0, 0)]
    return [{"input": [v, lo, hi], "expected": expected, "actual": candidate(v, lo, hi),
             "passed": candidate(v, lo, hi) == expected} for v, lo, hi, expected in cases]


def run_demo(store: Store, manifest_path: str | Path) -> dict:
    pack = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if pack["metadata"]["executionMode"] != "deterministic-fixture" or pack["constraints"]["externalWrites"]:
        raise ValueError("this runner accepts local fixture packs only")
    tenant = "demo"
    root = store.create_run(tenant, "demo:" + str(uuid4()), acceptance=pack["acceptance"],
                            cost_limit=pack["budget"]["maxMicrousd"], token_limit=pack["budget"]["maxTokens"])
    run_id = root["run_id"]
    prefix = run_id + ":"
    steps = {prefix + step["id"]: step for step in pack["workflow"]}
    for task_id, step in steps.items():
        store.add_task(tenant, run_id, "add:" + task_id, task_id=task_id, title=step["id"],
                       depends_on=[prefix + dep for dep in step["dependsOn"]], channel=step.get("channel", "business"))
    max_parallel = 0
    repaired = 0

    def execute(claim):
        step = steps[claim.task_id]
        role = pack["kindRole"][step["kind"]]
        reservation = f"{claim.task_id}:{claim.epoch}"
        store.reserve(claim, reservation, max_cost=25, max_tokens=25)
        implementation = "clamp-v1" if step["id"] == "code" and claim.epoch == 1 else "clamp-v2"
        checks = clamp_tests(implementation)
        output = {"mode": "fixture", "role": role, "step": step["id"], "implementation": implementation,
                  "test_results": checks, "source_ref": "fixture://clamp-spec/v1"}
        store.submit_output(claim, "submit:" + reservation, output)
        passed = all(case["passed"] for case in checks)
        result = store.complete(claim, "verify:" + reservation, output,
                                verification=fixture_verification(claim, output, passed))
        store.settle(tenant, reservation, actual_cost=10, actual_tokens=10)
        return claim, result

    with ThreadPoolExecutor(max_workers=store.max_workers) as pool:
        while True:
            claims = []
            for channel in ("verification", "business"):
                while claim := store.claim_next(tenant, run_id, "fixture-pool", channel=channel):
                    claims.append(claim)
            if not claims:
                break
            max_parallel = max(max_parallel, len(claims))
            for claim, result in pool.map(execute, claims):
                if result["status"] != "done":
                    if claim.epoch >= pack["budget"]["maxAttemptsPerTask"]:
                        raise RuntimeError("fixture repair budget exhausted")
                    store.record_repair(tenant, run_id, claim.task_id, primitive="Rebind", reason="clamp lower-bound assertion failed; use corrected fixture")
                    repaired += 1
    snapshot = store.snapshot(tenant, run_id)
    all_done = all(t["status"] == "done" for t in snapshot["tasks"])
    final_cases = clamp_tests("clamp-v2")
    outcome = classify_progress(acceptance_satisfied=all_done and all(c["passed"] for c in final_cases), idle_rounds=0, max_idle_rounds=2)
    if not outcome.counts_as_success:
        raise RuntimeError("workflow stopped without satisfying acceptance")
    store.finish_run(tenant, run_id, "finish:" + run_id,
                     {"schema.valid": True, "tests.pass": all(c["passed"] for c in final_cases), "report.traceable": True})
    chain = store.events(tenant, run_id)
    replay = store.replay(tenant, run_id)
    current = store.snapshot(tenant, run_id)
    return {"mode": "deterministic-fixture", "real_models_used": False, "real_external_writes": 0,
            "run_id": run_id, "outcome": asdict(outcome), "tasks": len(current["tasks"]),
            "max_concurrent_dispatched": max_parallel, "repair_count": repaired,
            "gate_failure_count": sum(e["type"] == "gate.failed" for e in chain),
            "event_count": len(chain), "chain_head": chain[-1]["hash"], "chain_valid": store.verify_events(chain),
            "checkpoint_warning": "head is local, not externally signed", "replay_matches_task_statuses":
                all(replay["tasks"][t["id"]]["status"] == t["status"] for t in current["tasks"]),
            "simulated_cost_microusd": current["spent"], "provider_cost_usd": 0,
            "events": chain, "replay": replay}
