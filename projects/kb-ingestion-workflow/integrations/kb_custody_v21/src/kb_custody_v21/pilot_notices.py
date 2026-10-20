"""Bounded P11 notices through the existing official CLI and physical journal.

Code availability is not activation: a new exact approval/window is required.
No fallback sender, credential lookup, remote reconciliation or retry is added.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from .contracts import ContractError
from .f01 import PREFIX, encoded, validated_message_content
from .f05 import file_sha
from .notifications import DeliveryUnknown
from .pilot_feishu import PilotReadSession, LIMITS
from .runtime import no_symlinks

PURPOSES = ['service', 'custody', 'collection', 'publication']
ITEM_FIELDS = ('notification_id', 'provider_request_key', 'target_chat_id', 'payload')


class PilotExecutionSession(PilotReadSession):
    reference, root_name = 'P11', 'p11-requests'

    def approved_limits(self, approval):
        limits = approval.get('limits')
        if (not isinstance(limits, dict) or set(limits) != set(LIMITS)
                or any(type(v) is not int or v < 0 for v in limits.values())
                or not 0 < sum(limits.values()) <= 100
                or type(approval.get('maximum_total')) is not int
                or approval.get('maximum_total') != sum(limits.values())
                or approval.get('notice_purposes') != PURPOSES):
            raise ContractError('new exact P11 category budget and notice purposes required')
        return dict(limits)


def notice_item(item):
    value = {k: item.get(k) for k in ITEM_FIELDS}
    if (not isinstance(value['notification_id'], str)
            or not re.fullmatch('(?:[a-f0-9]{64}|notification_[a-f0-9]{32})', value['notification_id'])
            or not isinstance(value['provider_request_key'], str)
            or not re.fullmatch('[A-Za-z0-9_-]{1,50}', value['provider_request_key'])
            or not isinstance(value['payload'], str) or not value['payload'].strip()
            or len(value['payload'].encode()) > 6000):
        raise ContractError('one exact bounded notice identity and payload required')
    return value


class PilotNoticeSender:
    """Adapt a checked persisted service/outbox/report item to Sender.

    checked_item must re-read the authoritative plan and its current source
    conditions. It is called before credentials/HTTP and again before success.
    The caller retains its existing lifecycle/outbox transaction and locks.
    """
    def __init__(self, session, purpose, checked_item):
        if not isinstance(session, PilotExecutionSession) or purpose not in PURPOSES or not callable(checked_item):
            raise ContractError('approved P11 and authoritative notice checker required')
        self.session, self.purpose, self.checked_item = session, purpose, checked_item

    def lookup(self, item, attempt):
        # A lookup budget/contract has not been granted by this sender. Preserve
        # the existing outbox unknown; do not consume reads or enable resending.
        return 'unknown', None

    def send(self, item, attempt):
        s = self.session
        try:
            s.check()
            wanted = notice_item(item)
            if s.value('isolation_verified') is not True or wanted['target_chat_id'] != s.approval['chat_id']:
                raise ContractError('fixed group isolation required')

            def check():
                s.check()
                if notice_item(self.checked_item()) != wanted:
                    raise ContractError('authoritative notice or source state changed')
                s.check()

            check()
            plan = dict(reference='P11', purpose=self.purpose, app_id=s.approval['app_id'],
                        account_id=s.approval['account_id'], **wanted)
            raw = encoded(plan).encode()
            path = s.root/(wanted['notification_id']+'.notice.local.json')
            if path.exists():
                if no_symlinks(path).read_bytes() != raw:
                    raise ContractError('notice identity cannot change its content')
            else:
                with path.open('xb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                fd = os.open(s.root, os.O_RDONLY)
                try: os.fsync(fd)
                finally: os.close(fd)
            digest = file_sha(path)
            body = encoded(dict(receive_id=wanted['target_chat_id'], msg_type='text',
                content=encoded(dict(text=wanted['payload'])), uuid=wanted['provider_request_key']))
            if len(body.encode()) > 8192:
                raise ContractError('notice exceeds physical transport body limit')
            check()
            value, key = s._call('send', PREFIX+'/im/v1/messages?receive_id_type=chat_id',
                operation='notice:'+wanted['notification_id'], mode='send', body=body,
                notice=dict(notification_id=wanted['notification_id'], plan_path=str(path)),
                frozen_extra={str(path):digest})
            receipt = value.get('data')
            validated_message_content(receipt, kind='text', name='Pilot notice', text=wanted['payload'],
                app_id=s.approval['app_id'], bot_id=s.approval['bot_id'], chat_id=s.approval['chat_id'])
            if file_sha(path) != digest:
                raise ContractError('notice plan changed before success')
            observation = dict(message_id=receipt['message_id'], response_file=key+'.response.local',
                               response_sha256=file_sha(s.root/(key+'.response.local')))
            check()  # Hashing saved output and reading source state may cross the window.
            s.bind('notice:'+wanted['notification_id']+':verified', observation)
            return receipt['message_id']
        except (OSError, ContractError, ValueError, KeyError, TypeError) as error:
            # No diagnosis here can prove an attempted write was not received.
            # Stop this window; keep its durable intent and the outbox unknown.
            s.append(dict(stopped='notice', error_type=type(error).__name__))
            raise DeliveryUnknown('Pilot notice stopped; preserve evidence; no retry') from error


def collection_text(report):
    """One bounded, visible checklist. Never silently truncate or claim completeness."""
    interval = report['interval']
    rows = report['materials']
    def time_label(value):
        return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    text = [f"材料接管核对清单（北京时间 {time_label(interval['start'])} 至 {time_label(interval['end'])}）", report['notice']]
    labels = dict(ACCEPTED='原件已保存', NOT_CONFIRMED='尚未确认接管',
                  INTEGRITY_HOLD='原件完整性待核查', NEEDS_RESUBMISSION='需要补投')
    for i, row in enumerate(rows, 1):
        # Quote filenames as data; a filename containing line breaks cannot
        # masquerade as another status line. Include send time for same-name files.
        name = encoded(row['filename']) if row['filename'] else '文件名未知'
        if row['status'] not in labels:
            raise ContractError('unsupported collection status')
        text.append(f"{i}. {name}（投递时间：{row.get('sent_at') or '待核对'}）"
                    f"：{labels[row['status']]}；{row['action']}。")
    if not rows:
        text.append('本次查询未观察到文件；这不证明停机期间没有投递。')
    if not report['query_complete']:
        text.append('未核对时段：上述整个查询区间。请对照飞书补投未确认的材料。')
    text.append('本清单仅对应上述查询时段。收到某份材料的接管确认后，才能确认该份原件已保存。')
    result = '\n'.join(text)
    if len(result.encode()) > 6000:
        raise ContractError('checklist exceeds one bounded message; keep local report and plan a metered split')
    return result
