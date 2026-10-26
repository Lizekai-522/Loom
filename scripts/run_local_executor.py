"""处理工作台已批准的本机执行请求；重启只核对账本，不自动重发。"""
import argparse
import asyncio
import json
from pathlib import Path

from loom.executor_config import load_executor_config
from loom.local_executor import LocalExecutor
from loom.store import Store,LoomError


async def run(args):
    if not Path(args.db).is_file():raise ValueError('existing database required')
    executor=LocalExecutor(Store(args.db),load_executor_config(args.config))
    while True:
        result=await executor.drain_one()
        if result:print(json.dumps(dict(request_id=result['request_id'],state=result['state'],counts_as_success=False)),flush=True)
        if not args.watch:return
        await asyncio.sleep(0.5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    try:asyncio.run(run(args))
    except (LoomError,OSError,ValueError,TypeError):
        print(json.dumps(dict(passed=False,error_code='LOCAL_EXECUTOR_STOPPED',message='本机执行器已停止，请核对私有配置和执行账本；请勿盲目重发。'),ensure_ascii=False));return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
