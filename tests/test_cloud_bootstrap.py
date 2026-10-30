"""Cloud preparation must keep secrets private and refuse existing state."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('cloud_bootstrap_script',SCRIPTS/'prepare_cloud.py')
cloud=importlib.util.module_from_spec(spec);spec.loader.exec_module(cloud)


def secret(tmp_path):
    path=tmp_path/'secret.json';path.write_text(json.dumps({'DEEPSEEK_API_KEY':'fixture-not-a-real-provider-key'}))
    path.chmod(0o600)
    return path


def test_new_cloud_has_real_private_signers_frozen_tests_and_single_inflight_quota(tmp_path,capsys):
    from loom.executor_config import load_executor_config,load_host_tests
    from loom.execution import freeze_tests
    from loom.verification_config import load_verification_config
    from loom.quotas import QuotaService
    from loom.store import Store
    state=tmp_path/'state'
    result=cloud.initialize(state,origin='https://loom.example.com',image='sha256:'+'1'*64,secrets_file=secret(tmp_path))
    assert result['passed'] and result['model_requests']==0 and not result['production_ready']
    private=state/'loom-public-preview';config=load_executor_config(private/'executor.json')
    assert len(config.host_tests)==1
    for digest,path in config.host_tests.items():assert freeze_tests(load_host_tests(path))[1]==digest
    verifier=load_verification_config(config.verification_config)
    signing,trusted=verifier.load_signers()
    assert len(signing)==len(trusted)==2
    assert config.model_config==verifier.model_config
    routes=json.loads(config.model_config.read_bytes())['routes']
    assert {r['model'] for r in routes}=={'deepseek-flash','deepseek-v4-pro'}
    store=Store(private/'preview.db')
    snapshot=store.snapshot('public-preview',result['run_id'])
    assert snapshot['status']=='draft' and snapshot['spent']==snapshot['reserved']==0
    assert store.verify_events(store.events('public-preview',result['run_id']))
    with store.connect() as db:
        policy=json.loads(db.execute('SELECT record FROM quota_policies').fetchone()[0])
        assert 'fixture-not-a-real-provider-key' not in str(list(db.execute('SELECT envelope FROM events')))
    assert policy['policy']['tenant']['max_in_flight']==1
    assert not capsys.readouterr().out
    if os.name!='nt':
        assert state.stat().st_mode & 0o077==0
        for path in [state/'loom-private/model-secrets.json',private/'identities.json',private/'访问信息.txt']:
            assert path.stat().st_mode & 0o077==0


def test_existing_state_is_untouched(tmp_path):
    state=tmp_path/'state';state.mkdir();marker=state/'keep';marker.write_text('original')
    with pytest.raises(ValueError):cloud.initialize(state,origin='https://loom.example.com',image='sha256:'+'1'*64,secrets_file=secret(tmp_path))
    assert list(state.iterdir())==[marker] and marker.read_text()=='original'


@pytest.mark.parametrize('origin,image',[('http://loom.example.com','sha256:'+'1'*64),('https://loom.example.com/path','sha256:'+'1'*64),('https://loom.example.com','mutable:latest')])
def test_bad_boundary_inputs_create_no_state(tmp_path,origin,image):
    state=tmp_path/'state'
    with pytest.raises(ValueError):cloud.initialize(state,origin=origin,image=image,secrets_file=secret(tmp_path))
    assert not state.exists()
