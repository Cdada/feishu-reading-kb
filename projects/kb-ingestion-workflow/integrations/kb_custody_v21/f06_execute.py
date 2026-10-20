#!/usr/bin/env python3
"""Explicit F06 one-shot entry. Preparation is local; execution needs approval."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from f02_execute import PACKAGE, checked_source, write_private
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import CliProcess, file_sha
from kb_custody_v21.f06 import OutboxExperiment, LIMITS, RUNTIME, prepare
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import DEFAULTS, Runtime, no_symlinks


def execute():
    os.umask(0o077)
    worktree = Path(subprocess.check_output(['git','-C',str(PACKAGE),'rev-parse','--show-toplevel'],text=True).strip())
    base = worktree/'.dev-runtime/kb-v21/real-validation/f01-v1'
    approval_path = no_symlinks(base/'f06-approval.local.json')
    digest = file_sha(approval_path)
    approval = json.loads(approval_path.read_text())
    source = checked_source(worktree,approval)
    if approval.get('approved') is not True or approval.get('runtime_relative')!=RUNTIME:
        raise ContractError('final F06 approval and fixed isolated registry required')
    registry = Registry(Runtime(worktree,DEFAULTS,worktree/RUNTIME))
    process = CliProcess(worktree/'.dev-runtime/kb-v21/f06-cli-guard',approval['cli_guard_sha256'])
    def check_execution():
        checked_source(worktree,approval)
        if file_sha(approval_path)!=digest:
            raise ContractError('F06 approval changed')
        process.check()
    def guarded(control):
        check_execution()
        control['frozen'].update({str(worktree/n):h for n,h in approval['source_snapshot']['files'].items()})
        control['frozen'][str(approval_path)]=digest
        return process(control)
    client = OutboxExperiment(base,approval,registry,runner=guarded)
    result = dict(started_at=datetime.now(timezone.utc).isoformat(),status='STOPPED',
        real_user_entry='NOT_RUN',unknown_recovery='NOT_RUN',end_to_end='NOT_RUN')
    try:
        write_private(client.root/'executed-source.local.json',dict(source,cli_guard_sha256=approval['cli_guard_sha256']))
        client.run()
        result['status']='NORMAL_OUTBOX_VERIFIED'
    except Exception as error:
        result['error_type']=type(error).__name__
    finally:
        result.update(ended_at=datetime.now(timezone.utc).isoformat(),observations=client.observations(),
            outbox_delivered=client.value('outbox_delivered'),
            reopened_worker_skipped_delivered=client.value('reopened_worker_skipped_delivered'))
        try:
            try:
                events=client.http_events()
                result['physical_requests']={k:sum(e.get('type')=='intent' and e.get('kind')==k for e in events) for k in LIMITS}
                result['unfinished_requests']=len({e['key'] for e in events if e.get('type')=='intent'}-
                                                  {e['key'] for e in events if e.get('type')=='complete'})
                result['outbox_state']=registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state']
            except (OSError,ValueError,KeyError,ContractError) as error:
                result.update(status='STOPPED',evidence_error_type=type(error).__name__,physical_requests='UNVERIFIED')
            if result['status']=='NORMAL_OUTBOX_VERIFIED':
                try:
                    check_execution()
                    client.check()
                except (OSError,ValueError,ContractError,subprocess.CalledProcessError) as error:
                    result.update(status='STOPPED',final_validation_error_type=type(error).__name__)
                    client.append(dict(stopped='final_validation',error_type=type(error).__name__))
            write_private(client.root/'result.local.json',result)
        finally:
            client.close()
    print(result['status']+'; private evidence retained; no retry')
    return 0 if result['status']=='NORMAL_OUTBOX_VERIFIED' else 1


if __name__=='__main__':
    if sys.argv[1:]!=['--execute']:
        raise SystemExit('explicit --execute and fixed F06 approval required')
    try: sys.exit(execute())
    except Exception as error:
        print('STOPPED: '+type(error).__name__); sys.exit(1)
