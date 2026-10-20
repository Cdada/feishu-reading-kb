#!/usr/bin/env python3
"""Installed local launcher. All bindings are local; it never grants approval."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description='启动、停止或查看飞书知识库')
    parser.add_argument('action',choices=('start','stop','status','check'),nargs='?',default='status')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    binding=Path(__file__).absolute().with_name('launcher.local.json')
    config=json.loads(binding.read_text())
    if config.get('reference')!='P32-launcher-v1':
        raise ValueError('invalid local launcher binding')
    worktree=Path(config['worktree'])
    package=worktree/config['package_relative']
    root=worktree/config['runtime_relative']/'conversion-work/daily-service'
    if not package.is_relative_to(worktree) or not root.is_relative_to(worktree/'.dev-runtime'):
        raise ValueError('launcher path outside configured worktree')
    rules=config.get('rules_path')
    digest=config.get('rules_sha256')
    if args.action in ('start','check') and (not rules or not digest):
        result=dict(state='not_activated',running=False,
            message='启动器已安装；日常运行规则尚未集中批准并启用，目前不会调用飞书或正文模型。')
        print(json.dumps(result,ensure_ascii=False) if args.json else result['message'])
        return 2
    environment={k:v for k,v in os.environ.items() if k in ('HOME','PATH','TMPDIR','LANG','LC_ALL')}
    environment.update(PYTHONDONTWRITEBYTECODE='1',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',
        PYTHONPATH=os.pathsep.join([str(package/'src'),str(package),str(worktree/config['conversion_relative']),*config['python_dependencies']]))
    if args.action in ('status','stop'):
        script=package/'daily_local.py'
        command=[config['python'],str(script),args.action,'--root',str(root)]
    else:
        command=[config['python'],str(package/'daily.py'),args.action,'--rules',rules,'--sha256',digest]
    if args.json:command.append('--json')
    return subprocess.run(command,cwd=worktree,env=environment).returncode


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,KeyError) as error:
        print('启动器配置需要检查：'+str(error),file=sys.stderr)
        raise SystemExit(2)
