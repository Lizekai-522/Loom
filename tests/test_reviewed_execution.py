"""Reviewed suites use existing guarded execution; model and sandbox are mocks."""
import asyncio
from functools import wraps
import json
from unittest.mock import patch

import httpx
import pytest

from loom.artifacts import ExecutionContextError
from loom.execution import LocalTaskRunner, freeze_tests
from loom.model_gateway import ModelGateway, ModelRoute
from loom.operator import OperatorService
from loom.repair import RebindService
from loom.sandbox import FrozenTest
from loom.store import Conflict
from tests.test_repair import SequenceSandbox
from tests.test_suite_binding import ACCEPTANCE, binding, approve_binding

GOOD = 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'
BAD = 'import json\nprint(json.dumps({"answer":0}))\n'


def async_test(function):
    """Use the standard-library loop, matching this project's dependency set."""
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def runtime(b, *, outcomes=(True,True), sources=(GOOD,), callback=None):
    approve_binding(b)
    claim=b["store"].claim_next("a",b["target"],"developer",lease_seconds=3600)
    requests=[]
    pending=list(sources)
    def handler(request):
        requests.append(request)
        if callback:
            callback()
        return httpx.Response(200,json={"id":"reviewed-execution-public-fixture","object":"chat.completion",
            "created":1792700000,"model":"deepseek-flash","choices":[{"index":0,"finish_reason":"stop",
            "message":{"role":"assistant","content":json.dumps({"source":pending.pop(0)})}}],
            "usage":{"prompt_tokens":25,"completion_tokens":10,"total_tokens":35}})
    gateway=ModelGateway([ModelRoute("deepseek-flash",timeout_seconds=1)],
        transport=httpx.MockTransport(handler),environ={"DEEPSEEK_API_KEY":"reviewed-runtime-public-fixture-secret"})
    sandbox=SequenceSandbox(outcomes)
    runner=LocalTaskRunner(b["store"],gateway,sandbox)
    return runner,claim,gateway,sandbox,requests


def invalidate(b):
    b["store"].invalidate("a",b["source"],b["source_claim"].task_id,"Source changed during execution")


def test_suite_only_context_cannot_be_omitted_for_admission_or_submission(binding):
    b=binding
    approve_binding(b)
    claim=b["store"].claim_next("a",b["target"],"developer",lease_seconds=3600)
    captured=b["artifacts"].capture_execution_context(claim)
    assert captured["items"]==[] and captured["development_suite"]==b["ref"]
    with pytest.raises(ExecutionContextError):
        b["artifacts"].submit_execution_output(claim,"omit",{},None)
    with pytest.raises(ExecutionContextError):
        b["artifacts"].admit_execution_effect(claim,None,reservation_id="invalid",max_cost=1,max_tokens=1,
            operation_id="invalid",request={"prompt_digest":"1"*64})
    assert not b["store"].snapshot("a",b["target"])["reserved"]


def test_suite_context_event_failure_rolls_back_reservation_effect_and_context_audit(binding):
    b=binding
    approve_binding(b)
    claim=b["store"].claim_next("a",b["target"],"developer",lease_seconds=3600)
    snapshot=b["artifacts"].capture_execution_context(claim)
    event=b["store"]._event
    def fail(db,tenant,run,kind,*args,**kwargs):
        if kind=="execution.context_bound":
            raise RuntimeError("simulated audit failure")
        return event(db,tenant,run,kind,*args,**kwargs)
    with patch.object(b["store"],"_event",fail), pytest.raises(RuntimeError):
        b["artifacts"].admit_execution_effect(claim,snapshot,reservation_id="rolled-back",max_cost=1,max_tokens=1,
            operation_id="rolled-back",request={"prompt_digest":"1"*64})
    state=b["store"].snapshot("a",b["target"])
    assert state["reserved"]==state["tokens_reserved"]==0
    with b["store"].connect() as db:
        assert db.execute("SELECT count(*) FROM effects WHERE run_id=?",(b["target"],)).fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM reservations WHERE run_id=?",(b["target"],)).fetchone()[0]==0


@async_test
async def test_registry_cases_load_without_caller_supplied_tests_and_keep_review_blocked(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b)
    try:
        result=await runner.run_python_cycle(claim,ACCEPTANCE)
    finally:
        await gateway.aclose()
    assert result["status"]=="submitted" and result["verified"] is False and result["development_passed"]
    assert len(requests)==1 and sandbox.execution_calls==2
    assert [t.name for t in sandbox.inputs[0][1]]==["public-small","public-exact"]
    assert sandbox.inputs[0][1][1].input["value"]==9007199254740993
    assert freeze_tests(sandbox.inputs[0][1])[1]==b["ref"]["development_digest"]
    events=b["store"].events("a",b["target"])
    bound=next(e["payload"] for e in events if e["type"]=="execution.context_bound")
    assert bound["context_item_count"]==0 and bound["development_suite"]==b["ref"]
    assert b["store"].snapshot("a",b["target"])["reserved"]==0
    assert b["store"].claim_next("a",b["target"],"verifier",channel="verification") is None
    assert "674593" not in requests[0].content.decode()
    assert "674594" not in requests[0].content.decode()


@async_test
async def test_changed_source_after_approval_stops_without_model_or_sandbox(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b)
    invalidate(b)
    try:
        result=await runner.run_python_cycle(claim,ACCEPTANCE)
    finally:
        await gateway.aclose()
    assert result["status"]=="awaiting_human" and result["reason"]=="CONTEXT_SUITE_INVALID"
    assert not requests and sandbox.execution_calls==sandbox.preflight_calls==0
    assert b["store"].snapshot("a",b["target"])["reserved"]==0


@async_test
async def test_changed_source_between_preflight_and_atomic_admission_leaves_no_hold(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b)
    old=sandbox.preflight
    def preflight():
        old()
        invalidate(b)
    sandbox.preflight=preflight
    try:
        result=await runner.run_python_cycle(claim,ACCEPTANCE)
    finally:
        await gateway.aclose()
    assert result["reason"]=="CONTEXT_SUITE_INVALID" and not requests and sandbox.execution_calls==0
    with b["store"].connect() as db:
        assert db.execute("SELECT count(*) FROM reservations WHERE run_id=?",(b["target"],)).fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM effects WHERE run_id=?",(b["target"],)).fetchone()[0]==0


@async_test
async def test_source_changed_after_effect_intent_keeps_uncertain_hold_without_dispatch(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b)
    admit=runner.artifacts.admit_execution_effect
    def changed(*args,**kwargs):
        result=admit(*args,**kwargs)
        invalidate(b)
        return result
    try:
        with patch.object(runner.artifacts,"admit_execution_effect",changed):
            result=await runner.run_python_cycle(claim,ACCEPTANCE)
    finally:
        await gateway.aclose()
    assert result["reason"]=="CONTEXT_SUITE_INVALID" and result["billing_uncertain"] is True
    assert result["usage_complete"] is False and not requests and sandbox.execution_calls==0
    state=b["store"].snapshot("a",b["target"])
    assert state["reserved"]>0 and state["tokens_reserved"]>0 and state["tokens_spent"]==0
    with b["store"].connect() as db:
        effect=db.execute("SELECT state FROM effects WHERE run_id=?",(b["target"],)).fetchone()
        assert effect["state"]=="indeterminate"


@async_test
async def test_source_change_during_known_response_preserves_usage_and_blocks_sandbox(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b,callback=lambda:invalidate(b))
    try:
        result=await runner.run_python_cycle(claim,ACCEPTANCE)
    finally:
        await gateway.aclose()
    assert result["reason"]=="CONTEXT_SUITE_INVALID" and len(requests)==1 and sandbox.execution_calls==0
    state=b["store"].snapshot("a",b["target"])
    assert state["tokens_spent"]==35 and state["reserved"]==state["tokens_reserved"]==0
    assert not any(e["type"]=="execution.evaluated" for e in b["store"].events("a",b["target"]))


@async_test
async def test_caller_cannot_swap_development_tests(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b)
    try:
        with pytest.raises(Conflict):
            await runner.run_python_cycle(claim,ACCEPTANCE,[FrozenTest("forged",{},True)])
    finally:
        await gateway.aclose()
    assert not requests and sandbox.execution_calls==0


@async_test
async def test_bounded_repair_reuses_pinned_suite_each_epoch_and_hides_acceptance(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b,outcomes=(False,True,True),sources=(BAD,GOOD))
    try:
        result=await runner.run_python_cycle(claim,ACCEPTANCE)
    finally:
        await gateway.aclose()
    assert result["status"]=="submitted" and result["repairs_applied"]==1 and len(requests)==2
    events=b["store"].events("a",b["target"])
    bounds=[e["payload"] for e in events if e["type"]=="execution.context_bound"]
    assert [e["epoch"] for e in bounds]==[1,2] and all(e["development_suite"]==b["ref"] for e in bounds)
    assert bounds[0]["context_digest"]!=bounds[1]["context_digest"]
    for request in requests:
        assert "674593" not in request.content.decode() and "674594" not in request.content.decode()
    assert b["store"].claim_next("a",b["target"],"verifier",channel="verification") is None


@async_test
async def test_source_change_at_rebind_boundary_does_not_increment_epoch(binding):
    b=binding
    runner,claim,gateway,sandbox,requests=runtime(b,outcomes=(False,),sources=(BAD,))
    rebind=RebindService.rebind
    def changed(service,*args,**kwargs):
        invalidate(b)
        return rebind(service,*args,**kwargs)
    try:
        with patch.object(RebindService,"rebind",changed):
            result=await runner.run_python_cycle(claim,ACCEPTANCE)
    finally:
        await gateway.aclose()
    assert result["reason"]=="CONTEXT_SUITE_INVALID" and len(requests)==1
    assert b["store"].snapshot("a",b["target"])["tasks"][0]["epoch"]==1
    assert not any(e["type"]=="repair.applied" for e in b["store"].events("a",b["target"]))
