"""Explicit mention intake. Events are hints; native read receipts admit files.

The listener only persists small IDs. The main scheduler performs metered reads,
then shares the ordinary file queue and preview approval; no second AI worker.
"""
import hashlib
import json
from pathlib import Path
from .document_formats import daily_formats, unsupported_message
import re

from .contracts import ContractError, stable_key
from .daily_cards import bounded_json, _identity
from .daily_card_io import CardDelivery
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .daily_transport import durable_file
from .f01 import PREFIX
from .f05 import file_sha
from .registry import encoded

PROFILE = dict(reference='P46-archive-mention-v1', enabled=True)
SCHEMA = """CREATE TABLE IF NOT EXISTS daily_archive_inbox(
 message_id TEXT PRIMARY KEY, event_id TEXT, proof TEXT NOT NULL,
 state TEXT NOT NULL, available REAL NOT NULL, created REAL NOT NULL,
 result TEXT, card_id TEXT);
 CREATE UNIQUE INDEX IF NOT EXISTS daily_archive_event_id ON daily_archive_inbox(event_id);
"""


def archive_command(item, policy):
    """Only a platform-resolved mention, alone or followed by 归档, is intent."""
    # Feishu can mark a newly sent mention as updated. Authorize the current
    # physically read command and exact owner/mention, not that metadata flag.
    # This is only a receipt request; the original file and publication gates
    # still validate their own immutable inputs and separate owner decision.
    sender = item.get('sender', {})
    if (item.get('chat_id') != policy.chat_id or item.get('msg_type') != 'text'
            or item.get('deleted') is not False or type(item.get('updated')) is not bool
            or (sender.get('id'), sender.get('id_type'), sender.get('sender_type'))
            != (policy.account_id, 'open_id', 'user')):
        return False
    mentions = item.get('mentions')
    if not isinstance(mentions, list) or len(mentions) != 1 or not isinstance(mentions[0], dict):
        return False
    m = mentions[0]
    if (m.get('id') != policy.bot_id or m.get('id_type') != 'open_id'
            or not isinstance(m.get('key'), str) or not re.fullmatch(r'@_user_[0-9]+', m['key'])):
        return False
    try:
        body = bounded_json(item.get('body', {}).get('content'), 4096)
    except ContractError:
        return False
    if not isinstance(body, dict) or set(body) != {'text'} or not isinstance(body['text'], str):
        return False
    text = body['text'].strip()
    key = m['key']
    return text == key or (text.startswith(key) and text[len(key):len(key)+1].isspace()
                           and text[len(key):].strip() == '归档')


class ArchiveMentions:
    def __init__(self, backend):
        self.b, self.store = backend, backend.store
        with self.store.connection() as db:
            db.executescript(SCHEMA)
        self.delivery = CardDelivery(backend.transport, backend.card_flow.cards)

    def persist(self, event, *, app_id, session_path):
        b, p = self.b, self.b.authority.policy
        b.check_authorization()
        if (b.authority.config.get('archive_mentions') != PROFILE or app_id != p.app_id
                or set(event) != {'type','event_id','timestamp','operator_id','chat_id','message_id'}
                or event['type'] != 'im.message.receive_v1' or event['operator_id'] != p.account_id
                or event['chat_id'] != p.chat_id):
            raise ContractError('archive event scope differs')
        eid, mid = _identity(event['event_id'], ''), _identity(event['message_id'], 'om_')
        if not isinstance(event['timestamp'], str) or not re.fullmatch(r'[0-9]{1,20}', event['timestamp']):
            raise ContractError('archive event timestamp required')
        path = session_path.parent/(stable_key('archive-event', eid)+'.archive-event.local.json')
        value = dict(reference=PROFILE['reference'], app_id=app_id, event=event, listener_session=str(session_path))
        durable_file(path, value)
        proof = dict(evidence={str(path):file_sha(path), str(session_path):file_sha(session_path)})
        with self.store.transaction() as db:
            prior = db.execute('SELECT message_id FROM daily_archive_inbox WHERE event_id=?', (eid,)).fetchone()
            if prior and prior['message_id'] != mid:
                raise ContractError('archive event ID reused for another message')
            db.execute("INSERT OR IGNORE INTO daily_archive_inbox VALUES (?,?,?,'pending',0,?,NULL,NULL)",
                       (mid, eid, encoded(proof), self.store.now()))

    def collect(self):
        # History recovery shares the same message key with live events.
        cursor = self.store.get('archive-history-row') or 0
        for row in self.store.rows('SELECT rowid AS position,* FROM daily_messages WHERE rowid>? ORDER BY rowid LIMIT 50', (cursor,)):
            item = json.loads(row['payload'])
            if archive_command(item, self.b.authority.policy):
                proof = self.store.get('observation:'+row['version'])
                if not proof:
                    raise ContractError('archive history requires physical evidence')
                self.b.transport._checked_result(proof)
                with self.store.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO daily_archive_inbox VALUES (?,NULL,?,'pending',0,?,NULL,NULL)",
                               (row['id'], encoded(proof), self.store.now()))
            self.store.put('archive-history-row', row['position'])

    def _read(self, mid, origin, operation):
        self.b.transport._checked_result(origin)
        result = self.b.transport.request(operation, 'message', PREFIX+'/im/v1/messages/'+mid,
                                          evidence=origin['evidence'], confirmation=True)
        items = result['value'].get('data', {}).get('items')
        if not isinstance(items, list) or len(items) != 1 or items[0].get('message_id') != mid:
            raise MaterialProblem('无法读取对应消息，请回复仍可打开的附件后重新 @机器人。')
        # Retain the chain (event or history -> command -> direct parent).
        return items[0], dict(result, evidence=dict(origin['evidence'], **result['evidence']))

    def _observe(self, item, proof, *, register=False):
        digest = hashlib.sha256(encoded(item).encode()).hexdigest()
        self.store.put('observation:'+digest, proof)
        with self.store.transaction() as db:
            self.store._observe(db, item, register=register)

    def _prepare(self, row):
        mid, p = row['message_id'], self.b.authority.policy
        item, proof = self._read(mid, json.loads(row['proof']), 'archive-command:'+mid)
        if not archive_command(item, p):
            with self.store.transaction() as db:
                db.execute("UPDATE daily_archive_inbox SET state='ignored',result='command_changed' WHERE message_id=?", (mid,))
            return
        self._observe(item, proof)
        parent = item.get('parent_id')
        title = '请选择要归档的附件'
        text = '请直接回复那条附件消息，再选择 @机器人，发送“归档”或只发送该 @。不会按最近一篇猜测附件。'
        if isinstance(parent, str) and re.fullmatch(r'om_[A-Za-z0-9_-]{1,160}', parent):
            try:
                target, target_proof = self._read(parent, proof, 'archive-target:'+mid)
                sender = target.get('sender', {})
                if (target.get('chat_id') != p.chat_id or target.get('msg_type') != 'file'
                        or target.get('deleted') is not False or target.get('updated') is not False
                        or (sender.get('id'), sender.get('id_type'), sender.get('sender_type'))
                        != (p.account_id, 'open_id', 'user')):
                    raise MaterialProblem('请回复你在本群发送的原始附件；当前引用的消息不是可归档文件。')
                metadata = bounded_json(target.get('body', {}).get('content'), 4096)
                name = metadata.get('file_name') if isinstance(metadata, dict) else None
                supported = set(daily_formats(self.b.authority.config))
                if not isinstance(name, str) or not name or len(name.encode()) > 900 or Path(name).suffix.lower() not in supported:
                    raise MaterialProblem(unsupported_message(self.b.authority.config))
                self._observe(target, target_proof, register=True)
                document = self.store.document(parent)
                phase = document['phase']
                title = name
                if phase in ('notified', 'published', 'reading', 'reading_wait'):
                    text = '这篇资料已入库，沿用现有记录；后续整理如未完成会继续。'
                elif phase == 'duplicate':
                    text = '已有相同原件的处理记录，本次不会重复生成导读。'
                elif phase == 'needs_upload':
                    text = '这篇资料需要补传原件。请发送新的完整附件，再直接回复新附件并 @；旧记录不会重新开始。'
                elif phase == 'confirmed':
                    text = '你已确认这篇导读入库，正在等待完成写入与后续整理，无需重复确认。'
                elif phase in ('model_unknown', 'send_unknown'):
                    text = '已有处理记录，上次生成或发送的结果尚待核查；此次 @ 不会重新生成导读。'
                elif phase in ('held', 'rejected'):
                    text = '这篇资料已暂停或暂不入库；本次 @ 不会重新生成，请由 Codex 核查原记录。'
                elif phase in ('awaiting_confirmation', 'awaiting_images'):
                    text = '正在等待你的选择，请查看此前的导读或缺图处理卡片；本次 @ 不会代替确认。'
                else:
                    text = '已收到归档请求，正在进入处理队列。已有任务时会依次处理；内存、资料盘或额度不足时暂停并保留记录。导读完成后仍需你确认入库。'
                self.store.put('archive-request:'+parent, dict(command_id=mid, phase=phase, at=self.store.now()))
            except MaterialProblem as error:
                text = str(error)
            except Deferred as error:
                if error.reason != 'queue_full':
                    raise
                # Keep the request pending while reporting backpressure once.
                self.b.notice('archive-full:'+mid, '归档队列已满，已保留这次 @ 请求；有空位后继续核对附件。', confirmation=True)
                raise
        binding = dict(purpose='archive_status', document_id=mid, target_message_id=mid,
                       source_sha256=hashlib.sha256(encoded(item).encode()).hexdigest(), details=text)
        card = self.b.card_flow.cards.prepare(binding, title)
        # Persist the exact answer before sending; retries cannot change content.
        with self.store.transaction() as db:
            db.execute("UPDATE daily_archive_inbox SET state='responding',proof=?,card_id=?,result=? WHERE message_id=?",
                       (encoded(proof), card['card_id'], text, mid))

    def drain(self):
        self.b.check_authorization()
        for row in self.store.rows("SELECT * FROM daily_archive_inbox WHERE state IN ('pending','responding') AND available<=? ORDER BY created,message_id LIMIT 4", (self.store.now(),)):
            if self.b.stop.is_set():
                return
            try:
                if row['state'] == 'pending':
                    self._prepare(row)
                fresh = self.store.rows('SELECT * FROM daily_archive_inbox WHERE message_id=?', (row['message_id'],))[0]
                if fresh['state'] == 'responding':
                    card = self.b.card_flow.cards.get(fresh['card_id'])
                    if card['state'] == 'prepared':
                        self.delivery.send(card, json.loads(fresh['proof']))
                    with self.store.transaction() as db:
                        db.execute("UPDATE daily_archive_inbox SET state='processed' WHERE message_id=?", (row['message_id'],))
            except Deferred as error:
                with self.store.transaction() as db:
                    db.execute('UPDATE daily_archive_inbox SET available=?,result=? WHERE message_id=?',
                               (error.until or self.store.now()+self.store.policy.poll_seconds, error.reason, row['message_id']))
            except MaterialProblem as error:
                # An unreadable command or unresolved send is isolated, never
                # silently retried as a new archive/model operation.
                with self.store.transaction() as db:
                    db.execute("UPDATE daily_archive_inbox SET state='held',result=? WHERE message_id=?", (str(error), row['message_id']))
                self.store.put('archive-mention-problem', dict(message_id=row['message_id'], reason=str(error), at=self.store.now()))
