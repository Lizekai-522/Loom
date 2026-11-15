"""处理工作台已批准的本机执行请求；重启只核对账本，不自动重发。"""
import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from loom.executor_config import load_executor_config
from loom.local_executor import LocalExecutor
from loom.public_access import PreviewStore
from loom.store import Store,LoomError


def setup_logging(config_path):
    """Mirror process observations into a bounded rotating log beside the private
    configuration, so the settings page can show executor activity without shell access."""
    logs=Path(config_path).resolve().parent/'logs'
    logs.mkdir(mode=0o700,parents=True,exist_ok=True)
    handler=RotatingFileHandler(logs/'executor.log',maxBytes=1_000_000,backupCount=2,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root=logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return logs/'executor.log'


async def run(args):
    if not Path(args.db).is_file():raise ValueError('existing database required')
    config=load_executor_config(args.config)
    log_path=setup_logging(args.config)
    logging.getLogger('loom.executor').info('executor starting: db=%s log=%s',args.db,log_path)
    # The shared public preview keeps its durable goal/budget caps for goals the
    # autopilot creates inside this private worker, not only for HTTP requests.
    store=PreviewStore(args.db) if config.executor_id=='public-preview' else Store(args.db)
    executor=LocalExecutor(store,config)
    async with executor.status.monitor():
        # The resident assistant (conversation + background work + patrol) runs
        # beside the approved-request drain loop in the same private process.
        assistant=asyncio.create_task(executor.run_assistant()) if args.watch else None
        probes=asyncio.create_task(executor.run_probes()) if args.watch else None
        try:
            while True:
                result=await executor.drain_one()
                if result:
                    print(json.dumps(dict(request_id=result['request_id'],state=result['state'],counts_as_success=False)),flush=True)
                    logging.getLogger('loom.executor').info('drain: %s',json.dumps(result))
                if not args.watch:return
                await asyncio.sleep(0.5)
        finally:
            if assistant:assistant.cancel()
            if probes:probes.cancel()


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
