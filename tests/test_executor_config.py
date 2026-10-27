import importlib.util
import json
from pathlib import Path

import pytest

from loom.executor_config import load_executor_config
from loom.execution import freeze_tests
from loom.store import InvalidInput
from tests.test_approved_node import node_case,ROOT

module=importlib.util.spec_from_file_location('bootstrap_local',ROOT/'scripts/bootstrap_local.py')
bootstrap=importlib.util.module_from_spec(module);module.loader.exec_module(bootstrap)


def test_bootstrap_preserves_existing_credentials_and_never_loads_model_key(tmp_path):
    first=bootstrap.prepare(tmp_path/'private-work');assert first['passed'] and not first['model_key_loaded']
    paths=[tmp_path/'private-work/loom-first-round-session.json',tmp_path/'private-work/loom-private/local-identities.json']
    before=[p.read_bytes() for p in paths];second=bootstrap.prepare(tmp_path/'private-work')
    assert before==[p.read_bytes() for p in paths] and first==second
    assert not (tmp_path/'private-work/loom-private/model-secrets.json').exists()


def test_bootstrap_rejects_public_output_location():
    with pytest.raises(ValueError,match='outside public'):bootstrap.prepare(ROOT/'output/forbidden-private')


@pytest.mark.parametrize('change',['extra-key','invalid-image','duplicate-key','invalid-registry'])
def test_executor_config_rejects_invalid_or_secret_inline_fields(tmp_path,change):
    bootstrap.prepare(tmp_path/'work');path=tmp_path/'work/loom-private/local-executor.json';data=json.loads(path.read_text())
    if change=='extra-key':data['api_key']='never-inline-keys'
    elif change=='invalid-image':data['sandbox_image']='latest'
    elif change=='invalid-registry':data['host_tests']={'not-a-hash':'file'}
    elif change=='duplicate-key':
        path.write_text('{"schema_version":1,"schema_version":2}',encoding='utf-8')
        with pytest.raises(InvalidInput):load_executor_config(path)
        return
    path.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(InvalidInput):load_executor_config(path)


def test_host_registry_can_refresh_without_changing_execution_policy(node_case,tmp_path):
    b=node_case('developer');private=tmp_path/'work';bootstrap.prepare(private)
    path=private/'loom-private/local-executor.json';config=load_executor_config(path)
    spec=b['workflow'].get_task_spec('a',b['run'],b['run']+':work')
    with pytest.raises(InvalidInput,match='NOT_REGISTERED'):config.tests_for(spec)
    data=json.loads(path.read_text());data['host_tests'][spec['evaluation_digest']]=str(b['args'].tests_file);path.write_text(json.dumps(data),encoding='utf-8')
    assert freeze_tests(config.tests_for(spec))[1]==spec['evaluation_digest']
    b['args'].tests_file.write_text('{"tests":[]}',encoding='utf-8')
    with pytest.raises(InvalidInput):config.tests_for(spec)
