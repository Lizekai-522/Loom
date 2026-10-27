"""登记已独立冻结的宿主用例，只记录摘要映射，不改变批准合同。"""
import argparse
import json
from pathlib import Path
from loom.executor_config import load_host_tests,load_executor_config
from loom.execution import freeze_tests
from bootstrap_local import write_private


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--tests-file',type=Path,required=True);parser.add_argument('--expected-digest',required=True);args=parser.parse_args()
    load_executor_config(args.config)
    tests=load_host_tests(args.tests_file);actual=freeze_tests(tests)[1]
    if actual!=args.expected_digest:parser.error('宿主用例与预期冻结摘要不一致')
    data=json.loads(args.config.read_text(encoding='utf-8-sig'));data['host_tests'][actual]=str(args.tests_file.resolve());write_private(args.config,data)
    print(json.dumps(dict(passed=True,evaluation_digest=actual,host_case_count=len(tests),model_calls=0,contracts_changed=False)))


if __name__=='__main__':main()
