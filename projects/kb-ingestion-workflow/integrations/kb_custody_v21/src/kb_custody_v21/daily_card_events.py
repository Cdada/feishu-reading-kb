"""Own one bounded listener; persist ingress before acknowledging the callback.

The listener's stdout is a private parent/child protocol, not a file-import
API. Card bodies are fetched later by the ordinary metered one-shot transport,
so network latency cannot delay the platform callback acknowledgement.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

from .contracts import ContractError, stable_key
from .daily_cards import bounded_json, _identity, render_card
from .daily_card_io import read_card
from .daily_card_readback import card_matches
from .daily_store import Deferred
from .daily_transport import durable_file
from .f05 import file_sha
from .registry import encoded
from .runtime import no_symlinks

SCHEMA = '''CREATE TABLE IF NOT EXISTS daily_card_inbox(
 event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
 proof TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, created REAL NOT NULL);'''


class EventIngress:
    def __init__(self, backend):
        self.backend, self.store = backend, backend.store
        with self.store.connection() as db:
            db.executescript(SCHEMA)

    def persist(self, event, *, app_id, session_path):
        b = self.backend
        b.check_authorization()
        p = b.authority.policy
        if (app_id!=p.app_id or event.get('type')!='card.action.trigger' or event.get('operator_id')!=p.account_id
                or event.get('chat_id')!=p.chat_id or event.get('host')!='im_message' or event.get('action_tag')!='button'):
            raise ContractError('listener callback scope differs')
        eid = _identity(event.get('event_id'), '')
        value = bounded_json(event.get('action_value'), 4096)
        card = b.card_flow.cards.get(value.get('card_id'))
        _identity(event.get('message_id'), 'om_')
        if card['message_id'] is not None and event.get('message_id') != card['message_id']:
            raise ContractError('listener callback message differs')
        # Token is not used for PATCH and must never enter saved ingress.
        if set(event) != {'type','event_id','timestamp','operator_id','chat_id','host','message_id','action_tag','action_value'}:
            raise ContractError('minimal credential-free listener event required')
        raw = encoded(event)
        if len(raw.encode())>65536:
            raise ContractError('oversized card event')
        digest = hashlib.sha256(raw.encode()).hexdigest()
        path = no_symlinks(session_path.parent/(stable_key('card-event',eid)+'.event.local.json'))
        durable_file(path, event)
        proof = dict(receipts={str(path):file_sha(path), str(session_path):file_sha(session_path)},
                     listener_session=str(session_path))
        with self.store.transaction() as db:
            old = db.execute('SELECT digest FROM daily_card_inbox WHERE event_id=?',(eid,)).fetchone()
            if old and old['digest']!=digest:
                raise ContractError('event ID reused with changed contents')
            if not old:
                db.execute('INSERT INTO daily_card_inbox VALUES (?,?,?,?,?,?,?)',
                    (eid,digest,raw,encoded(proof),'pending',None,self.store.now()))
        return eid

    def drain(self):
        b = self.backend
        for row in self.store.rows("SELECT * FROM daily_card_inbox WHERE state='pending' ORDER BY created,event_id LIMIT 20"):
            event = bounded_json(row['payload'])
            proof = json.loads(row['proof'])
            if any(file_sha(Path(p))!=h for p,h in proof['receipts'].items()):
                raise ContractError('saved callback evidence changed')
            value = bounded_json(event['action_value'],4096)
            card = b.card_flow.cards.get(value['card_id'])
            if card['purpose']=='qa' and getattr(b,'qa',None) is None:
                outcome = 'disabled'
            elif card['purpose'].startswith('relation_') and getattr(b,'relations',None) is None:
                outcome = 'disabled'
            elif card['state'] in ('completed','superseded'):
                outcome = 'duplicate' if card['state']=='completed' else 'stale'
            else:
                receipt = card['receipt']
                if receipt is None:
                    # A user can click between POST and the original-card GET.
                    # Keep ingress durable; only verified delivery admits choice.
                    self.store.put('card-event-wait:'+row['event_id'], dict(reason='card_delivery_pending'))
                    continue
                self.store.put('card-event-wait:'+row['event_id'], None)
                evidence = dict(receipt['sent']['evidence'], **receipt['target']['evidence'])
                actual, checked = read_card(b.transport, 'card-event-read:'+stable_key(row['event_id']),
                                           receipt['contract'], evidence)
                if not card_matches(actual, render_card(card)):
                    raise ContractError('callback card differs from its registered definition')
                event['card_content'] = encoded(actual)
                proof['receipts'].update(checked['evidence'])
                outcome = b.card_flow.receive(encoded(event), proof)
            with self.store.transaction() as db:
                db.execute("UPDATE daily_card_inbox SET state='processed',outcome=? WHERE event_id=?",(outcome,row['event_id']))


class CardListener:
    def __init__(self, backend, *, popen=subprocess.Popen):
        self.backend, self.store = backend, backend.store
        self.ingress = EventIngress(backend)
        self.popen = popen
        self.process = self.reader = None
        self._ready = threading.Event()
        self.failure = None
        self.root = no_symlinks(backend.authority.root/'card-events')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pending_http = {}

    @property
    def ready(self):
        return self._ready.is_set() and self.process is not None and self.process.poll() is None and self.failure is None

    def _handle(self, value):
        b = self.backend
        b.check_authorization()
        if b.stop.is_set():
            raise Deferred('stopping')
        kind = value.get('type')
        if kind=='check':
            return
        if kind=='reserve':
            category,path = value.get('kind'),value.get('path')
            if (category,path) not in (('event_bootstrap','/callback/ws/endpoint'),('event_upgrade','websocket')):
                raise ContractError('unapproved event request')
            key = 'card-event-http:'+self.session+':'+str(value['sequence'])
            self.store.operation(key, category, dict(kind=category,path=path,session=self.session))
            attempt = self.store.begin(key, physical_requests=1, kind=category, confirmation=True)
            if category in self.pending_http:
                raise ContractError('concurrent event requests are not supported')
            self.pending_http[category] = (key,attempt)
            durable_file(self.session_path.parent/(str(value['sequence'])+'.intent.local.json'),
                dict(reference='P40-event-http-v1',operation=key,attempt=attempt,kind=category,path=path,
                     rules_sha256=b.authority.digest,at=self.store.now()))
            return
        if kind=='http_complete':
            category = value.get('kind')
            if category!='event_bootstrap' or value.get('status')!=200 or category not in self.pending_http:
                raise ContractError('event response without a matching reservation')
            key,attempt = self.pending_http.pop(category)
            self.store.complete(key, attempt, dict(status=200,credential_payload_retained=False))
            return
        if kind=='ready':
            if 'event_upgrade' not in self.pending_http:
                raise ContractError('event listener ready without reserved handshake')
            key,attempt = self.pending_http.pop('event_upgrade')
            self.store.complete(key,attempt,dict(status=101,credential_payload_retained=False))
            self._ready.set()
            self.store.put('card_listener_status',dict(state='ready',at=self.store.now(),session=self.session))
            return
        if kind=='event':
            if not self._ready.is_set() or not isinstance(value.get('event'),dict):
                raise ContractError('callback from a listener that is not ready')
            event = value['event']
            if event.get('type') == 'im.message.receive_v1':
                if b.archive_mentions is None:
                    raise ContractError('archive mention listener is disabled')
                b.archive_mentions.persist(event, app_id=value.get('app_id'), session_path=self.session_path)
            else:
                self.ingress.persist(event,app_id=value.get('app_id'),session_path=self.session_path)
            return
        raise ContractError('unknown listener protocol message')

    def _read(self):
        sequence = 0
        try:
            while True:
                raw = self.process.stdout.readline(1024*1024+1)
                if not raw:
                    return
                if len(raw)>1024*1024 or not raw.endswith(b'\n'):
                    raise ContractError('listener protocol exceeds line bound')
                value = bounded_json(raw.decode(),1024*1024)
                if not isinstance(value,dict) or type(value.get('sequence')) is not int or value['sequence']!=sequence+1:
                    raise ContractError('listener protocol sequence differs')
                sequence = value['sequence']
                self._handle(value)
                self.process.stdin.write((encoded(dict(sequence=sequence,ok=True))+'\n').encode())
                self.process.stdin.flush()
        except Exception as error:
            # This is the child-protocol thread boundary: retain every failure
            # for the scheduler instead of silently losing the reader thread.
            self.failure = error
            self._ready.clear()
            self.store.put('card_listener_status',dict(state='failed',reason=str(error),at=self.store.now()))
            # Close only our child; the child cancels on EOF and exits normally.
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def _start(self):
        b = self.backend
        b.check_authorization()
        b.collector.verify_isolation(confirmation=True)
        generation = self.store.get('card-listener-generation')
        if generation is None:
            generation = uuid.uuid4().hex
            self.store.put('card-listener-generation',generation)
        key = 'card-listener:'+generation
        operation = self.store.operation(key,'events',dict(reference='P40-events-v1'))
        if operation['state'] in ('started','unknown'):
            self.store.resolve_retry(key,dict(safe_to_retry=True,receipts=[str(b.authority.path)],
                reason='owned_read_only_listener_process_no_longer_running'))
        attempt = self.store.begin(key,confirmation=True)
        self.operation, self.attempt = key, attempt
        self.session = uuid.uuid4().hex
        directory = no_symlinks(self.root/self.session)
        directory.mkdir(mode=0o700)
        a = b.authority
        self.started = self.store.now()
        now = datetime.fromtimestamp(self.started,timezone.utc)
        control = dict(reference='P40-events-v1', mode='events', root=str(self.root), profile=a.config['cli_profile'],
            app_id=a.policy.app_id,kind='message',key='owned-card-listener',path='',session=str(a.path),
            starts_at=now.isoformat(), expires_at=(now+timedelta(minutes=10)).isoformat(),frozen=a.check())
        self.session_path = directory/'listener.local.json'
        durable_file(self.session_path,control)
        helper = no_symlinks(Path(a.config['cli_guard']))
        if file_sha(helper)!=a.frozen[str(helper)]:
            raise ContractError('reviewed card listener binary required')
        env = {k:os.environ[k] for k in ('HOME','USER','LOGNAME','TMPDIR','LANG') if k in os.environ}
        env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin',GOMAXPROCS='1',GOMEMLIMIT='128MiB',GODEBUG='http2client=0',
                   LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
        self.failure = None
        self.pending_http = {}
        self._ready.clear()
        self.process = self.popen([str(helper),'--card-listener',str(self.session_path),file_sha(self.session_path)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)
        self.reader = threading.Thread(target=self._read,name='kb-card-listener',daemon=True)
        self.reader.start()
        self.store.put('card_listener_status',dict(state='starting',session=self.session,at=self.started))

    def tick(self, *, process_events=True):
        # Receiving and renewing the bounded listener only require local state.
        # A missing content volume must not expire the callback window.
        if process_events:
            self.ingress.drain()
        if self.failure is not None and not isinstance(self.failure, (Deferred, OSError)):
            raise ContractError('card listener stopped: '+str(self.failure)) from self.failure
        if self.backend.stop.is_set():
            return
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                if not self._ready.is_set() and self.store.now()-self.started>30:
                    self.close()
                else:
                    return
            else:
                self._finish(code)
        self._start()

    def _finish(self, code):
        self.reader.join(timeout=3)
        if self.reader.is_alive():
            raise ContractError('owned card reader failed to stop')
        for key,attempt in self.pending_http.values():
            self.store.fail(key,attempt,'event_read_interrupted')
        if code==0 and self.failure is None:
            self.store.complete(self.operation,self.attempt,dict(state='window_complete'))
            self.store.put('card-listener-generation',None)
        else:
            self.store.fail(self.operation,self.attempt,'card_listener_disconnected',temporary=True)
        for stream in (self.process.stdin,self.process.stdout):
            stream.close()
        self.process = self.reader = None
        self._ready.clear()

    def close(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise ContractError('owned card listener did not stop after SIGTERM') from error
        self._finish(code)
        self.store.put('card_listener_status',dict(state='stopped',at=self.store.now()))
