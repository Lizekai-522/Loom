"""用本机具名操作员登记持久配额；不调用模型，不释放未知调用。"""
import argparse
import json
from pathlib import Path

from loom.auth import load_identities
from loom.quotas import QuotaService,QuotaPolicy
from loom.store import Store,LoomError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--identities-file',required=True,type=Path)
    parser.add_argument('--policy-file',required=True,type=Path)
    parser.add_argument('--tenant',required=True)
    parser.add_argument('--expected-revision',required=True,type=int)
    parser.add_argument('--key',required=True)
    parser.add_argument('--reason',required=True)
    args=parser.parse_args()
    try:
        if not args.db.is_file():raise ValueError
        raw=args.policy_file.read_bytes()
        if len(raw)>8192:raise ValueError
        def unique(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError
                result[key]=value
            return result
        policy=QuotaPolicy.parse(json.loads(raw,object_pairs_hook=unique))
        identities=[i for i in load_identities(args.identities_file) if i.tenant_id==args.tenant and 'operator' in i.roles]
        if len(identities)!=1:raise ValueError
        result=QuotaService(Store(args.db)).configure(identities[0],policy,key=args.key,expected_revision=args.expected_revision,reason=args.reason)
        print(json.dumps(dict(passed=True,**result,model_dispatched=False,counts_as_success=False)))
        return 0
    except (LoomError,OSError,ValueError,TypeError):
        print(json.dumps(dict(passed=False,code='QUOTA_CONFIGURATION_FAILED',message='配额配置未通过，请核对具名操作员、策略和当前修订；未知调用不会自动解除。'),ensure_ascii=False))
        return 1


if __name__=='__main__':raise SystemExit(main())
