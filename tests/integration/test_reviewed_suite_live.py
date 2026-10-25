"""Real Docker for reviewed generated-style cases; provider responses are mocks."""
import asyncio
import json
import os

import httpx
import pytest

from loom.execution import LocalTaskRunner
from loom.model_gateway import ModelGateway, ModelRoute
from loom.sandbox import DockerSandbox, SandboxConfig
from tests.test_suite_binding import ACCEPTANCE, binding, approve_binding

GOOD = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'
BAD = 'import json\nprint(json.dumps({"answer":0}))\n'
HIDDEN_FAIL = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":0 if x["value"]==674593 else x["value"]+1}))\n'
pytestmark = pytest.mark.skipif(not os.environ.get("LOOM_SANDBOX_TEST_IMAGE"),
                               reason="explicit approved Docker image required")


class ObservedSandbox(DockerSandbox):
    def __init__(self, image):
        super().__init__(SandboxConfig(image,timeout_seconds=5))
        self.runs=[]
    def run_python(self, files, tests, *, entrypoint="main.py"):
        result=super().run_python(files,tests,entrypoint=entrypoint)
        self.runs.append(result)
        return result


def execute(b, sources, callback=None):
    approve_binding(b)
    claim=b["store"].claim_next("a",b["target"],"developer",lease_seconds=3600)
    requests=[]
    remaining=list(sources)
    def handler(request):
        requests.append(request)
        if callback:
            callback()
        return httpx.Response(200,json={"id":"reviewed-docker-mock-response","object":"chat.completion",
            "created":1792700000,"model":"deepseek-flash","choices":[{"index":0,"finish_reason":"stop",
                "message":{"role":"assistant","content":json.dumps({"source":remaining.pop(0)})}}],
            "usage":{"prompt_tokens":25,"completion_tokens":10,"total_tokens":35}})
    gateway=ModelGateway([ModelRoute("deepseek-flash",timeout_seconds=1)],
        transport=httpx.MockTransport(handler),environ={"DEEPSEEK_API_KEY":"reviewed-docker-public-fixture-only"})
    sandbox=ObservedSandbox(os.environ["LOOM_SANDBOX_TEST_IMAGE"])
    sandbox.preflight()
    async def run():
        try:
            return await LocalTaskRunner(b["store"],gateway,sandbox).run_python_cycle(claim,ACCEPTANCE)
        finally:
            await gateway.aclose()
    result=asyncio.run(run())
    return result,sandbox,requests


def test_reviewed_source_cases_and_distinct_host_acceptance_execute_in_real_docker(binding):
    b=binding
    result,sandbox,requests=execute(b,[GOOD])
    assert result["status"]=="submitted" and result["verified"] is False
    assert len(requests)==1 and len(sandbox.runs)==2
    assert [c.name for c in sandbox.runs[0].cases]==["public-small","public-exact"]
    assert sandbox.runs[0].tests_sha256==b["ref"]["development_digest"]
    assert all(r.passed for r in sandbox.runs) and len(sandbox.runs[1].cases)==1
    assert b["store"].claim_next("a",b["target"],"reviewer",channel="verification") is None


def test_generated_development_failure_uses_one_bounded_repair_before_host_acceptance(binding):
    b=binding
    result,sandbox,requests=execute(b,[BAD,GOOD])
    assert result["status"]=="submitted" and result["repairs_applied"]==1 and len(requests)==2
    assert len(sandbox.runs)==3 and not sandbox.runs[0].passed and all(r.passed for r in sandbox.runs[1:])
    assert sandbox.runs[0].tests_sha256==sandbox.runs[1].tests_sha256==b["ref"]["development_digest"]
    assert b"674593" not in requests[1].content and b"674594" not in requests[1].content
    assert b["store"].snapshot("a",b["target"])["reserved"]==0


def test_hidden_failure_is_not_used_to_repair_generated_suite_or_request_another_model(binding):
    b=binding
    result,sandbox,requests=execute(b,[HIDDEN_FAIL])
    assert result["status"]=="awaiting_human" and result["reason"]=="ACCEPTANCE_TESTS_FAILED"
    assert len(requests)==1 and len(sandbox.runs)==2
    assert sandbox.runs[0].passed and not sandbox.runs[1].passed
    assert result["repairs_applied"]==0 and b"674593" not in requests[0].content


def test_source_invalidated_during_model_response_stops_before_real_candidate_containers(binding):
    b=binding
    def changed():
        b["store"].invalidate("a",b["source"],b["source_claim"].task_id,"Source changed during known response")
    result,sandbox,requests=execute(b,[GOOD],changed)
    assert result["status"]=="awaiting_human" and result["reason"]=="CONTEXT_SUITE_INVALID"
    assert len(requests)==1 and not sandbox.runs
    state=b["store"].snapshot("a",b["target"])
    assert state["tokens_spent"]==35 and state["reserved"]==state["tokens_reserved"]==0
