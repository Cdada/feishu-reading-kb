"""Feishu owner messages and fixed cards over the existing metered transport."""
import hashlib
import json
import re
from pathlib import Path

from .contracts import ContractError, stable_key
from .daily_guide import DailyTextWire
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .f05 import file_sha
from .qa_engine import QAEngine
from .qa_index import QAIndex
from .qa_model import QAModel
from .qa_query import HELP, render_search
from .qa_sources import QASources
from .qa_store import QAStore
from .registry import encoded


class QAFlow:
    def __init__(self, backend, *, wire=None):
        self.backend, self.daily = backend, backend.store
        self.profile = backend.authority.qa
        self.store = QAStore(self.daily)
        self.sources = QASources(backend.authority, self.daily)
        self.index = QAIndex(backend.authority.root/'qa-index/index.sqlite3')
        c = backend.authority.config
        wire = wire if wire is not None else DailyTextWire(c['text_node'], self.profile.script,
            c['codex_binary'], backend.authority.runtime.worktree, stop=backend.stop)
        self.model = QAModel(backend.authority, self.profile, self.store, self.sources, wire)
        self.engine = QAEngine(self.store, self.sources, self.index, self.model)

    def collect(self):
        # The collector already limits observations to the configured owner and
        # isolated chat. Re-fetch every question before admitting its text.
        cursor = self.daily.get('qa-observation-cursor') or 0
        rows = self.daily.rows('SELECT rowid AS sequence,payload FROM daily_messages WHERE rowid>? ORDER BY rowid LIMIT 100', (cursor,))
        for row in rows:
            observed = json.loads(row['payload'])
            if observed.get('msg_type')=='text' and observed.get('deleted') is False:
                text = json.loads(observed.get('body', {}).get('content', '{}')).get('text', '')
                # Only exact prefixes initiate QA; ordinary archive confirmations
                # remain under their existing flow even when a question is open.
                candidate = bool(re.match(r'^\s*问库\s*[:：]', text)) or text.strip()=='问库'
                created = int(observed.get('create_time', '0'))/1000
                key = stable_key('qa-message', observed['message_id'])
                if candidate and created >= self.profile.since and self.store.get(key) is None:
                    if len(text.strip()) > 2000:
                        self.backend.notice('qa-too-long:'+key, '问题超过 2000 字，请缩短后重新发送。', purpose='service')
                        self.daily.put('qa-observation-cursor', row['sequence'])
                        continue
                    item, detail, listed = self.backend.collector.message(observed['message_id'])
                    if item.get('deleted') is not False or item.get('body') != observed.get('body'):
                        raise Deferred('qa_message_changed_refresh_required')
                    proof = dict(receipts=dict(detail['evidence'], **listed['evidence']))
                    conversation = item.get('thread_id') or 'chat'
                    self.store.seed_thread(conversation, item.get('parent_id'))
                    self.store.enqueue(key, observed['message_id'], text.strip(), proof,
                                       conversation=conversation)
            self.daily.put('qa-observation-cursor', row['sequence'])

    def pending(self):
        return self.store.pending() is not None

    def step(self):
        b = self.backend
        b.check_authorization(); b._storage_ready()
        if b.stop.is_set():
            raise Deferred('stopping')
        if b.pipeline.resources.check()['state'] != 'resources_ready':
            raise Deferred('paused_resources')
        job = self.store.pending()
        if job is None:
            return
        if any(file_sha(Path(p)) != h for p,h in job['proof']['receipts'].items()):
            raise ContractError('问答消息凭据发生变化。')
        try:
            self.engine.run(job)
        except (ContractError, FileNotFoundError) as error:
            # Bad questions or a missing/changed source are isolated from intake.
            # Keep diagnostics local; never leak filesystem paths into Feishu.
            self.daily.put('qa-error:'+job['id'], dict(type=type(error).__name__, detail=str(error)))
            self.store.complete(job['id'], dict(status='blocked', text='本次问题或资料校验未通过，未生成回答。请检查提问格式；若资料刚被修改，请联系 Codex 核查。\n\n'+HELP, hits=[]))

    def current(self, card):
        job = self.store.get(card['binding']['job_id'])
        if not job or job['state']!='sent' or not job['delivery']:
            raise ContractError('问答卡片没有对应的已发送结果。')
        return card['binding'], job['delivery']['receipt']

    def report(self):
        b = self.backend
        pending = self.store.pending()
        if pending is not None and not self.daily.get('qa-ack:'+pending['id']):
            b.notice('qa-ack:'+pending['id'], '已收到知识库问题，将在当前资料任务完成后查询；归档确认仍可继续使用。', purpose='service')
            self.daily.put('qa-ack:'+pending['id'], True)
        rows = self.daily.rows("SELECT id FROM qa_jobs WHERE state='ready' ORDER BY created LIMIT 1")
        for row in rows:
            job = self.store.get(row['id']); result = job['result']
            saved = self.daily.get('qa-delivery-text:'+job['id'])
            try:
                self.sources.verify(result['hits'])
            except (ContractError, FileNotFoundError):
                text = '检索后资料发生变化，本次回答已停止发送。请重新提问。'
                if saved is not None:
                    notice = b.notice('qa-invalidated:'+job['id'], text, purpose='service')
                    self.store.delivered(job['id'], notice)
                    continue
            else:
                text = result['text']
            if saved is None:
                self.daily.put('qa-delivery-text:'+job['id'], text)
            else:
                text = saved
            # The notification is durable and UUID-bound. A crash reuses its
            # physical receipt before a card is attached to that bot message.
            parts = [text[i:i+1200] for i in range(0, len(text), 1200)]
            for i, part in enumerate(parts):
                label = f'（{i+1}/{len(parts)}）\n' if len(parts)>1 else ''
                notice = b.notice('qa-answer:'+job['id']+':'+str(i), label+part, purpose='service')
            self.store.delivered(job['id'], notice)
        if b.card_listener is None or not b.card_listener.ready:
            return
        rows = self.daily.rows("SELECT id FROM qa_jobs WHERE state='sent' ORDER BY created DESC LIMIT 20")
        for row in rows:
            job = self.store.get(row['id'])
            binding = dict(purpose='qa', document_id=job['message_id'], source_sha256=hashlib.sha256(job['text'].encode()).hexdigest(),
                target_message_id=job['delivery']['message_id'], job_id=job['id'],
                details='可以继续发送“问库：追问 …”。以下选项仅作用于这次问答，不会把回答写入知识库。')
            card = b.card_flow.cards.prepare(binding, '继续查询测试知识库')
            if card['state']=='prepared':
                b.card_flow.delivery.send(card, job['delivery']['receipt'])
                return  # Bound platform work per collection tick.
            choice = b.card_flow.cards.choice(card['card_id'])
            if choice and choice['result'] is None:
                command = choice['command']
                key = stable_key('qa-card-choice', card['card_id'])
                if command=='查看原文依据':
                    created = self.store.enqueue(key, job['message_id'], '帮助', dict(receipts=choice['evidence']['receipts']), conversation=job['conversation'])
                    if created['state']=='queued':
                        self.sources.verify(job['result']['hits'])
                        self.store.complete(key, dict(status='retrieval', text=render_search(job['result']['hits']), hits=job['result']['hits']))
                else:
                    self.store.enqueue(key, job['message_id'], command, dict(receipts=choice['evidence']['receipts']), conversation=job['conversation'])
                b.card_flow.cards.finish(card['card_id'], dict(status='accepted'), checked_access=b.check_authorization)
            if choice and not self.daily.get('qa-card-displayed:'+card['card_id']):
                b.card_flow.delivery.update(b.card_flow.cards.get(card['card_id']), '已收到，请查看新的问答结果。')
                self.daily.put('qa-card-displayed:'+card['card_id'], True)
                return
