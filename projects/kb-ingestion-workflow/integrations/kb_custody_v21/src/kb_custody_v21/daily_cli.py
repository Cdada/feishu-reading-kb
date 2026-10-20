"""Explicit launcher CLI; importing it does not activate the service."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from .contracts import ContractError
from .daily_launcher import DailyLauncher
from .daily_status import local_status,user_summary
from .pilot_feishu import read_json
from .runtime import Runtime


def main(argv=None):
    parser=argparse.ArgumentParser(description='飞书知识库：启动、停止与本地状态')
    parser.add_argument('action',choices=('start','stop','status','check','run'))
    parser.add_argument('--rules',required=True)
    parser.add_argument('--sha256')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    config=read_json(Path(args.rules).absolute(),65536)
    runtime=Runtime(config['worktree'],config['runtime_config'])
    root=runtime.root/'conversion-work/daily-service'
    launcher=DailyLauncher(root)
    if args.action=='status':
        result=local_status(root)
    elif args.action=='stop':
        result=launcher.request_stop()
    elif args.action=='start' and launcher.status()['running']:
        result=dict(local_status(root),already_running=True)
    else:
        from .daily_authority import DailyAuthority
        authority=DailyAuthority(args.rules,args.sha256)
        if args.action=='check':
            result=dict(state='authorized',running=launcher.status()['running'],rules_sha256=authority.digest)
        elif args.action=='start':
            entry=Path(__file__).parents[2]/'daily.py'
            # Keep the established local runtime paths. Secrets continue to be
            # resolved only by the official CLI inside its credential gate.
            environment={k:v for k,v in os.environ.items() if k in (
                'HOME','PATH','TMPDIR','LANG','LC_ALL','PYTHONPATH','PYTHONDONTWRITEBYTECODE')}
            environment['PYTHONDONTWRITEBYTECODE']='1'
            result=launcher.start([sys.executable,str(entry),'run','--rules',str(authority.path),'--sha256',authority.digest],env=environment)
            result=dict(local_status(root),**result)
        else:
            from .daily_backend import DailyBackend
            from .daily_scheduler import DailyScheduler
            from .daily_store import DailyStore
            with launcher.own(configuration_sha=authority.digest) as instance:
                store=DailyStore(root/'queue.sqlite3',authority.policy)
                stop=threading.Event()
                def stop_signal(_number,_frame):
                    stop.set()
                old={s:signal.signal(s,stop_signal) for s in (signal.SIGINT,signal.SIGTERM)}
                try:
                    adapter=DailyBackend(authority,store)
                    scheduler=DailyScheduler(store,adapter,stop=stop,external_stop=lambda:launcher.stop_requested(instance))
                    scheduler.run()
                finally:
                    for s,handler in old.items():
                        signal.signal(s,handler)
            result=local_status(root)
    print(json.dumps(result,ensure_ascii=False,indent=2) if args.json else user_summary(result))
    return 0


def cli():
    try:
        return main()
    except (ContractError,KeyError,FileNotFoundError,PermissionError,BlockingIOError) as error:
        print('飞书知识库未启动或已暂停：'+str(error),file=sys.stderr)
        return 2
