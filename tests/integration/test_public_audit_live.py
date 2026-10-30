"""An actual container echoes private stdin; invited HTTP cannot retrieve it."""
from dataclasses import replace
import json
import os

from fastapi.testclient import TestClient
import pytest

from loom.api import create_app
from loom.public_access import PublicPreview
from loom.sandbox import DockerSandbox,SandboxConfig
from tests.test_approved_execution import OP,WORKER
from tests.test_approved_node import node_case,execute
from tests.test_public_access import CONFIG,ORIGIN,login


@pytest.mark.skipif(not os.environ.get('LOOM_SANDBOX_TEST_IMAGE'),reason='explicit immutable Docker image required')
def test_real_program_cannot_exfiltrate_host_stdin_through_public_audit(node_case):
    b=node_case('developer')
    source="import sys,json\ndata=json.load(sys.stdin)\nsys.stderr.write('sensitive-stdin:'+str(data['value']))\nprint(json.dumps({'answer':data['value']+1}))\n"
    result,_=execute(b,dict(source=source),sandbox=DockerSandbox(SandboxConfig(os.environ['LOOM_SANDBOX_TEST_IMAGE'])))
    assert result['execution']['hard_criteria_passed']
    with b['store'].connect() as db:
        output=json.loads(db.execute('SELECT output FROM tasks WHERE id=?',(b['run']+':work',)).fetchone()[0])
    assert output['tests']['cases'][0]['stderr']=='sensitive-stdin:1'
    app=PublicPreview(create_app(b['store'],{},identities=(OP,WORKER)),lambda:replace(CONFIG,bearer=OP.token))
    with TestClient(app,base_url=ORIGIN) as client:
        login(client)
        for suffix in ('/events','/replay','/review'):
            response=client.get('/v1/runs/'+b['run']+suffix)
            assert response.status_code==200 and 'sensitive-stdin:1' not in response.text
            assert 'private-host' not in response.text
        view=client.get('/v1/runs/'+b['run']+'/tasks/'+b['run']+':work/review-input?include_source=true').json()
        assert view['files']['main.py']==source and view['execution_summary']['hard_criteria_passed']
        assert view['hidden_tests_included'] is False
    assert b['store'].verify_events(b['store'].events('a',b['run']))
