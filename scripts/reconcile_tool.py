"""本机具名操作员核对未知工具调用，不改模型费用、不自动重跑。"""
import argparse
import json
from pathlib import Path

from loom.auth import load_identities
from loom.store import Store,LoomError
from loom.tool_recovery import ToolRecoveryService

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--identities-file',required=True,type=Path)
    parser.add_argument('--tenant',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--call-id',required=True)
    parser.add_argument('--expected-epoch',required=True,type=int)
    parser.add_argument('--expected-request-hash',required=True)
    parser.add_argument('--decision',required=True,choices=('completed','not_executed'))
    parser.add_argument('--reference',required=True)
    parser.add_argument('--summary',required=True)
    parser.add_argument('--key',required=True)
    args=parser.parse_args()
    try:
        if not args.db.is_file():raise ValueError
        identities=[i for i in load_identities(args.identities_file) if i.tenant_id==args.tenant and 'operator' in i.roles]
        if len(identities)!=1:raise ValueError
        result=ToolRecoveryService(Store(args.db)).reconcile(identities[0],args.run_id,args.call_id,args.key,
            expected_epoch=args.expected_epoch,expected_request_hash=args.expected_request_hash,decision=args.decision,
            evidence=dict(reference=args.reference,summary=args.summary))
        print(json.dumps(dict(passed=True,state=result['state'],counts_as_success=False,billing_changed=False,automatic_retry=False)))
        return 0
    except (LoomError,OSError,ValueError,TypeError):
        print(json.dumps(dict(passed=False,code='TOOL_RECONCILIATION_FAILED',message='工具核对未通过；请核对本机操作员、调用绑定、活动进程及原始日志。'),ensure_ascii=False))
        return 1

if __name__=='__main__':raise SystemExit(main())
