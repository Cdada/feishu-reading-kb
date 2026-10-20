#!/usr/bin/env python3
"""Explicit one-shot F05 entry. No key prompt, no import-time platform access."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from f02_execute import PACKAGE, checked_source, write_private
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import CliProcess, NotificationReadExperiment, LIMITS, file_sha
from kb_custody_v21.runtime import no_symlinks


def execute():
    os.umask(0o077)
    worktree = Path(subprocess.check_output(['git','-C',str(PACKAGE),'rev-parse','--show-toplevel'],text=True).strip())
    base = worktree/'.dev-runtime/kb-v21/real-validation/f01-v1'
    approval_path = no_symlinks(base/'f05-approval.local.json')
    approval_digest = file_sha(approval_path)
    approval = json.loads(approval_path.read_text())
    source = checked_source(worktree,approval)
    if approval.get('approved') is not True:
        raise ContractError('F05 final execution approval required')
    process = CliProcess(worktree/'.dev-runtime/kb-v21/f05-cli-guard',approval['cli_guard_sha256'])
    def check_execution():
        checked_source(worktree,approval)
        if file_sha(approval_path) != approval_digest:
            raise ContractError('F05 approval changed')
        process.check()
    def guarded_process(control):
        check_execution()
        # Recheck the same approved inputs immediately before every physical call.
        control['frozen'].update({str(worktree/n):h for n,h in approval['source_snapshot']['files'].items()})
        control['frozen'][str(approval_path)] = approval_digest
        return process(control)
    client = NotificationReadExperiment(base/'f04-requests',approval,runner=guarded_process)
    outcome = dict(started_at=datetime.now(timezone.utc).isoformat(),status='STOPPED',
        original_f04_status='STOPPED',unknown_recovery='NOT_RUN',product_outbox='NOT_RUN',end_to_end='NOT_RUN')
    try:
        write_private(client.root/'executed-source.local.json',dict(source,cli_guard_sha256=approval['cli_guard_sha256']))
        client.run()
        outcome['status'] = 'KNOWN_NOTIFICATION_CONTENT_VERIFIED'
        outcome['update_anomaly'] = any(v['updated'] for v in client.observations().values())
    except Exception as error:
        outcome['error_type'] = type(error).__name__
    finally:
        outcome['ended_at'] = datetime.now(timezone.utc).isoformat()
        outcome['observations'] = client.observations()
        try:
            try:
                events = client.http_events()
                outcome['physical_requests'] = {k:sum(e.get('type')=='intent' and e.get('kind')==k for e in events) for k in LIMITS}
                outcome['unfinished_requests'] = len({e['key'] for e in events if e.get('type')=='intent'} -
                                                     {e['key'] for e in events if e.get('type')=='complete'})
            except (OSError, ValueError, KeyError, ContractError) as error:
                outcome.update(status='STOPPED', evidence_error_type=type(error).__name__,
                               physical_requests='UNVERIFIED', unfinished_requests='UNVERIFIED')
            if outcome['status'] == 'KNOWN_NOTIFICATION_CONTENT_VERIFIED':
                try:
                    check_execution()
                    client.check()
                except (OSError, ValueError, ContractError, subprocess.CalledProcessError) as error:
                    outcome.update(status='STOPPED', final_validation_error_type=type(error).__name__)
                    client.append(dict(stopped='final_validation',error_type=type(error).__name__))
            write_private(client.root/'result.local.json',outcome)
        finally:
            client.close()
    print(outcome['status']+'; private evidence retained; no retry')
    return 0 if outcome['status']=='KNOWN_NOTIFICATION_CONTENT_VERIFIED' else 1


if __name__=='__main__':
    if sys.argv[1:]!=['--execute']:
        raise SystemExit('explicit --execute and fixed F05 approval required')
    try: sys.exit(execute())
    except Exception as error:
        print('STOPPED: '+type(error).__name__); sys.exit(1)
