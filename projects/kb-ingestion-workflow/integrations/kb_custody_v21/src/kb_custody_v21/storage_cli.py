"""Operator-only P43 CLI. No command mounts, formats, switches or deletes a volume."""
import argparse
import json
import sqlite3
from pathlib import Path

from .contracts import ContractError
from .runtime import Runtime, no_symlinks
from .storage_layout import ContentStore, StorageUnavailable, initialize_store
from .storage_migration import create_plan, copy_plan, switch_preview


def main(argv=None):
    parser=argparse.ArgumentParser(description='资料存储检查与迁移预览；实际建卷、切换和删除不在此工具中。')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('inspect','initialize','plan','copy','switch-preview'):
        child=commands.add_parser(name)
        if name in ('inspect','initialize','plan'):
            child.add_argument('--target',required=True,help='本机目标卷/资料目录绑定 JSON')
        if name=='initialize':child.add_argument('--approve-store-id',required=True)
        if name in ('plan','copy','switch-preview'):
            child.add_argument('--worktree',required=True)
            child.add_argument('--runtime-config',required=True)
        if name in ('copy','switch-preview'):
            child.add_argument('--plan',required=True)
            child.add_argument('--plan-sha256',required=True)
    args=parser.parse_args(argv)
    try:
        target=json.loads(no_symlinks(Path(args.target).absolute()).read_text()) if hasattr(args,'target') else None
        runtime=Runtime.load(args.worktree,args.runtime_config) if hasattr(args,'worktree') else None
        if args.command=='inspect':value=ContentStore(target).check(fresh=True)
        elif args.command=='initialize':value=initialize_store(target,approved_store_id=args.approve_store_id)
        elif args.command=='plan':value=create_plan(runtime,target)
        elif args.command=='copy':value=copy_plan(runtime,args.plan,args.plan_sha256)
        else:value=switch_preview(runtime,args.plan,args.plan_sha256)
        print(json.dumps(dict(state='complete',result=value),ensure_ascii=False,indent=2))
        return 0
    except StorageUnavailable as error:
        print(json.dumps(dict(state='paused_storage',reason=error.reason,message=str(error)),ensure_ascii=False))
        return 3
    except (ContractError,OSError,ValueError,sqlite3.Error) as error:
        print(json.dumps(dict(state='needs_review',message=str(error),live_binding_changed=False),ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
