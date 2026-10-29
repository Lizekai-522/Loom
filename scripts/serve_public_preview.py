"""仅将受邀体验服务绑定到回环；公网隧道只能连接此端口。"""
import argparse
import json
from pathlib import Path

import uvicorn
from loom.api import create_app
from loom.auth import load_identities
from loom.executor_config import load_executor_config
from loom.public_access import PreviewAccess, PreviewStore, PublicPreview
from prepare_public_preview import create_example,create_verified_example


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work-root',required=True,type=Path);args=parser.parse_args()
    private=args.work_root.resolve()/'loom-public-preview'
    identities=load_identities(private/'identities.json')
    operator=next(i for i in identities if 'operator' in i.roles)
    def access():
        raw=(private/'access.json').read_bytes()
        if len(raw)>4096:raise ValueError('invalid access config')
        data=json.loads(raw)
        return PreviewAccess(data['origin'],data['access_code'],operator.token)
    access()
    port=json.loads((private/'access.json').read_bytes())['port']
    store=PreviewStore(private/'preview.db');executor=load_executor_config(private/'executor.json')
    app=create_app(store,{},identities=identities,local_console=False,executor_config=executor)
    def example(key):
        return create_example(store,Path(__file__).resolve().parents[1],next(iter(executor.host_tests)),operator,'preview-example:'+key)
    def verified_example(key):
        return create_verified_example(store,Path(__file__).resolve().parents[1],next(iter(executor.host_tests)),operator,'preview-verified:'+key)
    # No access log: login bodies and session cookies never enter request logs.
    uvicorn.run(PublicPreview(app,access,example=example,verified_example=verified_example),host='127.0.0.1',port=port,proxy_headers=False,access_log=False,log_level='warning')


if __name__=='__main__':main()
