#!/usr/bin/env python3
"""Separately approved read-only B02; old B01 is never restarted or edited."""
import os
from pathlib import Path
import subprocess
import sys
from b01_execute import private_approval, source_check, obtain_code
from f02_execute import PACKAGE
from kb_custody_v21.b02 import APPROVAL, RUNTIME, BaiduObservation
from kb_custody_v21.baidu_credentials import LoginKeychainReader
from kb_custody_v21.baidu_protocol import json_object
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import sha


def execute():
    os.umask(0o077)
    worktree=Path(subprocess.check_output(['git','-C',str(PACKAGE),'rev-parse','--show-toplevel'],text=True).strip())
    path=worktree/APPROVAL; frozen=private_approval(path); approval=json_object(frozen)
    if approval.get('approved') is not True or approval.get('runtime_relative')!=RUNTIME:
        raise ContractError('independent B02 approval and fixed observation runtime required')
    check=lambda: source_check(worktree,path,frozen)
    check()
    if not sys.stdin.isatty(): raise ContractError('operator TTY required for new OAuth')
    client=BaiduObservation(worktree,approval,checked_source=check)
    client.append({'binding':'approved_source','value':dict(commit=approval['source_commit'],
        source_sha256=approval['source_snapshot']['sha256'],approval_sha256=sha(frozen))})
    def load(app_id,app_name,before):
        before()
        return LoginKeychainReader().read_application(app_id,app_name,before_read=before)
    result=client.run(load_credentials=load,obtain_code=obtain_code)
    print(result['status']+'; old unknown/holds unchanged; no retry')
    return 1 if result['status']=='STOPPED' else 0


if __name__=='__main__':
    if sys.argv[1:]!=['--execute']: raise SystemExit('explicit --execute and separate B02 approval required')
    try: sys.exit(execute())
    except (Exception,KeyboardInterrupt) as error:
        print('STOPPED: '+type(error).__name__+'; preserve B01 and B02 evidence'); sys.exit(1)
