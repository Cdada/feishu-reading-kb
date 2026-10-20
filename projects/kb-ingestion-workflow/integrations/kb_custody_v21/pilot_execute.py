#!/usr/bin/env python3
"""Explicit P11 finite operator entry, with no credential prompt or daemon.

The separately confirmed approval binds a source manifest and all inputs.
Importing this module performs no platform access. No activation is shipped.
"""
from datetime import datetime, timedelta, timezone
import argparse
import os
from pathlib import Path
import subprocess

from f02_execute import checked_source, write_private
from kb_custody_v21.contracts import ContractError, stable_key
from kb_custody_v21.f05 import CliProcess, file_sha
from kb_custody_v21.pilot_feishu import read_json
from kb_custody_v21.pilot_guide_preparation import PilotGuidePreparation
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_notices import PilotExecutionSession
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.pilot_owner_followup import owner_followup
from kb_custody_v21.pilot_owner_wait import owner_wait
from kb_custody_v21.pilot_inbox import receive_inbox
from kb_custody_v21.pilot_daily import PilotDailyRun
from kb_custody_v21.pilot_service import PilotServiceRun
from kb_custody_v21.pilot_publication_inputs import reject_moving_inputs
from kb_custody_v21.pilot_publication_notice import publication_notice
from kb_custody_v21.pilot_run import PilotBoundedRun
from kb_custody_v21.pilot_text import CodexTextWire
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, no_symlinks
from kb_custody_v21.s6 import instant


def execution_inputs(root, approval_sha, *, clock=None):
    """Read-only startup validation, repeated before every external boundary."""
    clock = clock or (lambda:datetime.now(timezone.utc))
    root = no_symlinks(Path(root).absolute())
    path = root/'approval.local.json'
    if root.name != 'p11-requests' or '.dev-runtime' not in root.parts or file_sha(path) != approval_sha:
        raise ContractError('exact private P11 approval required')
    approval = read_json(path,65536)
    def window():
        if (approval.get('approved') is not True or approval.get('reference') != 'P11'
                or approval.get('root') != str(root)
                or not instant(approval['starts_at']) <= clock() < instant(approval['expires_at'])):
            raise ContractError('P11 is unapproved or outside its fixed window')
    window()
    manifest_path = no_symlinks(Path(approval['source_manifest']).absolute())
    if file_sha(manifest_path) != approval['source_manifest_sha256']:
        raise ContractError('execution source manifest changed')
    manifest = read_json(manifest_path,4*1024*1024)
    worktree = no_symlinks(Path(manifest['worktree']).absolute())
    checked_source(worktree,manifest)
    extra = manifest.get('approved_files')
    if not isinstance(extra,dict) or not extra:
        raise ContractError('explicit material, plan and executable bindings required')
    frozen = {str(worktree/n):digest for n,digest in manifest['source_snapshot']['files'].items()}
    for name,digest in extra.items():
        p = no_symlinks(Path(name).absolute())
        if str(p) != name or file_sha(p) != digest:
            raise ContractError('approved input changed')
        frozen[name]=digest
    reject_moving_inputs(Runtime(worktree,approval['runtime_config']),frozen)
    dependencies = ('run_plan','cli_guard') if approval.get('operation') in {'owner-followup','owner-wait','intake-convert','publication-notice','receive-inbox'} else (
        'run_plan','guide_plan','cli_guard','text_node','text_script','codex_binary')
    required = [approval[k] for k in dependencies]
    if any(str(no_symlinks(Path(p).absolute())) != p or p not in frozen for p in required):
        raise ContractError('executor dependencies must be included in the complete frozen manifest')
    if file_sha(path) != approval_sha or file_sha(manifest_path) != approval['source_manifest_sha256']:
        raise ContractError('approval or manifest changed during source verification')
    window()
    frozen[str(path)]=approval_sha
    frozen[str(manifest_path)]=approval['source_manifest_sha256']
    return worktree,approval,frozen


def execute(root, approval_sha, action, *, message_id=None, lookback_seconds=3600, max_pages=1):
    os.umask(0o077)
    if action not in {'intake','intake-convert','prepare','revise','continue','owner-reply','owner-followup','owner-wait','publication-notice','receive-inbox','inbox-daily','inbox-service'} or (action != 'owner-reply' and message_id is not None):
        raise ContractError('one explicit Pilot operation required')
    if action=='owner-reply' and (not message_id or type(lookback_seconds) is not int
            or not 1 <= lookback_seconds <= 86400 or type(max_pages) is not int or not 1 <= max_pages <= 12):
        raise ContractError('explicit reply identity and bounded query required')
    worktree,a,_ = execution_inputs(root,approval_sha)
    if (action=='owner-followup') != (a.get('operation')=='owner-followup'):
        raise ContractError('owner follow-up requires its own explicit approval')
    if (action=='owner-wait') != (a.get('operation')=='owner-wait'):
        raise ContractError('owner wait requires its own explicit approval')
    if (action in {'intake-convert','prepare'} or a.get('operation') in {'intake-convert','prepare'}) and a.get('operation') != action:
        raise ContractError('staged material operations require their own explicit approval')
    if (action=='publication-notice') != (a.get('operation')=='publication-notice'):
        raise ContractError('publication notice requires its own explicit approval')
    if (action=='receive-inbox') != (a.get('operation')=='receive-inbox'):
        raise ContractError('new file inbox requires its own explicit approval')
    if (action=='inbox-daily') != (a.get('operation')=='inbox-daily'):
        raise ContractError('daily new material requires its own explicit approval')
    if (action=='inbox-service') != (a.get('operation')=='inbox-service'):
        raise ContractError('continuous service requires its own explicit approval')
    run_plan = read_json(Path(a['run_plan']),65536)
    if (action == 'continue' and not {'revision','continuation'} <= run_plan.keys()
            or action == 'revise' and ('revision' not in run_plan or 'continuation' in run_plan)
            or action in {'intake','intake-convert'} and any(k in run_plan for k in ('revision','continuation','prepared_material'))
            or action == 'prepare' and ('prepared_material' not in run_plan or any(k in run_plan for k in ('revision','continuation')))):
        raise ContractError('operator action must match the explicitly approved material plan')
    root = no_symlinks(Path(root).absolute())
    # Reusing an entry point cannot create a new session/budget. A failed entry
    # keeps its marker and evidence; an operator must inspect before proceeding.
    marker = root/('entry-'+stable_key(action,message_id or action)+'.local.json')
    write_private(marker,dict(action=action,message_id=message_id,started_at=datetime.now(timezone.utc).isoformat()))
    session = controller = None
    result = dict(status='STOPPED',action=action,end_to_end='NOT_PASSED')
    try:
        def source():return execution_inputs(root,approval_sha)[2]
        frozen=source()
        process=CliProcess(a['cli_guard'],frozen[a['cli_guard']])
        def runner(control):
            source()
            return process(control)
        session=PilotExecutionSession(root,approval_sha,runner=runner,checked_source=source)
        runtime=Runtime(worktree,a['runtime_config'])
        registry=Registry(runtime)
        scope=PilotScope(a['app_id'],a['chat_id'],a['account_id'])
        intake=PilotIntake(registry,scope,max_attachments=1,max_pending=8)
        if action=='publication-notice':
            value=publication_notice(session,intake,a['run_plan'])
        elif action=='owner-followup':
            value=owner_followup(session,intake,a['run_plan'])
        elif action=='owner-wait':
            value=owner_wait(session,intake,a['run_plan'])
        elif action=='receive-inbox':
            value=receive_inbox(session,intake,a['run_plan'])
        elif action in {'inbox-daily','inbox-service'}:
            wire=CodexTextWire(a['text_node'],a['text_script'],a['codex_binary'],worktree)
            def ready():
                source();process.check();runtime.gate()
                if any(not os.access(a[k],os.X_OK) for k in ('cli_guard','text_node','codex_binary')):
                    raise ContractError('approved daily executables are not ready')
                return True
            cls=PilotServiceRun if action=='inbox-service' else PilotDailyRun
            controller=cls(session,intake,a['run_plan'],wire=wire,checked_ready=ready)
            value=controller.run()
        else:
            conversion_only = action == 'intake-convert'
            if conversion_only:
                def guide(*_):
                    raise ContractError('conversion-only operation cannot prepare a guide')
            else:
                wire=CodexTextWire(a['text_node'],a['text_script'],a['codex_binary'],worktree)
                guide=PilotGuidePreparation(session,a['guide_plan'],wire=wire)
            def ready():
                source();process.check();runtime.gate()
                executables = ('cli_guard',) if conversion_only else ('cli_guard','text_node','codex_binary')
                if any(not os.access(a[k],os.X_OK) for k in executables):
                    raise ContractError('approved local executables are not ready')
                # Account/remaining quota is checked by the official client before
                # its body turn; readiness does not certify future model quality.
                return True
            controller=PilotBoundedRun(session,intake,a['run_plan'],prepare_candidate=guide,checked_ready=ready)
            if action=='intake':
                value=controller.run_to_preview()
            elif action=='intake-convert':
                value=controller.run_to_conversion()
            elif action=='prepare':
                value=controller.prepared_to_preview()
            elif action in {'revise','continue'}:
                value=controller.revise_to_preview()
            else:
                session.verify_isolation()
                sid='scan_'+stable_key('owner-reply',approval_sha,message_id)
                previous=session.value(sid+':interval')
                end=session.clock()
                start=max(end-timedelta(seconds=lookback_seconds),instant(a['starts_at'])-timedelta(hours=24))
                if previous:
                    start=datetime.fromtimestamp(previous['start'],timezone.utc)
                    end=datetime.fromtimestamp(previous['end'],timezone.utc)
                session.scan(sid,start=start,end=end,max_pages=max_pages)
                value=controller.owner_reply(sid,message_id)
        result['result']=value  # Preserve actual publication if a later notice/window check stops.
        session.check();source()
        result.update(status='COMPLETED_BOUNDED_ACTION')
    except (ContractError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        result['error_type']=type(error).__name__
        if isinstance(controller,(PilotDailyRun,PilotServiceRun)):result['result']=controller.saved_observation()
        if session is not None:session.append(dict(stopped='operator_entry',error_type=type(error).__name__))
    finally:
        result['ended_at']=datetime.now(timezone.utc).isoformat()
        if session is not None:
            try:
                events=session.http_events()
                result['physical_requests']={k:sum(e.get('type')=='intent' and e.get('kind')==k for e in events)
                                             for k in session.limits}
            except (ContractError,OSError,ValueError,TypeError):
                result['physical_requests']='UNVERIFIED_RETAIN_JOURNAL'
            if result['status']=='COMPLETED_BOUNDED_ACTION':
                try:
                    session.check();source()
                except (ContractError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
                    result.update(status='STOPPED',final_validation_error_type=type(error).__name__)
                    session.append(dict(stopped='final_validation',error_type=type(error).__name__))
            try:
                if controller is not None:controller.close()
            finally:session.close()
        write_private(root/(marker.stem+'-result.local.json'),result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True);parser.add_argument('--approval-sha',required=True)
    parser.add_argument('--execute',required=True,choices=('intake','intake-convert','prepare','revise','continue','owner-reply','owner-followup','owner-wait','publication-notice','receive-inbox','inbox-daily','inbox-service'))
    parser.add_argument('--message-id');parser.add_argument('--lookback-seconds',type=int,default=3600)
    parser.add_argument('--max-pages',type=int,default=1)
    args=parser.parse_args()
    try:
        result=execute(args.root,args.approval_sha,args.execute,message_id=args.message_id,
                       lookback_seconds=args.lookback_seconds,max_pages=args.max_pages)
        print(result['status']+'; private evidence retained; no automatic retry')
        return 0 if result['status']=='COMPLETED_BOUNDED_ACTION' else 1
    except (ContractError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        print('STOPPED before execution setup: '+type(error).__name__)
        return 1


if __name__=='__main__':raise SystemExit(main())
