"""Pilot service notices and observed-file reports, without a background service.

The live composition must establish readiness/stop admission and supply the
authorized Sender. This module neither detects power loss nor performs scans.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
from types import MappingProxyType

from .contracts import ContractError, stable_key
from .f05 import file_sha
from .locking import open_lock
from .notifications import Sender, DeliveryUnknown, DefiniteSendError, RetryableSendError
from .pilot_preview import _identity


TEXT = {
    'pausing': '知识库接管服务即将暂停。此期间投递的资料尚不能确认已保存；恢复后会尝试补收并提供核对清单。请暂时保留原文件。',
    'ready': '知识库接管服务已恢复。停机期间的资料会尝试补收并提供核对清单；收到某份资料的接管确认后，才能确认该份原件已保存。',
}


def check_scope(intake, approval):
    s = intake.scope
    if (s.app_id, s.chat_id, s.user_id) != (approval['app_id'], approval['chat_id'], approval['account_id']):
        raise ContractError('report scope differs from the fixed application, group and person')


class PilotServiceNotices:
    """Use the existing event journal and Sender; never auto-retry or lookup.

    These are service-level facts, not asset acceptance notifications. They must
    not invent an intake or enter the asset-only success/failure outbox.
    """
    def __init__(self, intake):
        self.intake, self.registry = intake, intake.registry
        self.scope = intake.scope
        self.aggregate = stable_key('pilot-service', self.scope.app_id, self.scope.chat_id, self.scope.user_id)

    @contextmanager
    def _serial(self):
        runtime = self.registry.runtime
        runtime.gate()
        fd = open_lock(runtime.checked(runtime.root/'conversion-work/pilot-service-notices.lock'))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _events(self):
        return [json.loads(r['payload_json']) for r in self.registry.rows(
            "SELECT payload_json FROM custody_events WHERE event_type='pilot_service_notice' AND aggregate_id=? ORDER BY rowid",
            (self.aggregate,))]

    def _append(self, key, payload):
        with self.registry.transaction() as db:
            self.registry.event(db, 'pilot_service_notice', self.aggregate, key, payload)

    def prepare(self, run_id, phase, *, checked_state):
        """Caller checks actual readiness, or that new intake has been paused.

        A run ID is persisted by the lifecycle controller, not regenerated on
        retry. Repeated preparation returns the same immutable request identity.
        """
        _identity(run_id)
        if phase not in TEXT or not callable(checked_state):
            raise ContractError('explicit service phase and state checker required')
        nid = stable_key(self.aggregate, run_id, phase)
        with self._serial():
            events = self._events()
            existing = [e['plan'] for e in events if e.get('plan', {}).get('notification_id') == nid]
            if existing:
                return existing[0]
            phases = [e['plan']['phase'] for e in events if e.get('plan', {}).get('run_id') == run_id]
            if phase == 'ready' and 'pausing' in phases:
                raise ContractError('a stopped run cannot announce readiness again')
            if checked_state(phase) is not True:
                raise ContractError('service state not verified')
            plan = dict(notification_id=nid, run_id=run_id, phase=phase, payload=TEXT[phase],
                provider_request_key='pilot-svc-'+nid[:40], target_chat_id=self.scope.chat_id)
            self._append(nid+':plan', dict(plan=plan))
            return plan

    def dispatch(self, notification_id, sender, *, checked_access, checked_state):
        """Exactly one attempted send per prepared notice, including crashes.

        checked_access binds the live window/budget/source/scope; checked_state
        rechecks actual service state immediately before the send. No default
        real sender exists. The original N10 executor does not call this path.
        """
        if not isinstance(sender, Sender) or not callable(checked_access) or not callable(checked_state):
            raise ContractError('explicit sender, authorized access and readiness checks required')
        with self._serial():
            events = self._events()
            plans = [e['plan'] for e in events if 'plan' in e]
            matches = [p for p in plans if p['notification_id'] == notification_id]
            if len(matches) != 1:
                raise ContractError('one frozen service notice required')
            item = matches[0]
            attempts = [e for e in events if e.get('notice') == notification_id]
            if attempts:
                # Intent alone includes process death at either side of send.
                return dict(attempts[-1], delivery_state=attempts[-1].get('delivery_state', 'delivery_unknown'))
            if plans[-1] != item:
                raise ContractError('superseded service notice must not be sent')
            checked_access()
            if checked_state(item['phase']) is not True:
                raise ContractError('service state changed before sending')
            checked_access()  # A readiness check can itself cross the window.
            attempt = notification_id+':send'
            self._append(attempt+':intent', dict(notice=notification_id, attempt=attempt))
            try:
                mid = sender.send(MappingProxyType(item), attempt)
                if not isinstance(mid, str) or not mid:
                    raise DeliveryUnknown('missing service notice receipt')
                checked_access()
                result = dict(notice=notification_id, delivery_state='delivered', provider_message_id=mid)
            except (DefiniteSendError, RetryableSendError):
                result = dict(notice=notification_id, delivery_state='failed_no_retry')
            except (OSError, ContractError):
                result = dict(notice=notification_id, delivery_state='delivery_unknown')
            self._append(attempt+':result', result)
            return result


def collection_report(session, intake, scan_id):
    """Join an already saved bounded scan with custody; no network or writes.

    Reports are snapshots, not proof of ongoing availability. The entire scan
    interval remains uncertain when paging stops; do not infer a missing suffix.
    """
    session.binding()
    check_scope(intake, session.approval)
    scan = session.value(scan_id+':result')
    if not scan or scan['status'] not in {'COMPLETE_INTERVAL', 'INCOMPLETE_PAGE_LIMIT'}:
        raise ContractError('completed bounded scan observation required')
    for path, digest in scan['receipts'].items():
        if file_sha(Path(path)) != digest:
            raise ContractError('scan receipt changed')
    observed = {}
    for key in scan['pages']:
        for item in session._receipt(key, 'list')['data']['items']:
            mid = item['message_id']
            if mid not in scan['message_ids']:
                continue
            if (item.get('chat_id') != session.approval['chat_id'] or any(item.get('sender', {}).get(k) != v
                    for k, v in dict(id=session.approval['account_id'], id_type='open_id', sender_type='user').items())):
                raise ContractError('observed message identity differs')
            if mid in observed and observed[mid] != item:
                raise ContractError('conflicting observations for the same message')
            observed[mid] = item
    if set(observed) != set(scan['message_ids']):
        raise ContractError('scan selection differs from verified receipts')
    rows = []
    for mid, item in observed.items():
        if item.get('msg_type') != 'file':
            continue
        try:
            content = json.loads(item.get('body', {}).get('content', ''))
        except (ValueError, TypeError):
            content = {}
        if not isinstance(content, dict):
            content = {}
        name, key = content.get('file_name'), content.get('file_key')
        name = name if isinstance(name, str) and name else None
        row = dict(message_id=mid, filename=name, sent_at=item.get('create_time'),
                   status='NOT_CONFIRMED', action='核对原消息及本地接管记录，必要时补投', entry_id=None)
        message_key = stable_key('pilot-message', intake.scope.app_id, intake.scope.chat_id, mid)
        jobs = intake.registry.rows('SELECT entry_id FROM pilot_conversion_jobs WHERE message_key=?', (message_key,))
        if jobs:
            if len(jobs) != 1:
                raise ContractError('file report requires one exact registered attachment')
            eid = jobs[0]['entry_id']
            entry = intake.registry.entry(eid)
            lineage = json.loads(entry['lineage_json'])
            if key != lineage['file_key'] or name != entry['received_filename']:
                raise ContractError('observed file differs from registered attachment')
            status = intake.status(eid)
            row.update(entry_id=eid, processing_state=status['processing_state'])
            if status['original_status'] == 'accepted':
                row.update(status='ACCEPTED', action='原件已接管，无需重发')
                if status['custody_health'] != 'healthy':
                    row.update(status='INTEGRITY_HOLD', action='已接管原件完整性待核查，请保留源文件')
            elif status['original_status'] == 'failed':
                row.update(status='NEEDS_RESUBMISSION', action='原件接管失败；核对失败原因后补投')
        elif item.get('deleted') is True:
            row.update(status='NEEDS_RESUBMISSION', action='原消息已删除且没有本地接管记录，请补投原文件')
        rows.append(row)
    session.binding()
    for path, digest in scan['receipts'].items():
        if file_sha(Path(path)) != digest:
            raise ContractError('scan receipt changed while building the report')
    complete = scan['status'] == 'COMPLETE_INTERVAL'
    return dict(interval=scan['interval'], query_complete=complete, materials=rows,
        all_observed_files_accepted=bool(rows) and all(r['status']=='ACCEPTED' for r in rows),
        unchecked_interval=None if complete else scan['interval'],
        permanent_backfill_verified=False,
        notice=('本次查询已结束，请逐项核对材料状态。' if complete else
                '清单可能不完整。请对照飞书记录核对整个查询时段，补投未确认接管的材料；系统无法列出完全未观察到的消息。'))
