"""Operator-only backup entry point; never restarts or repoints the service."""
import argparse
import json
import sqlite3
import zipfile

from .content_backup import plan_backup, create_backup, restore_backup
from .contracts import ContractError
from .runtime import Runtime
from .storage_layout import StorageUnavailable


def main(argv=None):
    parser = argparse.ArgumentParser(description='内容备份与空目录恢复；不恢复运行服务，不上传，不删除原件。')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'create', 'restore'):
        child = commands.add_parser(name)
        if name in ('plan', 'create'):
            child.add_argument('--worktree', required=True)
            child.add_argument('--runtime-config', required=True)
        if name in ('plan', 'restore'):
            child.add_argument('--bundle', required=True)
        if name == 'create':
            child.add_argument('--plan', required=True)
            child.add_argument('--plan-sha256', required=True)
        if name == 'restore':
            child.add_argument('--sha256', required=True, help='包外单独保管的可信 SHA-256')
            child.add_argument('--target', required=True, help='现有父目录下尚不存在的新目录')
    args = parser.parse_args(argv)
    try:
        if args.command == 'restore':
            result = restore_backup(args.bundle, args.sha256, args.target)
        else:
            runtime = Runtime.load(args.worktree, args.runtime_config)
            result = (plan_backup(runtime, args.bundle) if args.command == 'plan' else
                      create_backup(runtime, args.plan, args.plan_sha256))
        print(json.dumps(dict(state='complete', result=result), ensure_ascii=False, indent=2))
        return 0
    except StorageUnavailable as error:
        print(json.dumps(dict(state='paused_storage', reason=error.reason, message=str(error)), ensure_ascii=False))
        return 3
    except (ContractError, OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, KeyError) as error:
        print(json.dumps(dict(state='needs_review', message=str(error),
            live_binding_changed=False, partial_files_retained=True), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
