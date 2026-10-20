"""Durable P32 scheduling state, separate from the authoritative material registry.

Connections are short lived so the collector and the one heavy worker can use
the same store. No transaction is held across a network/model call. An intent
survives a crash; only explicit evidence resolution can make it retryable.
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .contracts import ContractError
from .daily_policy import DailyPolicy
from .registry import encoded
from .runtime import no_symlinks


class Deferred(Exception):
    def __init__(self, reason, until=None):
        super().__init__(reason)
        self.reason, self.until = reason, until


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_messages(
 id TEXT NOT NULL, version TEXT NOT NULL, observed REAL NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS daily_documents(
 message_id TEXT PRIMARY KEY, filename TEXT NOT NULL, phase TEXT NOT NULL,
 source_sha TEXT, canonical_id TEXT, entry_id TEXT, publication_id TEXT,
 preview_message_id TEXT, user_action TEXT, reason TEXT,
 created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS daily_documents_phase ON daily_documents(phase, created);
CREATE INDEX IF NOT EXISTS daily_documents_source ON daily_documents(source_sha);
CREATE TABLE IF NOT EXISTS daily_scans(
 channel TEXT PRIMARY KEY, completed REAL NOT NULL, start REAL,
 end REAL, token TEXT, pages INTEGER NOT NULL DEFAULT 0,
 gap_start REAL, gap_end REAL);
CREATE TABLE IF NOT EXISTS daily_operations(
 op_key TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 due REAL NOT NULL DEFAULT 0, result TEXT, error TEXT, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS daily_requests(
 id INTEGER PRIMARY KEY, op_key TEXT NOT NULL, attempt INTEGER NOT NULL,
 kind TEXT NOT NULL, at REAL NOT NULL, count INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS daily_requests_at ON daily_requests(at);
CREATE TABLE IF NOT EXISTS daily_models(
 source_sha TEXT NOT NULL, attempt INTEGER NOT NULL,
 state TEXT NOT NULL, evidence TEXT, updated REAL NOT NULL,
 PRIMARY KEY(source_sha, attempt));
CREATE TABLE IF NOT EXISTS daily_events(
 id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
"""

PHASES = {
    'registered': {'original_saved', 'needs_upload', 'held'},
    'original_saved': {'converted', 'held', 'needs_upload', 'duplicate'},
    'converted': {'generating', 'awaiting_images', 'held'},
    'awaiting_images': {'converted', 'needs_upload', 'held'},
    'generating': {'guide_ready', 'model_unknown', 'held'},
    'model_unknown': {'generating', 'guide_ready', 'held'},
    'guide_ready': {'preview_ready', 'held'},
    'preview_ready': {'awaiting_confirmation', 'send_unknown', 'held'},
    'send_unknown': {'awaiting_confirmation', 'held'},
    'awaiting_confirmation': {'confirmed', 'rejected', 'held'},
    'confirmed': {'published', 'reading', 'rejected', 'held'},
    'reading': {'reading_wait', 'published', 'held'},
    'reading_wait': {'reading', 'held'},
    'published': {'notified'},
    'notified': set(), 'rejected': set(), 'duplicate': set(),
    'needs_upload': {'registered'}, 'held': set(),
}


def _hash(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class DailyStore:
    def __init__(self, path, policy: DailyPolicy, *, clock=time.time):
        self.path = no_symlinks(Path(path).absolute())
        self.policy, self.clock = policy, clock
        if (not self.path.exists() or self.path.stat().st_size==0) and any(
                (self.path.parent/name).exists() for name in ('requests','model-grants')):
            raise ContractError('daily ledger is missing beside retained requests; refuse to reset usage')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'daily_meta' not in tables:
                raise ContractError('refusing to modify an unrelated database')
            if 'daily_meta' in tables and self._get(db, 'schema') != 1:
                raise ContractError('unsupported daily database schema')
            db.executescript(SCHEMA)
        self.path.chmod(0o600)
        with self.transaction() as db:
            version = self._get(db, 'schema')
            if version not in (None, 1):
                raise ContractError('unsupported daily database schema')
            scope = self._get(db, 'scope')
            if scope is not None and scope != policy.scope:
                raise ContractError('daily database belongs to another identity')
            self._put(db, 'schema', 1)
            self._put(db, 'scope', policy.scope)
            # Same ledger survives policy replacement. Budgets are never reset.
            if self._get(db, 'policy_digest') != policy.digest:
                self._event(db, 'policy_opened', dict(digest=policy.digest))
                self._put(db, 'policy_digest', policy.digest)
                self._put(db, 'policy', asdict(policy))

    def now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ContractError('valid UTC timestamp required')
        return float(value)

    @contextmanager
    def connection(self):
        for suffix in ('', '-wal', '-shm', '-journal'):
            no_symlinks(str(self.path) + suffix)
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                # Fail closed on a backward wall clock: rolling quotas must not
                # be reset by moving the system time or restoring an old config.
                previous = self._get(db, 'last_time')
                current = self.now()
                if previous is not None and current < previous:
                    raise ContractError('clock moved backwards; retain ledger and inspect time')
                self._put(db, 'last_time', current)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _get(db, key):
        row = db.execute('SELECT value FROM daily_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db, key, value):
        db.execute('INSERT INTO daily_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, encoded(value)))

    def _event(self, db, kind, value):
        db.execute('INSERT INTO daily_events(at,kind,payload) VALUES (?,?,?)',
                   (self.now(), kind, encoded(value)))

    def rows(self, sql, values=()):
        with self.connection() as db:
            return [dict(r) for r in db.execute(sql, values)]

    def get(self, key):
        with self.connection() as db:
            return self._get(db, key)

    def put(self, key, value):
        with self.transaction() as db:
            self._put(db, key, value)

    def scan(self, channel, *, initial_start):
        """Return a stable interval and cursor; restarts reuse its original end."""
        if not isinstance(channel, str) or not channel or len(channel) > 200:
            raise ContractError('explicit scan channel required')
        now = self.now()
        if type(initial_start) not in (int, float) or not 0 <= initial_start <= now:
            raise ContractError('invalid initial scan boundary')
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO daily_scans(channel,completed) VALUES (?,?)',
                       (channel, initial_start))
            row = db.execute('SELECT * FROM daily_scans WHERE channel=?', (channel,)).fetchone()
            if row['start'] is None:
                wanted = max(0, row['completed'] - self.policy.overlap_seconds)
                start = max(wanted, now - self.policy.history_seconds)
                gap = (row['completed'], start) if start > row['completed'] else (row['gap_start'], row['gap_end'])
                db.execute('UPDATE daily_scans SET start=?,end=?,token=NULL,pages=0,gap_start=?,gap_end=? WHERE channel=?',
                           (start, now, *gap, channel))
            return dict(db.execute('SELECT * FROM daily_scans WHERE channel=?', (channel,)).fetchone())

    def save_page(self, scan, items, *, has_more, next_token=None):
        """Atomically retain a whole page, queue files, then advance the cursor.

        Identity filtering is repeated here even after a transport validated it.
        A changed message version is retained for edited owner replies. A file
        message cannot silently change its original resource identity.
        """
        if (type(has_more) is not bool or not isinstance(items, list)
                or len(items) > self.policy.page_size
                or has_more and (not isinstance(next_token, str) or not next_token
                                  or next_token == scan['token'] or len(next_token) > 4096)):
            raise ContractError('complete bounded page and progressing token required')
        # Retain the whole observation before applying the file-queue limit.
        # Owner replies on a full-queue page must remain available to free work;
        # otherwise awaiting PDFs and new files could block each other forever.
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_scans WHERE channel=?', (scan['channel'],)).fetchone()
            if not row or dict(row) != scan:
                raise ContractError('stale scan result cannot add observations')
            for item in items:
                self._observe(db, item, register=False)
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_scans WHERE channel=?', (scan['channel'],)).fetchone()
            if not row or dict(row) != scan:
                raise ContractError('stale scan result cannot move the current cursor')
            full=False
            for item in items:
                try:
                    self._observe(db, item)
                except Deferred as error:
                    if error.reason!='queue_full':
                        raise
                    full=True
            if not full:
                if has_more:
                    db.execute('UPDATE daily_scans SET token=?,pages=pages+1 WHERE channel=?', (next_token, scan['channel']))
                else:
                    db.execute('UPDATE daily_scans SET completed=end,start=NULL,end=NULL,token=NULL,pages=0 WHERE channel=?',
                               (scan['channel'],))
        if full:
            # Admit the available prefix, retain the unadvanced whole page.
            # This also works when the configured queue is smaller than a page.
            raise Deferred('queue_full')

    def _observe(self, db, item, *, register=True):
        if not isinstance(item, dict):
            raise ContractError('native message object required')
        sender = item.get('sender', {})
        if item.get('chat_id') != self.policy.chat_id:
            raise ContractError('history response belongs to another chat')
        mid = item.get('message_id')
        if not isinstance(mid, str) or not re.fullmatch(r'om_[A-Za-z0-9_-]+', mid):
            raise ContractError('stable platform message identity required')
        # Other senders and deleted records never become intake/owner decisions.
        if (sender.get('id'), sender.get('id_type'), sender.get('sender_type')) != (
                self.policy.account_id, 'open_id', 'user') or type(item.get('deleted')) is not bool:
            return
        raw = encoded(item)
        if len(raw.encode()) > 65536:
            raise ContractError('message exceeds bounded metadata size')
        db.execute('INSERT OR IGNORE INTO daily_messages VALUES (?,?,?,?)', (mid, _hash(item), self.now(), raw))
        if item.get('deleted') or item.get('msg_type') != 'file' or not register:
            return
        try:
            body = json.loads(item['body']['content'])
            filename, file_key = body['file_name'], body['file_key']
            if not isinstance(filename, str) or not filename or not isinstance(file_key, str) or not file_key:
                raise ValueError('incomplete resource')
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError('native file metadata required') from error
        old = db.execute('SELECT * FROM daily_documents WHERE message_id=?', (mid,)).fetchone()
        resource = dict(file_key=file_key, filename=filename)
        if old:
            if json.loads(old['data'])['resource'] != resource:
                raise ContractError('file message changed its frozen resource')
            return
        pending = db.execute("SELECT count(*) FROM daily_documents WHERE phase NOT IN ('notified','rejected','duplicate')").fetchone()[0]
        if pending >= self.policy.max_pending:
            raise Deferred('queue_full')
        db.execute('''INSERT INTO daily_documents(message_id,filename,phase,created,updated,data)
                      VALUES (?,?,'registered',?,?,?)''',
                   (mid, filename, self.now(), self.now(), encoded(dict(resource=resource))))

    def document(self, message_id):
        rows = self.rows('SELECT * FROM daily_documents WHERE message_id=?', (message_id,))
        if len(rows) != 1:
            raise ContractError('unknown daily material')
        value = rows[0]
        value['data'] = json.loads(value['data'])
        return value

    def advance(self, message_id, expected, target, *, data=None, **fields):
        if target not in PHASES.get(expected, set()):
            raise ContractError('invalid daily phase transition')
        allowed = {'entry_id', 'publication_id', 'preview_message_id', 'user_action', 'reason'}
        if set(fields) - allowed:
            raise ContractError('unknown daily material field')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_documents WHERE message_id=?', (message_id,)).fetchone()
            if not row or row['phase'] != expected:
                raise ContractError('material phase changed')
            payload = json.loads(row['data'])
            if data:
                if 'resource' in data and data['resource'] != payload['resource']:
                    raise ContractError('cannot change original resource')
                payload.update(data)
            changes = dict(fields, phase=target, updated=self.now(), data=encoded(payload))
            db.execute('UPDATE daily_documents SET ' + ','.join(k+'=?' for k in changes) + ' WHERE message_id=?',
                       (*changes.values(), message_id))
            self._event(db, 'phase', dict(message_id=message_id, before=expected, after=target))

    def bind_source(self, message_id, digest):
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ContractError('verified original SHA-256 required')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_documents WHERE message_id=?', (message_id,)).fetchone()
            if not row or row['phase'] != 'original_saved' or row['source_sha'] not in (None, digest):
                raise ContractError('source binding requires the same saved original')
            prior = db.execute('SELECT message_id FROM daily_documents WHERE source_sha=? AND canonical_id IS NULL ORDER BY created,message_id LIMIT 1',
                               (digest,)).fetchone()
            canonical = prior[0] if prior and prior[0] != message_id else None
            db.execute('UPDATE daily_documents SET source_sha=?,canonical_id=?,updated=? WHERE message_id=?',
                       (digest, canonical, self.now(), message_id))
            return canonical

    def operation(self, key, action, payload):
        if not isinstance(key, str) or not key or len(key) > 300:
            raise ContractError('stable bounded operation key required')
        raw = encoded(payload)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO daily_operations(op_key,action,payload,state,updated) VALUES (?,?,?,'pending',?)",
                       (key, action, raw, self.now()))
            row = dict(db.execute('SELECT * FROM daily_operations WHERE op_key=?', (key,)).fetchone())
            if (row['action'], row['payload']) != (action, raw):
                raise ContractError('operation identity cannot change its request')
            return row

    def begin(self, key, *, physical_requests=0, kind='read', confirmation=False):
        if type(physical_requests) is not int or not 0 <= physical_requests <= 60:
            raise ContractError('explicit physical request reservation required')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_operations WHERE op_key=?', (key,)).fetchone()
            if not row:
                raise ContractError('prepare operation before attempting it')
            if row['state'] not in ('pending', 'retry'):
                raise Deferred('operation_' + row['state'])
            if row['due'] > self.now():
                raise Deferred('retry_wait', row['due'])
            if row['attempts'] >= self.policy.retry_attempts:
                raise Deferred('attempts_exhausted')
            payload = json.loads(row['payload'])
            if payload.get('mode')=='card_update':
                target = payload['card']['message_id']
                used = db.execute("SELECT coalesce(sum(attempts),0) FROM daily_operations WHERE json_extract(payload,'$.mode')='card_update' AND json_extract(payload,'$.card.message_id')=?", (target,)).fetchone()[0]
                if used >= 20:
                    raise Deferred('card_update_limit')
            if payload.get('mode')=='update':
                target = payload['progress']['message_id']
                used = db.execute("SELECT coalesce(sum(attempts),0) FROM daily_operations WHERE json_extract(payload,'$.mode')='update' AND json_extract(payload,'$.progress.message_id')=?", (target,)).fetchone()[0]
                if used >= 20:
                    raise Deferred('progress_update_limit')
            pause = self._get(db, 'network_pause')
            if physical_requests and pause and (pause['until'] is None or pause['until'] > self.now()):
                raise Deferred(pause['reason'], pause['until'])
            if physical_requests and pause and pause['until'] is not None and pause['until'] <= self.now():
                self._put(db, 'network_pause', None)
            if physical_requests:
                minute = db.execute('SELECT coalesce(sum(count),0) FROM daily_requests WHERE at>?', (self.now()-60,)).fetchone()[0]
                day = db.execute('SELECT coalesce(sum(count),0) FROM daily_requests WHERE at>?', (self.now()-86400,)).fetchone()[0]
                ceiling = self.policy.http_per_day - (0 if confirmation else self.policy.http_confirmation_reserve)
                if minute + physical_requests > self.policy.http_per_minute:
                    raise Deferred('request_rate', self.now()+5)
                if day + physical_requests > ceiling:
                    raise Deferred('request_daily_limit', self.now()+60)
            attempt = row['attempts'] + 1
            db.execute("UPDATE daily_operations SET state='started',attempts=?,updated=? WHERE op_key=?", (attempt, self.now(), key))
            if physical_requests:
                db.execute('INSERT INTO daily_requests(op_key,attempt,kind,at,count) VALUES (?,?,?,?,?)',
                           (key, attempt, kind, self.now(), physical_requests))
            return attempt

    def complete(self, key, attempt, result):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_operations WHERE op_key=?', (key,)).fetchone()
            if not row or row['attempts'] != attempt or row['state'] not in ('started', 'unknown'):
                raise ContractError('completion must match the outstanding attempt')
            db.execute("UPDATE daily_operations SET state='complete',result=?,updated=? WHERE op_key=?", (encoded(result), self.now(), key))

    def fail(self, key, attempt, reason, *, temporary=False, uncertain=False, retry_after=None, global_pause=False):
        if temporary and uncertain:
            raise ContractError('unknown effects must be reconciled before retry')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_operations WHERE op_key=?', (key,)).fetchone()
            if not row or row['state'] != 'started' or row['attempts'] != attempt:
                raise ContractError('failure must match current outstanding attempt')
            due = self.now()
            if uncertain:
                state = 'unknown'
            elif temporary and attempt < self.policy.retry_attempts:
                state = 'retry'
                due += self.policy.retry_delay(attempt, retry_after)
            else:
                state = 'failed'
            db.execute('UPDATE daily_operations SET state=?,due=?,error=?,updated=? WHERE op_key=?', (state, due, reason, self.now(), key))
            if global_pause:
                self._put(db, 'network_pause', dict(reason=reason, until=due if state=='retry' else None))

    def resolve_retry(self, key, evidence):
        """Adapter supplies positive reconciliation evidence; never infer by age."""
        if not isinstance(evidence, dict) or evidence.get('safe_to_retry') is not True or not evidence.get('receipts'):
            raise ContractError('explicit reconciliation evidence required')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM daily_operations WHERE op_key=?', (key,)).fetchone()
            if not row or row['state'] not in ('started', 'unknown'):
                raise ContractError('only unresolved operations can be reconciled')
            state = 'retry' if row['attempts'] < self.policy.retry_attempts else 'failed'
            db.execute('UPDATE daily_operations SET state=?,due=?,updated=? WHERE op_key=?', (state, self.now(), self.now(), key))
            self._event(db, 'retry_reconciled', dict(key=key, evidence=evidence))

    def reserve_model(self, source_sha):
        if not isinstance(source_sha, str) or not re.fullmatch('[a-f0-9]{64}', source_sha):
            raise ContractError('model reservation requires original SHA-256')
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM daily_models WHERE source_sha=? ORDER BY attempt', (source_sha,)).fetchall()
            if rows and (len(rows) >= 2 or rows[-1]['state'] != 'retry_authorized'):
                raise Deferred('model_already_reserved')
            count = db.execute('SELECT count(*) FROM daily_models').fetchone()[0]
            if count >= self.policy.model_turn_limit:
                raise Deferred('model_limit')
            attempt = len(rows) + 1
            db.execute("INSERT INTO daily_models VALUES (?,?,'started',NULL,?)", (source_sha, attempt, self.now()))
            return attempt

    def model_result(self, source_sha, attempt, state, evidence):
        if state not in ('validated', 'unknown', 'rejected') or not isinstance(evidence, dict):
            raise ContractError('explicit model result required')
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM daily_models WHERE source_sha=? ORDER BY attempt', (source_sha,)).fetchall()
            if not rows or attempt > len(rows) or attempt < 1:
                raise ContractError('unknown model attempt')
            if attempt != len(rows) or rows[-1]['state'] == 'retry_authorized':
                self._event(db, 'late_model_result', dict(source_sha=source_sha, attempt=attempt, state=state, evidence=evidence))
                return False
            if rows[-1]['state'] not in ('started', 'unknown'):
                raise ContractError('model result is already terminal')
            db.execute('UPDATE daily_models SET state=?,evidence=?,updated=? WHERE source_sha=? AND attempt=?',
                       (state, encoded(evidence), self.now(), source_sha, attempt))
            return True

    def allow_model_recovery(self, source_sha, evidence):
        if (not isinstance(evidence, dict) or evidence.get('checked_saved_results') is not True
                or evidence.get('old_attempt_fenced') is not True or not evidence.get('receipts')):
            raise ContractError('saved-result check and stale-result fence evidence required')
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM daily_models WHERE source_sha=? ORDER BY attempt', (source_sha,)).fetchall()
            if len(rows) != 1 or rows[0]['state'] not in ('started', 'unknown'):
                raise ContractError('only the first unresolved model attempt may be regenerated')
            db.execute("UPDATE daily_models SET state='retry_authorized',evidence=?,updated=? WHERE source_sha=? AND attempt=1",
                       (encoded(evidence), self.now(), source_sha))
            self._event(db, 'model_recovery_authorized', dict(source_sha=source_sha, evidence=evidence))

    def summary(self):
        rows = self.rows('SELECT * FROM daily_documents ORDER BY updated,message_id')
        published = [r for r in rows if r['phase'] in ('published', 'notified')]
        actions = [dict(filename=r['filename'], action=r['user_action'], reason=r['reason'],
                        preview_message_id=r['preview_message_id']) for r in rows if r['user_action']]
        uploads = [r['filename'] for r in rows if r['phase']=='needs_upload']
        active = [r for r in rows if r['phase'] not in ('published','notified','rejected','duplicate','needs_upload') and not r['user_action']]
        return dict(last_published=published[-1]['filename'] if published else None,
                    continuing_count=len(active), needs_action=actions, needs_upload=uploads,
                    pending_notifications=sum(r['phase']=='published' for r in rows),
                    model_turns=self.rows('SELECT count(*) AS n FROM daily_models')[0]['n'],
                    history_gaps=self.rows('SELECT channel,gap_start,gap_end FROM daily_scans WHERE gap_start IS NOT NULL'))
