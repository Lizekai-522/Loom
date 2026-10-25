"""Reviewed CLI previews are offline; approval checks private roles before pay."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import httpx

from loom.auth import LocalIdentity
from loom.model_gateway import ModelGateway, ModelRoute
from loom.store import Conflict
from tests.test_repair import SequenceSandbox
from tests.test_suite_binding import ACCEPTANCE, binding

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("reviewed_task_cli",ROOT/"scripts/run_local_task.py")
script=importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


def args_for(b,tmp_path):
    path=tmp_path/"job.json"
    path.write_text(json.dumps({"title":"Reviewed CLI","description":"Transform value to answer plus one.",
        "tests":[{"name":t.name,"input":t.input,"expected":t.expected} for t in ACCEPTANCE],
        "development_suite":b["ref"]}),encoding="utf-8")
    return SimpleNamespace(task_file=path,max_repairs=1,approve=False,db=b["store"].path,tenant="a",
        model_config=ROOT/"configs/models.example.json",secrets_file=tmp_path/"absent-secrets.json",
        identities_file=tmp_path/"absent-identities.json",sandbox_image="sha256:"+"1"*64,max_output_tokens=512)


def test_reviewed_preview_does_not_read_private_roles_secrets_or_database(binding,tmp_path):
    args=args_for(binding,tmp_path)
    with patch.object(script,"Store",side_effect=AssertionError("DB")), \
         patch.object(script,"load_identities",side_effect=AssertionError("private roles")), \
         patch.object(script,"load_project_secrets",side_effect=AssertionError("secrets")), \
         patch.object(script,"DockerSandbox",side_effect=AssertionError("Docker")):
        result=asyncio.run(script.run(args))
    node=result["definition"]["nodes"][0]
    assert node["development_suite"]==binding["ref"] and result["network_requests"]==0
    assert node["repair"]["development_digest"]==binding["ref"]["development_digest"]
    assert node["evaluation_digest"]!=node["repair"]["development_digest"]


def test_reference_cannot_mix_raw_cases_or_silently_enable_repair(binding,tmp_path):
    args=args_for(binding,tmp_path)
    args.max_repairs=0
    with pytest.raises(ValueError,match="REVIEWED_SUITE_REQUIRES_REPAIR_POLICY"):
        asyncio.run(script.run(args))
    body=json.loads(args.task_file.read_bytes())
    body["development_tests"]=[{"name":"raw","input":1,"expected":2}]
    args.task_file.write_text(json.dumps(body),encoding="utf-8")
    with pytest.raises(ValueError,match="REVIEWED_SUITE_AND_RAW_CASES_CANNOT_MIX"):
        script.load_job(args.task_file)


def test_approved_reference_requires_roles_before_loading_model_secrets(binding,tmp_path):
    args=args_for(binding,tmp_path)
    args.approve=True
    args.identities_file=None
    with patch.object(script,"load_project_secrets",side_effect=AssertionError("secrets read")):
        with pytest.raises(ValueError,match="REVIEWED_SUITE_IDENTITIES"):
            asyncio.run(script.run(args))


def test_stale_source_stops_cli_before_model_configuration_or_secret_read(binding,tmp_path):
    b=binding
    args=args_for(b,tmp_path)
    args.approve=True
    b["store"].invalidate("a",b["source"],b["source_claim"].task_id,"Source changed")
    identities=tuple(LocalIdentity("a",role,frozenset({role}),"reviewed-cli-public-fixture-"+role+"-00000000")
                     for role in ("operator","worker"))
    with patch.object(script,"load_identities",return_value=identities), \
         patch.object(script,"load_project_secrets",side_effect=AssertionError("secrets read")), \
         patch.object(script,"load_model_config",side_effect=AssertionError("model config read")):
        with pytest.raises(Conflict):
            asyncio.run(script.run(args))


def approved_run(b,args,*,stop_before_dispatch=False):
    identities=tuple(LocalIdentity("a",role,frozenset({role}),"reviewed-cli-public-fixture-"+role+"-00000000")
                     for role in ("operator","worker"))
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={"id":"reviewed-cli-mock","object":"chat.completion","created":1792700000,
            "model":"deepseek-flash","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant",
            "content":json.dumps({"source":'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'})}}],
            "usage":{"prompt_tokens":25,"completion_tokens":10,"total_tokens":35}})
    gateway=ModelGateway([ModelRoute("deepseek-flash",timeout_seconds=1)],transport=httpx.MockTransport(handler),
                         environ={"DEEPSEEK_API_KEY":"reviewed-cli-only-mock-secret"})
    original=script.LocalTaskRunner.run_python_cycle
    async def stopped(runner,claim,*params,**kwargs):
        b["store"].invalidate("a",b["source"],b["source_claim"].task_id,"Source changed after approval")
        return await original(runner,claim,*params,**kwargs)
    args.approve=True
    with patch.object(script,"load_identities",return_value=identities), \
         patch.object(script,"load_project_secrets",return_value={"DEEPSEEK_API_KEY":"reviewed-cli-only-mock-secret"}), \
         patch.object(script,"ModelGateway",return_value=gateway), \
         patch.object(script,"DockerSandbox",return_value=SequenceSandbox([True,True])):
        if stop_before_dispatch:
            with patch.object(script.LocalTaskRunner,"run_python_cycle",stopped):
                result=asyncio.run(script.run(args))
        else:
            result=asyncio.run(script.run(args))
    return result,requests


def test_approved_cli_routes_reviewed_suite_and_records_known_responses(binding,tmp_path):
    result,requests=approved_run(binding,args_for(binding,tmp_path))
    assert result["passed"] and result["reviewed_suite_used"] and result["generated_public_checks_executed"]
    assert result["model_dispatch_admissions"]==result["model_responses_received"]==len(requests)==1
    assert result["execution"]["verified"] is result["counts_as_success"] is False
    assert result["remaining_cost_reservation_microusd"]==0


def test_cli_does_not_claim_model_use_when_execution_stops_before_any_request(binding,tmp_path):
    result,requests=approved_run(binding,args_for(binding,tmp_path),stop_before_dispatch=True)
    assert not result["passed"] and not result["real_model_used"] and not requests
    assert result["model_dispatch_outcome"]=="not_started"
    assert result["model_dispatch_admissions"]==result["model_responses_received"]==0
    assert result["generated_public_checks_executed"] is False
