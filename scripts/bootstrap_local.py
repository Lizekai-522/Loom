"""准备源码之外的本机身份与执行器配置；不输出、不覆盖现有密钥。"""
import argparse
import json
from pathlib import Path
import secrets

from loom.auth import load_identities


def write_private(path,data):
    temporary=path.with_name(path.name+'.new')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(path)
    try:path.chmod(0o600)
    except OSError:pass


def prepare(work_root,port=8796):
    repo=Path(__file__).resolve().parents[1];work=Path(work_root).resolve()
    if work.is_relative_to(repo.parent) or work==Path(work.anchor):raise ValueError('private work directory must be outside public outputs')
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('invalid local port')
    private=work/'loom-private';private.mkdir(parents=True,exist_ok=True)
    session_path=work/'loom-first-round-session.json'
    if not session_path.exists():write_private(session_path,dict(tenant='first-round',token=secrets.token_urlsafe(32),port=port,base_url=f'http://127.0.0.1:{port}',database=str(work/'loom-first-round-api.db')))
    session=json.loads(session_path.read_text(encoding='utf-8-sig'))
    identities=private/'local-identities.json'
    if not identities.exists():write_private(identities,dict(schema_version='loom.local-identities/v1',identities=[dict(tenant_id=session['tenant'],subject_id='local-'+role,roles=[role],token=secrets.token_urlsafe(32)) for role in ('operator','worker','viewer')]))
    load_identities(identities)
    config=private/'local-executor.json'
    if not config.exists():
        image=work/'loom-sandbox-image.id'
        write_private(config,dict(schema_version='loom.local-executor/v1',executor_id='local-dev',identities_file=str(identities),secrets_file=str(private/'model-secrets.json'),
            model_config=str(repo/'configs/models.example.json'),sandbox_image=image.read_text().strip() if image.exists() else None,host_tests={}))
    return dict(passed=True,base_url=f'http://127.0.0.1:{session["port"]}/console',private_configuration_ready=True,model_key_loaded=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work-root',type=Path,default=Path(__file__).resolve().parents[3]/'work');parser.add_argument('--port',type=int,default=8796);args=parser.parse_args()
    print(json.dumps(prepare(args.work_root,args.port),ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
