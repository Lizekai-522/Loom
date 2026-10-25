"""Existing approved run -> mock provider -> real Docker; no new Goal."""
import asyncio
import json
import os
from unittest.mock import patch
import httpx
import pytest
from loom.model_gateway import ModelGateway, ModelRoute
from tests.test_approved_execution import OP, WORKER
from tests.test_approved_task_script import args_for, script
from tests.test_suite_binding import binding

pytestmark=pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit approved Docker image required')
GOOD='import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"answer":x["value"]+1}))\n'
BAD='import json\nprint(json.dumps({"answer":0}))\n'

@pytest.mark.parametrize('sources',[(GOOD,),(BAD,GOOD)])
def test_existing_approved_cli_runs_in_real_docker_without_duplicate_goal(binding,tmp_path,sources):
    b=binding;args=args_for(b,tmp_path)
    before=b['workflow'].get_plan('a',b['target'])
    with b['store'].connect() as db:runs=db.execute('SELECT count(*) FROM runs').fetchone()[0]
    preview=asyncio.run(script.run(args))
    args.preview_digest=preview['preview_digest'];args.execute=True
    args.identities_file=tmp_path/'not-read-identity.json';args.secrets_file=tmp_path/'not-read-secret.json'
    args.sandbox_image=os.environ['LOOM_SANDBOX_TEST_IMAGE']
    remaining=list(sources);requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'id':'approved-real-docker-mock','object':'chat.completion','created':1792700000,'model':'deepseek-flash',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({'source':remaining.pop(0)})}}],
            'usage':{'prompt_tokens':25,'completion_tokens':10,'total_tokens':35}})
    gateway=ModelGateway([ModelRoute('deepseek-flash',timeout_seconds=1)],transport=httpx.MockTransport(handler),environ={'DEEPSEEK_API_KEY':'approved-docker-public-fixture-only'})
    with patch.object(script,'load_identities',return_value=(OP,WORKER)), patch.object(script,'load_project_secrets',return_value={'DEEPSEEK_API_KEY':'approved-docker-public-fixture-only'}), patch.object(script,'ModelGateway',return_value=gateway):
        report=asyncio.run(script.run(args))
    assert report['passed'] and report['run_id']==b['target'] and report['execution']['status']=='submitted'
    assert len(requests)==len(sources) and report['execution']['repairs_applied']==len(sources)-1
    assert len(report['execution']['development_cases'])==2 and len(report['execution']['cases'])==1
    assert all(case['passed'] for case in report['execution']['development_cases']+report['execution']['cases'])
    assert report['remaining_reservation']==0 and not report['counts_as_success']
    assert b['workflow'].get_plan('a',b['target'])==before
    with b['store'].connect() as db:assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==runs
    assert b['store'].claim_next('a',b['target'],'review',channel='verification') is None
