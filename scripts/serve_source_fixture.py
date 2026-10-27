"""源码界面离线验收样例：提供方和沙箱均为替身，不读取项目密钥。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

import uvicorn


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root',required=True,type=Path)
    parser.add_argument('--port',type=int,default=8804)
    args=parser.parse_args();repo=Path(__file__).resolve().parents[1];root=args.work_root.resolve()
    if root.exists() or root.is_relative_to(repo.parent) or root==Path(root.anchor):
        parser.error('use a new private fixture directory outside public outputs')
    if not 1024<=args.port<=65535:parser.error('invalid local fixture port')
    sys.path.insert(0,str(repo))
    from tests.test_review_inputs import review_case
    from tests.test_execution import SOURCE
    from tests.test_approved_execution import OP,WORKER
    from loom.auth import LocalIdentity
    from loom.api import create_app
    root.mkdir(parents=True)
    case=review_case.__wrapped__(root)
    source=SOURCE+'\n# 中文 😀 \u2028 <img src=x onerror="globalThis.loomInjected=1"> </code><script>globalThis.loomInjected=2</script>\n'
    result=asyncio.run(case['execute'](source=source))
    if result['status']!='submitted':raise RuntimeError('SOURCE_FIXTURE_FAILED')
    draft=case['workflow'].create_goal('a','source-other-draft',title='切换任务的草案',description='仅检验迟到响应清理',acceptance=['schema.valid'])
    viewer=LocalIdentity('a','viewer',frozenset({'viewer'}),'source-viewer-public-fixture-00000000')
    report=dict(run_id=case['run'],task_id=case['claim'].task_id,other_run_id=draft['run_id'],source=source,
                provider_requests_are_mock=True,real_provider_requests=0)
    (root/'fixture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    uvicorn.run(create_app(case['store'],{},identities=(OP,WORKER,viewer),local_console=True),host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
