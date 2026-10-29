"""Private verifier configuration. Reading readiness never loads private keys."""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey

from .auth import load_identities
from .local_config import load_project_secrets
from .store import InvalidInput
from .verification_signatures import TrustedVerifier

MODELS=('deepseek-flash','deepseek-v4-pro')
@dataclass(frozen=True)
class VerificationConfig:
    tenant_id:str
    identities_file:Path
    model_config:Path
    model_secrets_file:Path
    workspace:Path
    secrets_file:Path
    private_keys:dict
    public_keys:dict
    pool_limit_microusd:int
    path:Path

    def identities(self):
        identities=load_identities(self.identities_file)
        operators=[i for i in identities if i.tenant_id==self.tenant_id and 'operator' in i.roles]
        workers=[i for i in identities if i.tenant_id==self.tenant_id and 'worker' in i.roles]
        if len(operators)!=1 or len(workers)!=1:raise InvalidInput('ambiguous verification identities')
        return operators[0],workers[0]

    def load_signers(self):
        try:
            secrets=json.loads(self.secrets_file.read_bytes())
            if set(secrets)!=set(MODELS):raise ValueError
            private={};trusted={}
            for model in MODELS:
                raw=self.private_keys[model].read_bytes();public_raw=self.public_keys[model].read_bytes()
                if len(raw)>8192 or len(public_raw)>8192:raise ValueError
                key=serialization.load_pem_private_key(raw,password=secrets[model].encode())
                public=serialization.load_pem_public_key(public_raw)
                if not isinstance(key,Ed25519PrivateKey) or not isinstance(public,Ed25519PublicKey):raise ValueError
                if key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)!=public.public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw):raise ValueError
                private[model]=key
                trusted[model]=TrustedVerifier(model,'local-verifier:'+model,model,public)
            return private,trusted
        except (OSError,ValueError,TypeError,KeyError):raise InvalidInput('private verifier keys unavailable') from None

def load_verification_config(path):
    try:
        path=Path(path).resolve();raw=path.read_bytes()
        if len(raw)>65536:raise ValueError
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError
                result[key]=value
            return result
        data=json.loads(raw,object_pairs_hook=unique)
        if set(data)!={'schema_version','tenant_id','identities_file','model_config','model_secrets_file','workspace','secrets_file','private_keys','public_keys','pool_limit_microusd'}:raise ValueError
        if data['schema_version']!='loom.verification-runtime/v1' or not isinstance(data['tenant_id'],str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',data['tenant_id']):raise ValueError
        if type(data['pool_limit_microusd']) is not int or not 0<=data['pool_limit_microusd']<=10000000:raise ValueError
        repo=Path(__file__).resolve().parents[2]
        def resolve(value,*,private=False):
            if type(value) is not str or not value.strip() or len(value)>4096:raise ValueError
            p=Path(value);p=(path.parent/p).resolve() if not p.is_absolute() else p.resolve()
            if private and p.is_relative_to(repo):raise ValueError
            return p
        if set(data['private_keys'])!=set(MODELS) or set(data['public_keys'])!=set(MODELS):raise ValueError
        return VerificationConfig(data['tenant_id'],resolve(data['identities_file'],private=True),resolve(data['model_config']),
            resolve(data['model_secrets_file'],private=True),resolve(data['workspace'],private=True),resolve(data['secrets_file'],private=True),
            {model:resolve(data['private_keys'][model],private=True) for model in MODELS},
            {model:resolve(data['public_keys'][model],private=True) for model in MODELS},data['pool_limit_microusd'],path)
    except (OSError,ValueError,TypeError,KeyError,RecursionError):raise InvalidInput('invalid verification runtime configuration') from None
