"""本机具名操作员配置独立复核项目额度和加密签名密钥；不调用模型。"""
import argparse
import json
from pathlib import Path
import secrets
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from loom.auth import load_identities
from loom.store import InvalidInput,LoomError
from loom.verification_config import MODELS,load_verification_config

def configure(directory,*,tenant,identities,model_config,model_secrets,db,pool_limit):
    directory=Path(directory).resolve();repo=Path(__file__).resolve().parents[1]
    if directory.is_relative_to(repo) or directory.exists() or not Path(db).is_file():raise InvalidInput('new private configuration directory required')
    actors=load_identities(identities)
    if len([i for i in actors if i.tenant_id==tenant and 'operator' in i.roles])!=1:raise InvalidInput('named tenant operator required')
    if type(pool_limit) is not int or not 0<=pool_limit<=10000000:raise InvalidInput('invalid project pool')
    directory.mkdir(parents=True)
    passwords={model:secrets.token_urlsafe(40) for model in MODELS};private={};public={}
    for model in MODELS:
        private[model]=str(directory/(model+'.private.pem'));public[model]=str(directory/(model+'.public.pem'))
        key=Ed25519PrivateKey.generate()
        values=[(private[model],key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.BestAvailableEncryption(passwords[model].encode()))),
            (public[model],key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))]
        for name,data in values:
            with Path(name).open('xb') as stream:
                try:os.chmod(name,0o600)
                except OSError:pass
                stream.write(data);stream.flush();os.fsync(stream.fileno())
    secret_path=directory/'secrets.json'
    with secret_path.open('x',encoding='utf-8') as handle:json.dump(passwords,handle)
    try:secret_path.chmod(0o600)
    except OSError:pass
    config=dict(schema_version='loom.verification-runtime/v1',tenant_id=tenant,identities_file=str(Path(identities).resolve()),
        model_config=str(Path(model_config).resolve()),model_secrets_file=str(Path(model_secrets).resolve()),workspace=str(directory/'jobs'),
        secrets_file=str(secret_path),private_keys=private,public_keys=public,pool_limit_microusd=pool_limit)
    path=directory/'config.json'
    with path.open('x',encoding='utf-8') as handle:json.dump(config,handle,indent=2)
    loaded=load_verification_config(path);loaded.identities();loaded.load_signers()
    return path

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('directory','tenant','identities-file','model-config','model-secrets-file','db'):p.add_argument('--'+name,required=True)
    p.add_argument('--pool-limit-microusd',required=True,type=int)
    a=p.parse_args()
    try:
        configure(a.directory,tenant=a.tenant,identities=a.identities_file,model_config=a.model_config,model_secrets=a.model_secrets_file,db=a.db,pool_limit=a.pool_limit_microusd)
        print(json.dumps(dict(passed=True,configured=True,pool_limit_microusd=a.pool_limit_microusd,model_requests=0,private_values_printed=False)))
        return 0
    except (LoomError,OSError,ValueError,TypeError):
        print(json.dumps(dict(passed=False,code='VERIFICATION_CONFIGURATION_FAILED')));return 1
if __name__=='__main__':raise SystemExit(main())
