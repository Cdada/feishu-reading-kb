#!/usr/bin/env python3
"""Operator-only B03, distinct bytes and a new explicit authorization window."""
import os
from pathlib import Path
import subprocess
import sys
from b01_execute import private_approval, source_check, obtain_code, report_result
from f02_execute import PACKAGE
from kb_custody_v21.b03 import (APPROVAL, RETRY_APPROVAL, RUNTIME, DistinctArchiveExperiment,
                               StartupRetryExperiment, zero_request_predecessor, prepare)
from kb_custody_v21.baidu_credentials import LoginKeychainReader
from kb_custody_v21.baidu_protocol import json_object
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import sha


def execute(*, startup_retry=False):
    os.umask(0o077)
    worktree = Path(subprocess.check_output(['git','-C',str(PACKAGE),'rev-parse','--show-toplevel'],text=True).strip())
    path = worktree/(RETRY_APPROVAL if startup_retry else APPROVAL)
    frozen = private_approval(path); approval = json_object(frozen)
    if (approval.get('approved') is not True or approval.get('runtime_relative') != RUNTIME
            or approval.get('worktree') != str(worktree)):
        raise ContractError('independent B03 approval and fixed runtime required')
    check = lambda: source_check(worktree,path,frozen)
    check()
    if not sys.stdin.isatty(): raise ContractError('operator TTY required for new OAuth')
    if startup_retry:
        zero_request_predecessor(worktree, approval)
    elif 'attempt' in approval:
        raise ContractError('retry requires its separate explicit entry')
    registry, _ = prepare(worktree)
    cls = StartupRetryExperiment if startup_retry else DistinctArchiveExperiment
    experiment = cls(registry, approval, checked_source=check)
    experiment.append({'binding':'approved_source','value':dict(commit=approval['source_commit'],
        source_sha256=approval['source_snapshot']['sha256'],approval_sha256=sha(frozen))})
    def load(app_id, app_name, before):
        before()
        return LoginKeychainReader().read_application(app_id,app_name,before_read=before)
    result = experiment.run(load_credentials=load,obtain_code=obtain_code)
    report_result(result)
    return 0 if result['status']=='BOUNDED_BAIDU_UPLOAD_VERIFIED' else 1


if __name__=='__main__':
    if sys.argv[1:] not in (['--execute'],['--execute-r1']):
        raise SystemExit('explicit --execute or --execute-r1 and its separate B03 approval required')
    try: sys.exit(execute(startup_retry=sys.argv[1:] == ['--execute-r1']))
    except (Exception,KeyboardInterrupt) as error:
        print('STOPPED: '+type(error).__name__+'; preserve all evidence'); sys.exit(1)
