"""Trusted local configuration references. Public previews never contain paths or secrets."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from .execution import freeze_tests
from .sandbox import FrozenTest
from .store import InvalidInput,digest


def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate key')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs)


def load_host_tests(path):
    raw=Path(path).read_bytes()
    if len(raw)>262144:raise InvalidInput('host tests exceed local limit')
    try:
        data=strict_json(raw)
        if type(data) is not dict or set(data)!={'tests'} or type(data['tests']) is not list:raise ValueError
        return freeze_tests([FrozenTest(**case) for case in data['tests']])[0]
    except (TypeError,ValueError,RecursionError):raise InvalidInput('invalid frozen local host tests') from None


@dataclass(frozen=True)
class LocalExecutorConfig:
    executor_id:str
    identities_file:Path
    secrets_file:Path
    model_config:Path
    sandbox_image:str|None
    host_tests:dict[str,Path]
    source_path:Path|None=None
    verification_config:Path|None=None

    def binding(self,role,evaluation_digest):
        raw=self.model_config.read_bytes()
        if len(raw)>262144:raise InvalidInput('model configuration exceeds limit')
        return digest(dict(executor_id=self.executor_id,model_config_sha256=sha256(raw).hexdigest(),
            sandbox_image=self.sandbox_image if role in {'developer','fixer'} else None,evaluation_digest=evaluation_digest,max_output_tokens=1024))

    def tests_for(self,spec):
        if spec['role'] in {'analyst','architect','tester','reviewer'}:return None
        registry=self.host_tests
        if self.source_path is not None:
            current=load_executor_config(self.source_path)
            if any(getattr(current,k)!=getattr(self,k) for k in ('executor_id','identities_file','secrets_file','model_config','sandbox_image','verification_config')):
                raise InvalidInput('local executor configuration changed; restart required')
            registry=current.host_tests
        path=registry.get(spec['evaluation_digest'])
        if path is None:raise InvalidInput('HOST_TESTS_NOT_REGISTERED')
        tests=load_host_tests(path)
        if freeze_tests(tests)[1]!=spec['evaluation_digest']:raise InvalidInput('HOST_TESTS_CHANGED')
        return tests


def load_executor_config(path):
    try:
        source=Path(path).resolve();raw=source.read_bytes()
        if len(raw)>65536:raise ValueError
        data=strict_json(raw)
        expected={'schema_version','executor_id','identities_file','secrets_file','model_config','sandbox_image','host_tests'}
        if (type(data) is not dict or set(data) not in (expected,expected|{'verification_config'}) or data['schema_version']!='loom.local-executor/v1'
            or type(data['executor_id']) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',data['executor_id'])
            or data['sandbox_image'] is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}',data['sandbox_image'])
            or type(data['host_tests']) is not dict or len(data['host_tests'])>128):raise ValueError
        def resolve(value):
            if type(value) is not str or not value.strip() or len(value)>4096:raise ValueError
            supplied=Path(value);return (source.parent/supplied).resolve() if not supplied.is_absolute() else supplied.resolve()
        paths={}
        for key,value in data['host_tests'].items():
            if not re.fullmatch('[0-9a-f]{64}',key):raise ValueError
            paths[key]=resolve(value)
        verifier=resolve(data['verification_config']) if data.get('verification_config') is not None else None
        return LocalExecutorConfig(data['executor_id'],resolve(data['identities_file']),resolve(data['secrets_file']),resolve(data['model_config']),data['sandbox_image'],paths,source,verifier)
    except (OSError,TypeError,ValueError,RecursionError):raise InvalidInput('invalid local executor configuration') from None
