"""Finite, separately approved waiting for one exact preview decision.

Each empty, complete scan consumes its own budget. Errors never cause another
scan. No model, new intake, preview resend, daemon or restart is introduced.
"""
from datetime import timedelta
from pathlib import Path
import time

from .contracts import ContractError, stable_key
from .f05 import file_sha
from .pilot_decision import apply_owner_reply, observed_reply
from .pilot_feishu import read_json
from .pilot_notice_workflow import PilotNoticeWorkflow
from .pilot_owner_followup import checked_previous
from .pilot_preview import PilotPreviews
from .runtime import no_symlinks
from .s6 import instant


def owner_wait_limits(max_scans):
    # Three identity reads, N list reads, one detail and one final notice;
    # every physical business request also needs one counted auth request.
    if type(max_scans) is not int or not 2 <= max_scans <= 45:
        raise ContractError('owner wait requires 2 to 45 explicitly budgeted scans')
    return dict(auth=max_scans+5, identity=3, list=max_scans,
                message=1, download=0, upload=0, send=1)


def owner_wait(session, intake, plan_path, *, wait=time.sleep):
    path = no_symlinks(Path(plan_path).absolute())
    if session.binding().get(str(path)) != file_sha(path):
        raise ContractError('owner wait plan must be frozen')
    plan = read_json(path, 65536)
    fields = {'reference', 'previous_preview', 'lookback_seconds',
              'max_scans', 'poll_interval_seconds'}
    if (set(plan) != fields or plan['reference'] != 'P18-owner-wait-v1'
            or session.approval.get('operation') != 'owner-wait'
            or type(plan['lookback_seconds']) is not int
            or not 1 <= plan['lookback_seconds'] <= 86400
            or type(plan['poll_interval_seconds']) is not int
            or not 30 <= plan['poll_interval_seconds'] <= 3600
            or not callable(wait)):
        raise ContractError('exact finite owner wait plan and explicit approval required')
    if session.limits != owner_wait_limits(plan['max_scans']):
        raise ContractError('owner wait requires its exact category budget')
    binding = plan['previous_preview']
    previews = PilotPreviews(intake.registry, intake.scope)
    pid = binding['publication_id']

    def candidate():
        session.check()
        checked_previous(session, previews, binding)
        with intake.registry.transaction() as db:
            previews._candidate(db, pid)
            previews._current(db, pid)
        row = previews.publisher._row(pid)
        if (row['state'] != 'prepared' or intake.registry.rows(
                'SELECT * FROM pilot_preview_decisions WHERE publication_id=?', (pid,))):
            raise ContractError('owner wait requires an undecided prepared candidate')
        previews.publisher._tree(row, pid+'.candidate')
        entry = intake.registry.entry(row['entry_id'])
        original = intake.runtime.checked(intake.runtime.path(entry['relative_path']))
        if session.binding().get(str(original)) != entry['sha256']:
            raise ContractError('owner wait original must be frozen')
        session.check()

    candidate()
    prefix = 'owner-wait:'+session.approval_sha+':'
    if session.value(prefix+'started') is not None:
        raise ContractError('owner wait already started; inspect evidence without restarting')
    session.bind(prefix+'started', dict(plan_sha256=file_sha(path)))
    expires = instant(session.approval['expires_at'])
    # Keep time for detail verification/publication/notice. This is a local
    # admission margin, not an extension or a promise about network latency.
    margin = timedelta(seconds=60)
    scans = 0

    def pending(reason):
        value = dict(status='awaiting_owner_decision', publication_id=pid,
                     published=False, scans_completed=scans, reason=reason,
                     waiting_active=False, automatically_restarts=False)
        session.bind(prefix+'result', value)
        return value

    if session.clock()+margin >= expires:
        return pending('insufficient_window')
    session.verify_isolation()
    start = max(session.clock()-timedelta(seconds=plan['lookback_seconds']),
                instant(session.approval['starts_at'])-timedelta(hours=24))
    for index in range(plan['max_scans']):
        candidate()
        if session.clock()+margin >= expires:
            return pending('insufficient_window')
        sid = 'scan_'+stable_key('owner-wait', session.approval_sha, index)
        result = session.scan(sid, start=start, end=session.clock(), max_pages=1)
        scans += 1
        if result['status'] != 'COMPLETE_INTERVAL':
            raise ContractError('complete bounded reply interval required; waiting stopped')
        choices = []
        for receipt in result['receipts']:
            for item in read_json(Path(receipt))['data']['items']:
                if (item['message_id'] not in result['message_ids']
                        or item.get('parent_id') != binding['message_id']):
                    continue
                command = observed_reply(item, session.approval['bot_id'])[1]
                if command in previews.COMMANDS:
                    choices.append(item)
        if len(choices) > 1:
            raise ContractError('one unambiguous supported direct owner reply required')
        if choices:
            value = apply_owner_reply(session, previews, sid, choices[0]['message_id'],
                                      prior_delivery=binding, observed=choices[0])
            if value['status'] == 'published':
                value['notification'] = PilotNoticeWorkflow(session, intake).publication(pid)
            session.bind(prefix+'result', value)
            return value
        if index+1 == plan['max_scans']:
            return pending('scan_limit_reached')
        target = session.clock()+timedelta(seconds=plan['poll_interval_seconds'])
        if target+margin >= expires:
            return pending('insufficient_window')
        remaining = plan['poll_interval_seconds']
        while remaining:
            session.check()
            seconds = min(30, remaining)
            wait(seconds)
            remaining -= seconds
            session.check()
        if session.clock() < target:
            raise ContractError('poll wait returned before its scheduled time')
    raise AssertionError('validated scan bound must return a terminal result')
