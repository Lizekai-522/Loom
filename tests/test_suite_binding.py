"""A frozen source review only becomes executable through a new approved plan."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loom.api import create_app
from loom.artifacts import ArtifactService
from loom.auth import LocalIdentity
from loom.development_suites import DevelopmentSuiteService
from loom.execution import freeze_tests
from loom.sandbox import FrozenTest
from loom.store import BudgetExceeded, Conflict, InvalidInput, NotFound, Store, canonical
from loom.workflow import WorkflowDefinition, WorkflowService

PACK = Path(__file__).resolve().parents[1] / "packs/software-dev/workflow-v1.json"
BODY = {"schema_version":"loom.public-checks/v1","visibility":"development","cases":[
    {"name":"public-small","input":{"value":1},"expected":{"answer":2}},
    {"name":"public-exact","input":{"value":9007199254740993},"expected":{"answer":9007199254740994}}]}
ACCEPTANCE = (FrozenTest("hidden-host", {"value":674593}, {"answer":674594}),)


@pytest.fixture
def binding(tmp_path):
    store = Store(tmp_path / "binding.db")
    workflow = WorkflowService(store)
    artifacts = ArtifactService(store)
    suites = DevelopmentSuiteService(store)
    original = json.loads(PACK.read_text(encoding="utf-8"))
    original["nodes"] = [{"id":"checks","title":"Propose checks","instruction":"Untrusted case data",
        "role":"tester","depends":[],"limits":{"cost_limit_microusd":1000,"token_limit":1000,"max_attempts":1}}]
    source = workflow.create_goal("a","source-goal",title="Source",description="Read value and add one.",acceptance=["tests.pass"])["run_id"]
    saved = workflow.save_plan("a",source,"source-plan",original,0)
    workflow.approve_plan("a",source,"source-approve",saved["revision"],saved["digest"])
    claim = store.claim_next("a",source,"tester",lease_seconds=3600)
    candidate = artifacts.save_candidate(claim,"candidate","public-checks",deepcopy(BODY),actor="subject:tester")
    store.submit_output(claim,"source-submission",{"kind":"public-fixture-only"})
    preview = suites.preview("a",source,candidate["artifact_id"])
    frozen = suites.freeze("a",source,candidate["artifact_id"],"freeze",actor="subject:operator",
                            expected_review_digest=preview["review_digest"])
    ref = suites.get_suite("a",source,frozen["suite_id"])["execution_reference"]
    target = workflow.create_goal("a","target-goal",title="Execute reviewed checks",
        description="Read a JSON object and return answer equal to value plus one.",
        acceptance=["tests.pass"],cost_limit=2000100,token_limit=4000100)["run_id"]
    definition = json.loads(PACK.read_text(encoding="utf-8"))
    definition["nodes"] = [
        {"id":"work","title":"Implement","instruction":"Implement the supplied JSON transformation.",
         "role":"developer","depends":[],"limits":{"cost_limit_microusd":2000000,"token_limit":4000000,"max_attempts":2},
         "evaluation_digest":freeze_tests(ACCEPTANCE)[1],
         "repair":{"schema_version":"loom.rebind/v1","development_digest":ref["development_digest"],
                   "max_repairs":1,"stop_on_duplicate_source":True},
         "development_suite":ref},
        {"id":"review","title":"Independent review","instruction":"Trusted review is still required.",
         "role":"validator","channel":"verification","depends":["work"],
         "limits":{"cost_limit_microusd":100,"token_limit":100,"max_attempts":1}}]
    return dict(store=store,workflow=workflow,artifacts=artifacts,suites=suites,source=source,
                candidate=candidate,source_claim=claim,ref=ref,target=target,definition=definition)


def approve_binding(b):
    saved = b["workflow"].save_plan("a",b["target"],"target-plan",b["definition"],0)
    result = b["workflow"].approve_plan("a",b["target"],"target-approve",saved["revision"],saved["digest"],
                                        actor="subject:operator")
    return saved, result


def test_ref_is_frozen_in_new_contract_and_old_plan_and_origin_do_not_change(binding):
    b=binding
    old=b["workflow"].get_plan("a",b["source"])
    original=b["artifacts"].get_artifact("a",b["source"],b["candidate"]["artifact_id"])
    saved,result=approve_binding(b)
    spec=b["workflow"].get_task_spec("a",b["target"],result["task_ids"]["work"])
    assert spec["development_suite"]==b["ref"] and spec["plan_digest"]==saved["digest"]
    assert b["workflow"].get_plan("a",b["source"])==old
    assert b["artifacts"].get_artifact("a",b["source"],b["candidate"]["artifact_id"])==original
    with b["store"].connect() as db:
        assert db.execute("SELECT count(*) FROM artifact_candidates WHERE run_id=?",(b["target"],)).fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM effects").fetchone()[0]==0
    assert b["store"].snapshot("a",b["target"])["status"]=="open"


@pytest.mark.parametrize("field",["suite_digest","freeze_event_hash","review_digest","selected_body_hash"])
def test_changed_pins_reject_draft_without_partial_save(binding,field):
    b=binding
    b["definition"]["nodes"][0]["development_suite"][field]="0"*64
    with pytest.raises(Conflict):
        b["workflow"].save_plan("a",b["target"],"bad",b["definition"],0)
    assert b["workflow"].get_plan("a",b["target"])["revision"]==0


@pytest.mark.parametrize("change",[
    lambda n:n.pop("repair"),
    lambda n:n.update(role="tester"),
    lambda n:n["repair"].update(development_digest="0"*64),
    lambda n:n.update(evaluation_digest=n["development_suite"]["development_digest"]),
    lambda n:n["development_suite"].update(compiler_version="future"),
    lambda n:n["development_suite"].update(confers_authority=True),
])
def test_schema_rejects_unsupported_or_inconsistent_execution_contract(binding,change):
    b=binding
    change(b["definition"]["nodes"][0])
    with pytest.raises(InvalidInput):
        b["workflow"].save_plan("a",b["target"],"bad",b["definition"],0)


def test_source_changes_between_save_and_approve_keep_target_a_draft(binding):
    b=binding
    saved=b["workflow"].save_plan("a",b["target"],"target-plan",b["definition"],0)
    b["store"].invalidate("a",b["source"],b["source_claim"].task_id,"Source changed")
    before=b["store"].events("a",b["target"])
    with pytest.raises(Conflict):
        b["workflow"].approve_plan("a",b["target"],"approve",saved["revision"],saved["digest"],actor="subject:operator")
    assert b["store"].events("a",b["target"])==before
    assert b["store"].snapshot("a",b["target"])["tasks"]==[]
    assert b["workflow"].get_goal("a",b["target"])["plan_status"]=="draft"


def test_approval_needs_named_operator_and_budget_remains_enforced(binding):
    b=binding
    b["definition"]["nodes"][0]["limits"]["cost_limit_microusd"]=2000200
    saved=b["workflow"].save_plan("a",b["target"],"target-plan",b["definition"],0)
    with pytest.raises(InvalidInput):
        b["workflow"].approve_plan("a",b["target"],"anonymous",saved["revision"],saved["digest"])
    with pytest.raises(BudgetExceeded):
        b["workflow"].approve_plan("a",b["target"],"budget",saved["revision"],saved["digest"],actor="subject:operator")
    assert not b["store"].snapshot("a",b["target"])["tasks"]


def test_same_run_or_other_tenant_reference_is_not_an_import_permission(binding):
    b=binding
    with b["store"].connect() as db:
        with pytest.raises(Conflict):
            b["suites"].resolve_reference_in(db,"a",b["source"],b["ref"])
        with pytest.raises(NotFound):
            b["suites"].resolve_reference_in(db,"other",b["target"],b["ref"])


def test_existing_plans_omit_the_new_field_and_source_approval_stays_immutable(binding):
    b=binding
    plan=b["workflow"].get_plan("a",b["source"])
    checked=WorkflowDefinition.model_validate(plan["definition"]).model_dump(mode="json")
    assert canonical(checked)==canonical(plan["definition"])
    assert "development_suite" not in checked["nodes"][0]
    with pytest.raises(Conflict):
        b["workflow"].save_plan("a",b["source"],"modify-old",b["definition"],plan["revision"])


def test_http_legacy_can_edit_draft_but_only_named_operator_can_approve_suite_plan(binding):
    b=binding
    token="suite-binding-api-public-fixture-operator-00000000"
    legacy="suite-binding-api-public-fixture-legacy-00000000"
    identity=LocalIdentity("a","operator",frozenset({"operator"}),token)
    saved=b["workflow"].save_plan("a",b["target"],"target-plan",b["definition"],0)
    with TestClient(create_app(b["store"],{"a":legacy},identities=(identity,))) as client:
        path=f"/v1/goals/{b['target']}/plan/approve"
        data={"expected_revision":saved["revision"],"expected_digest":saved["digest"]}
        assert client.post(path,json=data,headers={"Authorization":"Bearer "+legacy,"Idempotency-Key":"legacy-approve"}).status_code==403
        result=client.post(path,json=data,headers={"Authorization":"Bearer "+token,"Idempotency-Key":"named-approve"})
        assert result.status_code==200 and result.json()["status"]=="open"
