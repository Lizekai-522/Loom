import importlib.util
import json
from pathlib import Path

import pytest

from loom.store import Store,InvalidInput
from loom.verification_config import load_verification_config
from tests.test_approved_execution import OP,WORKER

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('configure_verify_fixture',ROOT/'scripts/configure_verification.py')
script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)

def test_private_key_configuration_is_encrypted_distinct_and_never_overwritten(tmp_path):
    source=tmp_path/'source.db';Store(source)
    identities=tmp_path/'identities.json';identities.write_text(json.dumps(dict(schema_version='loom.local-identities/v1',identities=[dict(tenant_id=i.tenant_id,subject_id=i.subject_id,roles=sorted(i.roles),token=i.token) for i in (OP,WORKER)])),encoding='utf-8')
    directory=tmp_path/'private-keys'
    path=script.configure(directory,tenant='a',identities=identities,model_config=ROOT/'configs/models.dual.example.json',model_secrets=tmp_path/'project-secret.json',db=source,pool_limit=2000000)
    config=load_verification_config(path);private,trusted=config.load_signers()
    assert len(private)==2 and config.identities()==(OP,WORKER)
    assert all(b'ENCRYPTED PRIVATE KEY' in p.read_bytes() for p in config.private_keys.values())
    from loom.verification_signatures import public_fingerprint
    assert len({public_fingerprint(k.public_key) for k in trusted.values()})==2
    with pytest.raises(InvalidInput):script.configure(directory,tenant='a',identities=identities,model_config=ROOT/'configs/models.dual.example.json',model_secrets=tmp_path/'project-secret.json',db=source,pool_limit=2000000)
    data=json.loads(path.read_bytes());data['workspace']=str(ROOT/'private-should-be-rejected')
    path.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(InvalidInput):load_verification_config(path)
