"""只读计算宿主终验摘要，不连接模型、数据库或容器。"""
import argparse
import json
from run_approved_task import load_tests
from loom.execution import freeze_tests
from loom.store import LoomError

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tests-file',required=True)
    args=parser.parse_args()
    try:
        cases,digest=freeze_tests(load_tests(args.tests_file))
    except (LoomError,ValueError,TypeError,OSError,RecursionError):parser.error('用例文件无效，请检查结构、编码、大小及重复字段。')
    print(json.dumps(dict(evaluation_digest=digest,host_case_count=len(cases),network_requests=0,executes_code=False)))
