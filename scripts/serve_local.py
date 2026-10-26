"""使用本机私有配置启动回环工作台，不在命令或页面传递凭据。"""
import argparse
import json
import os
from pathlib import Path
import sys

from loom.cli import main as serve


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work-root',type=Path,required=True);args=parser.parse_args()
    work=args.work_root.resolve();session_path=work/'loom-first-round-session.json';session=json.loads(session_path.read_text(encoding='utf-8-sig'))
    session['process_id']=os.getpid();session_path.write_text(json.dumps(session,indent=2),encoding='utf-8')
    os.environ['LOOM_TENANT_KEYS']=json.dumps({session['tenant']:session['token']})
    sys.argv=['loom','serve','--port',str(session['port']),'--db',session['database'],'--identities-file',str(work/'loom-private/local-identities.json'),'--local-console','--executor-config',str(work/'loom-private/local-executor.json')]
    serve()


if __name__=='__main__':main()
