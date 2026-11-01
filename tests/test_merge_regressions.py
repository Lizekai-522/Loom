"""Regression cases reproduced during integration of the release branch."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from loom.agents import validate_schema
from tests.test_platform import api, h
from tests.test_autopilot import case


@pytest.mark.parametrize('index',[0,256,511])
def test_array_items_are_checked_after_the_first_page(index):
    values=[0]*(index+1)
    values[index]='invalid integer'
    assert validate_schema({'type':'array','items':{'type':'integer'}},values)


def test_array_tail_cannot_bypass_http_function_validation(api):
    response=api['client'].post('/v1/function-calls/validate',headers=h(key=False),
        json={'schema':{'type':'array','items':{'type':'integer'}},'arguments':[0]*256+['invalid']})
    assert response.status_code==200
    assert response.json()['valid'] is False
    assert '$[256]' in response.json()['errors'][0]


def test_large_json_integers_do_not_crash_numeric_schema_checks():
    value=10**400
    assert validate_schema({'type':'number','minimum':value},value)==[]


@pytest.mark.parametrize('schema',[
    {'type':['string',{}]}, {'type':'string','minLength':'bad'},
    {'type':'object','properties':[]}, {'type':'object','required':42},
    {'type':'array','items':False},
])
def test_malformed_schema_returns_a_violation_instead_of_crashing(schema):
    assert validate_schema(schema,{})


@pytest.mark.parametrize('schema',[
    {'type':['string',{}]}, {'type':'string','minLength':'bad'},
    {'type':'object','properties':[]}, {'type':'object','required':42},
    {'type':'array','items':False},
])
def test_malformed_schema_is_a_safe_http_validation_result(api,schema):
    response=api['client'].post('/v1/function-calls/validate',headers=h(key=False),
        json={'schema':schema,'arguments':{}})
    assert response.status_code==200
    assert response.json()['valid'] is False


def test_browser_fixture_registers_the_required_human_identity(tmp_path):
    script=Path(__file__).resolve().parents[1]/'scripts/serve_console_fixture.py'
    spec=importlib.util.spec_from_file_location('merge_browser_fixture',script)
    fixture=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    captured=[]
    def serve(app,**kwargs):
        with TestClient(app,client=('127.0.0.1',50000),base_url='http://127.0.0.1:8798') as client:
            me=client.get('/v1/me',headers={'X-Loom-Console':'local'})
            assert me.status_code==200
            assert me.json()['roles']==['user']
            assert client.get('/console').status_code==200
        captured.append(True)
    with patch('sys.argv',[str(script),'--db',str(tmp_path/'ui.db'),'--local-console']),patch.object(fixture.uvicorn,'run',side_effect=serve):
        fixture.main()
    assert captured==[True]
