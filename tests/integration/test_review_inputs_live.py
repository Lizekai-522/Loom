"""Actual Docker evidence -> exact read-only review input; model HTTP mocked."""
import asyncio
import os

import pytest

from loom.sandbox import DockerSandbox, SandboxConfig
from loom.store import canonical
from tests.test_review_inputs import review_case


pytestmark = pytest.mark.skipif(not os.environ.get("LOOM_SANDBOX_TEST_IMAGE"), reason="explicit approved Docker image required")


def test_real_docker_evidence_is_bound_and_hidden_outputs_do_not_enter_review(review_case):
    b = review_case
    sandbox = DockerSandbox(SandboxConfig(image=os.environ["LOOM_SANDBOX_TEST_IMAGE"]))
    result = asyncio.run(b["execute"](sandbox))
    assert result["status"] == "submitted" and result["hard_criteria_passed"]
    before = b["store"].events("a", b["run"])
    preview = b["service"].preview("a", b["run"], b["claim"].task_id, include_source=True)
    evidence = next(e for e in before if e["type"] == "execution.evaluated")
    assert preview["subject"]["evaluation_event_hash"] == evidence["hash"]
    assert preview["execution_summary"]["cases_passed"] == 1
    for hidden in ("hidden-name-marker", "secret-golden", "stdout", "stderr"):
        assert hidden not in canonical(preview)
    assert not preview["independently_verified"] and not preview["execution_authorized"]
    assert b["store"].events("a", b["run"]) == before
    assert b["store"].claim_next("a", b["run"], "reviewer") is None
