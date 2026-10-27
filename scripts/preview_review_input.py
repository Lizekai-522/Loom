"""只读核对审查输入或审查候选，不调用模型、不批准、不执行修复。"""
import argparse
import json
from pathlib import Path

from loom.approved_execution import ReadOnlyStore
from loom.review_inputs import ReviewInputService
from loom.store import LoomError


def load_candidate(path):
    raw = Path(path).read_bytes()
    if len(raw) > 32768:
        raise ValueError("candidate too large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("db", "tenant", "run-id", "task-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--include-source", action="store_true", help="显式包含已绑定的源码；默认仅输出摘要")
    parser.add_argument("--candidate-file", type=Path, help="只校验候选结构、精确来源与代码行号，不保存候选")
    args = parser.parse_args(argv)
    try:
        service = ReviewInputService(ReadOnlyStore(args.db))
        if args.candidate_file:
            result = service.validate_findings(args.tenant, args.run_id, args.task_id, load_candidate(args.candidate_file))
        else:
            result = service.preview(args.tenant, args.run_id, args.task_id, include_source=args.include_source)
    except (LoomError, OSError, ValueError, TypeError, RecursionError) as error:
        print(json.dumps({"passed": False, "error_code": getattr(error, "code", "REVIEW_PREVIEW_FAILED"),
                          "message": "审查输入未通过，请核对运行、任务和证据版本。"}, ensure_ascii=True))
        return 1
    print(json.dumps({"passed": True, **result}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
