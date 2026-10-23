"""Durable questions, bounded chat context and the existing cumulative quota."""
import json

from .contracts import ContractError, stable_key
from .daily_store import Deferred
from .registry import encoded

SCHEMA = '''
CREATE TABLE IF NOT EXISTS qa_jobs(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,
 conversation TEXT NOT NULL,text TEXT NOT NULL,proof TEXT NOT NULL,state TEXT NOT NULL,
 plan TEXT,result TEXT,delivery TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS qa_context(conversation TEXT PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS qa_calls(job_id TEXT PRIMARY KEY,model_key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL,state TEXT NOT NULL,result TEXT);
'''


class QAStore:
    def __init__(self, daily):
        self.daily = daily
        with daily.connection() as db:
            db.executescript(SCHEMA)

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        for key in ('proof', 'plan', 'result', 'delivery'):
            if key in value:
                value[key] = json.loads(value[key]) if value[key] is not None else None
        return value

    def get(self, key):
        rows = self.daily.rows('SELECT * FROM qa_jobs WHERE id=?', (key,))
        return self.decode(rows[0]) if rows else None

    def enqueue(self, key, message_id, text, proof, *, conversation='chat'):
        if not isinstance(text, str) or not 0 < len(text) <= 2000 or not proof:
            raise ContractError('需要有来源凭据的短问句。')
        with self.daily.transaction() as db:
            old = db.execute('SELECT * FROM qa_jobs WHERE id=?', (key,)).fetchone()
            if old:
                if old['text'] != text or old['message_id'] != message_id or old['conversation'] != conversation:
                    raise ContractError('已接收的问题不接受静默改写，请发送新问题。')
                return self.decode(old)
            if db.execute("SELECT count(*) FROM qa_jobs WHERE state IN ('queued','running','ready')").fetchone()[0] >= 20:
                raise Deferred('qa_queue_full')
            db.execute("INSERT INTO qa_jobs VALUES (?,?,?,?,?,'queued',NULL,NULL,NULL,?)",
                       (key, message_id, conversation, text, encoded(proof), self.daily.now()))
            self.daily._event(db, 'qa_received', dict(job_id=key))
        return self.get(key)

    def pending(self):
        rows = self.daily.rows("SELECT id FROM qa_jobs WHERE state IN ('queued','running') ORDER BY created,rowid LIMIT 1")
        return self.get(rows[0]['id']) if rows else None

    def context(self, conversation):
        rows = self.daily.rows('SELECT * FROM qa_context WHERE conversation=?', (conversation,))
        return json.loads(rows[0]['value']) if rows and self.daily.now()-rows[0]['updated'] <= 6*3600 else {}

    def prepare(self, key, plan):
        with self.daily.transaction() as db:
            old = self.get(key)
            if old['plan'] is not None and old['plan'] != plan:
                raise ContractError('已保存的问答计划不能被另一个版本覆盖。')
            if old['state'] not in ('queued', 'running'):
                raise ContractError('仅待处理问题可以准备证据。')
            db.execute("UPDATE qa_jobs SET state='running',plan=? WHERE id=?", (encoded(plan), key))

    def seed_thread(self, conversation, parent_message_id):
        if conversation == 'chat' or not parent_message_id:
            return
        rows = self.daily.rows("SELECT plan,result,created FROM qa_jobs WHERE state='sent' AND json_extract(delivery,'$.message_id')=?", (parent_message_id,))
        if not rows:
            return
        row = rows[0]
        if row['plan'] is None or self.daily.now()-row['created'] > 6*3600:
            return
        plan, result = json.loads(row['plan']), json.loads(row['result'])
        query = plan['query']
        context = dict(question=query['question'], selected=query['selected'],
            materials=list(dict.fromkeys(h['material_id'] for h in result['hits']))[:8],
            year_from=query.get('year_from'), year_to=query.get('year_to'),time_scope=query.get('time_scope'))
        if query['mode']=='clarify':
            context['pending_time']=query
        with self.daily.transaction() as db:
            db.execute('INSERT OR IGNORE INTO qa_context VALUES (?,?,?)', (conversation, encoded(context), row['created']))

    def complete(self, key, result, context=None):
        with self.daily.transaction() as db:
            job = self.get(key)
            if job['state'] not in ('queued', 'running'):
                raise ContractError('问题已经完成，请勿重复覆盖。')
            db.execute("UPDATE qa_jobs SET state='ready',result=? WHERE id=?", (encoded(result), key))
            if context is not None:
                db.execute('INSERT INTO qa_context VALUES (?,?,?) ON CONFLICT(conversation) DO UPDATE SET value=excluded.value,updated=excluded.updated',
                           (job['conversation'], encoded(context), self.daily.now()))
            self.daily._event(db, 'qa_ready', dict(job_id=key, status=result['status']))

    def delivered(self, key, proof):
        with self.daily.transaction() as db:
            db.execute("UPDATE qa_jobs SET state='sent',delivery=? WHERE id=? AND state='ready'", (encoded(proof), key))

    def call(self, key):
        rows = self.daily.rows('SELECT * FROM qa_calls WHERE job_id=?', (key,))
        return self.decode(rows[0]) if rows else None

    def reserve(self, key, digest, checked_access):
        checked_access()
        with self.daily.transaction() as db:
            if self.get(key)['state'] != 'running':
                raise ContractError('只有正在处理的问题可使用模型。')
            if db.execute('SELECT 1 FROM qa_calls WHERE job_id=?', (key,)).fetchone():
                raise Deferred('qa_model_already_reserved')
            if self.daily.policy.model_limit_reached(db.execute('SELECT count(*) FROM daily_models').fetchone()[0]):
                raise Deferred('model_limit')
            model_key = stable_key('P43-qa-model', key)
            checked_access()
            proof = dict(purpose='knowledge_qa', job_id=key, request_sha256=digest)
            db.execute("INSERT INTO daily_models VALUES (?,1,'started',?,?)", (model_key, encoded(proof), self.daily.now()))
            db.execute("INSERT INTO qa_calls VALUES (?,?,?,'started',NULL)", (key, model_key, digest))
            self.daily._event(db, 'qa_model_reserved', proof)

    def call_result(self, key, state, result):
        if state not in ('validated', 'rejected', 'unknown'):
            raise ContractError('无效的问答模型状态。')
        with self.daily.transaction() as db:
            call = self.call(key)
            if not call or call['state'] not in ('started', 'unknown'):
                raise ContractError('问答模型调用已结束或尚未登记。')
            db.execute('UPDATE qa_calls SET state=?,result=? WHERE job_id=?', (state, encoded(result), key))
            db.execute('UPDATE daily_models SET state=?,evidence=?,updated=? WHERE source_sha=? AND attempt=1',
                       (state, encoded(dict(purpose='knowledge_qa', job_id=key, result=result)), self.daily.now(), call['model_key']))
