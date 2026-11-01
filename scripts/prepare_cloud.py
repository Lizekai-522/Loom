"""Initialize a new invite-only Linux instance; no network calls or paid work."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets

from bootstrap_local import write_private
from configure_verification import configure
from prepare_public_preview import prepare
from loom.auth import load_identities
from loom.local_config import load_project_secrets
from loom.public_access import PreviewAccess
from loom.quotas import Limit, QuotaPolicy, QuotaService
from loom.store import Store


def initialize(work_root, *, origin, image, secrets_file):
    """Refuse existing state and secrets inside the distributed source tree."""
    repo=Path(__file__).resolve().parents[1]
    root=Path(work_root).absolute()
    if (root.is_symlink() or root.exists() or root==Path(root.anchor)
            or root.resolve().is_relative_to(repo.parent)):
        raise ValueError('new private state directory outside source required')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
        raise ValueError('immutable sandbox image required')
    # Validate all inputs before creating any files. Never accept a key on CLI.
    PreviewAccess(origin,secrets.token_urlsafe(32),secrets.token_urlsafe(32))
    if Path(secrets_file).resolve().is_relative_to(repo.parent):
        raise ValueError('secret input must be outside distributed source')
    project_secrets=load_project_secrets(secrets_file,allowed_names={'DEEPSEEK_API_KEY'})
    if 'DEEPSEEK_API_KEY' not in project_secrets:raise ValueError('model secret required')
    previous=os.umask(0o077)
    try:
        root.mkdir(parents=True,mode=0o700)
        private=root/'loom-private';private.mkdir(mode=0o700)
        write_private(private/'model-secrets.json',dict(project_secrets))
        (root/'loom-sandbox-image.id').write_text(image+'\n',encoding='utf-8')
        result=prepare(root)
        public=root/'loom-public-preview';public.chmod(0o700)
        access_path=public/'access.json';access=json.loads(access_path.read_bytes())
        access['origin']=origin;write_private(access_path,access)
        models=private/'models-dual.json'
        write_private(models,json.loads((repo/'configs/models.dual.example.json').read_bytes()))
        config=configure(public/'verification',tenant='public-preview',identities=public/'identities.json',
            model_config=models,model_secrets=private/'model-secrets.json',db=public/'preview.db',pool_limit=2000000)
        executor_path=public/'executor.json';executor=json.loads(executor_path.read_bytes())
        executor.update(model_config=str(models),verification_config=str(config))
        write_private(executor_path,executor)
        store=Store(public/'preview.db')
        operator=next(i for i in load_identities(public/'identities.json') if 'operator' in i.roles)
        QuotaService(store).configure(operator,QuotaPolicy(60,Limit(12,1),Limit(8,1),Limit(12,1)),
            key='cloud-initial-quotas-v1',expected_revision=0,reason='Single-host invite preview admission limits')
        # The invite is a private file, never stdout or a public deployment report.
        invite=public/'访问信息.txt'
        invite.write_text(origin+'/console\n访问码：'+access['access_code']+'\n',encoding='utf-8')
        invite.chmod(0o600)
        return dict(passed=True,initialized=True,model_requests=0,existing_state_overwritten=False,
                    production_ready=False,run_id=result['run_id'])
    finally:
        os.umask(previous)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('work-root','origin','image','secrets-file'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(initialize(args.work_root,origin=args.origin,image=args.image,secrets_file=args.secrets_file)))
        return 0
    except Exception:
        # Do not include exceptions with private paths or provider data.
        print('{"passed":false,"code":"CLOUD_INITIALIZATION_FAILED","automatic_retry":false}')
        return 1


if __name__=='__main__':raise SystemExit(main())
