import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import pytest
from loom.model_gateway import ModelGateway, ModelRoute
from tests.test_approved_execution import OP, WORKER
from tests.test_repair import SequenceSandbox
from tests.test_suite_binding import ACCEPTANCE, binding, approve_binding

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('approved_cli',ROOT/'scripts/run_approved_task.py')
script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)

def args_for(b,tmp_path):
    saved,_=approve_binding(b)
    tests=tmp_path/'host.json'
    tests.write_text(json.dumps({'tests':[{'name':t.name,'input':t.input,'expected':t.expected} for t in ACCEPTANCE]}),encoding='utf-8')
    return SimpleNamespace(db=b['store'].path,tenant='a',run_id=b['target'],node_id='work',expected_plan_digest=saved['digest'],
        tests_file=tests,max_output_tokens=768,execute=False,preview_digest=None,identities_file=None,secrets_file=None,
        sandbox_image=None,model_config=ROOT/'configs/models.example.json')

def test_cli_preview_reads_no_credentials_models_or_docker(binding,tmp_path):
    args=args_for(binding,tmp_path)
    with patch.object(script,'Store',side_effect=AssertionError('write store')),\
         patch.object(script,'load_project_secrets',side_effect=AssertionError('secret')),\
         patch.object(script,'load_identities',side_effect=AssertionError('identity')),\
         patch.object(script,'DockerSandbox',side_effect=AssertionError('Docker')):
        result=asyncio.run(script.run(args))
    assert result['mode']=='existing-plan-preview' and not result['creates_new_goal']

def test_execute_requires_the_exact_current_preview_before_reading_private_configuration(binding,tmp_path):
    args=args_for(binding,tmp_path);args.execute=True;args.preview_digest='0'*64
    with patch.object(script,'load_identities',side_effect=AssertionError('identity')),pytest.raises(ValueError,match='CURRENT_PREVIEW_REQUIRED'):
        asyncio.run(script.run(args))

def test_cli_executes_the_existing_run_once_and_preserves_plan(binding,tmp_path):
    b=binding;args=args_for(b,tmp_path)
    args.preview_digest=asyncio.run(script.run(args))['preview_digest'];args.execute=True
    args.identities_file=tmp_path/'not-read-identities.json';args.secrets_file=tmp_path/'not-read-secret.json';args.sandbox_image='sha256:'+'1'*64
    plan=b['workflow'].get_plan('a',b['target']);requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'id':'approved-cli-fixture','object':'chat.completion','created':1792700000,'model':'deepseek-flash',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({'source':'print(1)'})}}],
            'usage':{'prompt_tokens':25,'completion_tokens':10,'total_tokens':35}})
    gateway=ModelGateway([ModelRoute('deepseek-flash',timeout_seconds=1)],transport=httpx.MockTransport(handler),environ={'DEEPSEEK_API_KEY':'approved-cli-public-fixture-only'})
    with patch.object(script,'load_identities',return_value=(OP,WORKER)),\
         patch.object(script,'load_project_secrets',return_value={'DEEPSEEK_API_KEY':'approved-cli-public-fixture-only'}),\
         patch.object(script,'ModelGateway',return_value=gateway),patch.object(script,'DockerSandbox',return_value=SequenceSandbox([True,True])):
        report=asyncio.run(script.run(args))
    assert report['passed'] and report['run_id']==b['target'] and not report['creates_new_goal']
    assert len(requests)==1 and report['execution']['status']=='submitted'
    assert b['workflow'].get_plan('a',b['target'])==plan
    with patch.object(script,'load_identities',side_effect=AssertionError('no second private read')),pytest.raises(Exception,match='already started'):
        asyncio.run(script.run(args))

def test_host_file_duplicate_keys_and_unsupported_fields_rejected(tmp_path):
    path=tmp_path/'host.json';path.write_text('{"tests":[],"tests":[]}',encoding='utf-8')
    with pytest.raises(ValueError,match='DUPLICATE_TEST_FIELD'):script.load_tests(path)
    path.write_text('{"tests":[],"secret":"unexpected"}',encoding='utf-8')
    with pytest.raises(ValueError,match='HOST_TESTS_INVALID'):script.load_tests(path)
