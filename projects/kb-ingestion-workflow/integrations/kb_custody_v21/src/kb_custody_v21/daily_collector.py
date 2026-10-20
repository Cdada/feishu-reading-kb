"""Shared Feishu history collection with persistent complete-interval cursors."""
import hashlib
import json
import threading
import uuid
from urllib.parse import urlencode

from .contracts import ContractError, stable_key
from .f01 import PREFIX
from .daily_store import Deferred
from .f02 import ISOLATION, administration
from .registry import encoded


class DailyCollector:
    def __init__(self, authority, store, transport):
        self.authority, self.store, self.transport = authority, store, transport
        self.isolation_lock=threading.Lock()

    def verify_isolation(self, *, force=False, confirmation=False):
        with self.isolation_lock:
            return self._verify_isolation(force=force,confirmation=confirmation)

    def _verify_isolation(self, *, force=False, confirmation=False):
        a = self.authority
        a.check()
        previous = self.store.get('isolation_verified')
        if not force and previous and previous['at']+900 > self.store.now():
            self.transport._checked_result(previous['receipts'][0])
            for receipt in previous['receipts'][1:]:
                self.transport._checked_result(receipt)
            return
        pending=self.store.get('isolation_pending')
        if pending is None:
            pending=dict(key='isolation:'+uuid.uuid4().hex,at=self.store.now())
            self.store.put('isolation_pending',pending)
        key=pending['key']
        chat = PREFIX+'/im/v1/chats/'+a.policy.chat_id
        bot = self.transport.request(key+':bot','identity',PREFIX+'/bot/v3/info',confirmation=confirmation)
        if bot['value'].get('bot',{}).get('open_id') != a.policy.bot_id:
            raise ContractError('configured bot identity changed')
        group = self.transport.request(key+':chat','identity',chat,confirmation=confirmation)
        info = group['value'].get('data',{})
        if (info.get('chat_mode')!='group' or info.get('chat_type')!='private'
                or info.get('external') is not False or str(info.get('user_count'))!='1'
                or str(info.get('bot_count'))!='1' or administration(info)!=a.config['administration']
                or any(info.get(k)!=v for k,v in ISOLATION.items())):
            raise ContractError('dedicated private group isolation changed')
        members = self.transport.request(key+':members','identity',chat+'/members?member_id_type=open_id&page_size=100',confirmation=confirmation)
        data = members['value'].get('data',{})
        items = data.get('items',[])
        if (data.get('has_more') is not False or data.get('trigger_security_conf_limit') is True
                or len(items)!=1 or items[0].get('member_id')!=a.policy.account_id
                or items[0].get('member_id_type')!='open_id'):
            raise ContractError('complete sole-member check failed')
        self.store.put('isolation_pending',None)
        if self.store.now()-pending['at']>60:
            raise Deferred('refresh_scope_after_network_recovery')
        self.store.put('isolation_verified',dict(at=pending['at'],receipts=[bot,group,members]))

    def _collect_chat(self):
        p = self.authority.policy
        initial = self.authority.config['initial_history_start']
        scan = self.store.scan('chat',initial_start=initial)
        for _ in range(p.pages_per_tick):
            params = dict(container_id_type='chat',container_id=p.chat_id,
                start_time=str(int(scan['start'])),end_time=str(int(scan['end'])),
                page_size=str(p.page_size),sort_type='ByCreateTimeAsc')
            if params['start_time']==params['end_time']:
                # Timestamp rounding must never manufacture a zero-width scan.
                return
            if scan['token']:
                params['page_token']=scan['token']
            key = 'history:'+stable_key(scan['channel'],scan['start'],scan['end'],scan['token'])
            waiting = bool(self.store.rows("SELECT 1 FROM daily_documents WHERE phase IN ('awaiting_confirmation','confirmed','published') LIMIT 1"))
            result = self.transport.request(key,'list',PREFIX+'/im/v1/messages?'+urlencode(sorted(params.items())),confirmation=waiting)
            data = result['value'].get('data',{})
            items, more = data.get('items'), data.get('has_more')
            if not isinstance(items,list) or type(more) is not bool:
                raise ContractError('complete native history page required')
            # Bind every saved observation to actual physical receipt evidence.
            # This happens before queue admission; a full queue still retains
            # exact confirmation provenance from the same page.
            for message in items:
                version = hashlib.sha256(encoded(message).encode()).hexdigest()
                self.store.put('observation:'+version,result)
            self.store.save_page(scan,items,has_more=more,next_token=data.get('page_token'))
            if not more:
                return
            scan = self.store.scan('chat',initial_start=initial)

    def collect(self):
        self.verify_isolation()
        delayed=None
        try:
            self._collect_chat()
        except Deferred as error:
            delayed=error
        # One pending root per tick, round robin: many old PDFs must not consume
        # the entire request budget or block the ordinary chat history scan.
        self._collect_thread()
        if delayed:
            raise delayed

    def _collect_thread(self):
        waiting=self.store.rows("SELECT * FROM daily_documents WHERE phase IN ('awaiting_confirmation','awaiting_images') ORDER BY created,message_id")
        if not waiting:
            return
        offset=self.store.get('thread_round_robin') or 0
        document=waiting[offset%len(waiting)]
        self.store.put('thread_round_robin',offset+1)
        payload=json.loads(document['data'])
        if document['phase']=='awaiting_confirmation':
            parent=document['preview_message_id']
            origin=payload['preview']['sent']
        else:
            parent=payload['image_notice']['message_id']
            origin=payload['image_notice']['receipt']
        self.transport._checked_result(origin)
        known=self.store.get('thread:'+parent)
        if not known:
            # A root can acquire a thread ID after it was sent. Read its current
            # platform envelope, not a guessed thread ID or a user-provided ID.
            bucket=str(int(self.store.now()//self.authority.policy.poll_seconds))
            receipt=self.transport.request('thread-root:'+parent+':'+bucket,'message',
                PREFIX+'/im/v1/messages/'+parent,evidence=origin['evidence'],confirmation=True)
            items=receipt['value'].get('data',{}).get('items',[])
            if len(items)!=1 or items[0].get('message_id')!=parent:
                raise ContractError('exact pending root message required')
            root=items[0]
            if root.get('deleted') is True:
                self.store.advance(document['message_id'],document['phase'],'held',user_action='contact_codex',
                    reason='对应的确认消息已删除；资料和导读保留，请由 Codex 接续。')
                return
            sender=root.get('sender',{})
            p=self.authority.policy
            if (root.get('chat_id')!=p.chat_id or root.get('deleted') is not False or sender.get('sender_type')!='app'
                    or (sender.get('id_type'),sender.get('id')) not in (('app_id',p.app_id),('open_id',p.bot_id))):
                raise ContractError('pending root sender or group changed')
            if not root.get('thread_id'):
                self._refresh_owner_reply(parent)
                return
            known=dict(thread_id=root['thread_id'],receipt=receipt)
            self.store.put('thread:'+parent,known)
        self.transport._checked_result(known['receipt'])
        channel='thread:'+known['thread_id']
        for _ in range(self.authority.policy.pages_per_tick):
            scan=self.store.scan(channel,initial_start=self.store.now())
            params=dict(container_id_type='thread',container_id=known['thread_id'],
                page_size=str(self.authority.policy.page_size),sort_type='ByCreateTimeAsc')
            if scan['token']:
                params['page_token']=scan['token']
            key='thread-history:'+stable_key(channel,scan['start'],scan['end'],scan['token'])
            result=self.transport.request(key,'list',PREFIX+'/im/v1/messages?'+urlencode(sorted(params.items())),
                evidence=known['receipt']['evidence'],confirmation=True)
            data=result['value'].get('data',{})
            items,more=data.get('items'),data.get('has_more')
            if not isinstance(items,list) or type(more) is not bool:
                raise ContractError('complete thread page required')
            for message in items:
                if message.get('thread_id')!=known['thread_id']:
                    raise ContractError('thread response belongs to another conversation')
                version=hashlib.sha256(encoded(message).encode()).hexdigest()
                self.store.put('observation:'+version,result)
            self.store.save_page(scan,items,has_more=more,next_token=data.get('page_token'))
            if not more:
                return

    def _refresh_owner_reply(self,parent):
        # Ordinary chat history is ordered by creation time. Editing an old
        # invalid reply does not move it back into the new-message interval.
        latest={}
        for row in self.store.rows('SELECT * FROM daily_messages ORDER BY observed,rowid'):
            item=json.loads(row['payload'])
            latest[item['message_id']]=item
        items=[item for item in latest.values() if item.get('parent_id')==parent and item.get('msg_type')=='text']
        if not items:
            return
        offset=self.store.get('reply_round_robin:'+parent) or 0
        observed=items[offset%len(items)]
        self.store.put('reply_round_robin:'+parent,offset+1)
        mid=observed['message_id']
        _,origin=self.observed(mid)
        bucket=str(int(self.store.now()//self.authority.policy.poll_seconds))
        result=self.transport.request('refresh-owner:'+mid+':'+bucket,'message',
            PREFIX+'/im/v1/messages/'+mid,evidence=origin['evidence'],confirmation=True)
        values=result['value'].get('data',{}).get('items',[])
        if len(values)!=1 or values[0].get('message_id')!=mid or values[0].get('parent_id')!=parent:
            raise ContractError('owner reply refresh changed message binding')
        item=values[0]
        p=self.authority.policy
        sender=item.get('sender',{})
        if (item.get('chat_id')!=p.chat_id or (sender.get('id'),sender.get('id_type'),sender.get('sender_type'))!=(p.account_id,'open_id','user')):
            raise ContractError('owner reply refresh changed identity')
        proof=dict(result,evidence=dict(origin['evidence'],**result['evidence']))
        version=hashlib.sha256(encoded(item).encode()).hexdigest()
        self.store.put('observation:'+version,proof)
        with self.store.transaction() as db:
            self.store._observe(db,item,register=False)

    def observed(self, message_id):
        rows = self.store.rows('SELECT * FROM daily_messages WHERE id=? ORDER BY observed DESC,rowid DESC', (message_id,))
        if not rows:
            raise ContractError('message has no saved platform observation')
        row = rows[0]
        receipt = self.store.get('observation:'+row['version'])
        if not receipt:
            raise ContractError('message observation has no physical receipt')
        self.transport._checked_result(receipt)
        return json.loads(row['payload']),receipt

    def message(self, message_id):
        observed, listed = self.observed(message_id)
        version = hashlib.sha256(encoded(observed).encode()).hexdigest()
        bucket=str(int(self.store.now()//self.authority.policy.poll_seconds)) if observed.get('msg_type')=='text' else 'original'
        response = self.transport.request('detail:'+message_id+':'+version+':'+bucket,'message',
            PREFIX+'/im/v1/messages/'+message_id, evidence=listed['evidence'],confirmation=observed.get('msg_type')=='text')
        items = response['value'].get('data',{}).get('items',[])
        sender = items[0].get('sender',{}) if len(items)==1 else {}
        p = self.authority.policy
        if (len(items)!=1 or items[0].get('message_id')!=message_id or items[0].get('chat_id')!=p.chat_id
                or (sender.get('id'),sender.get('id_type'),sender.get('sender_type'))!=(p.account_id,'open_id','user')):
            raise ContractError('exact message owner/group identity changed')
        return items[0],response,listed
