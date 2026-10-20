"""One normal product-outbox send, guarded CLI transport, no unknown recovery.

The local input is the existing F01 synthetic TXT. It is not a real user entry.
Neither preparing this fixture nor importing this module accesses a platform.
"""
import json
from pathlib import Path

from .contracts import ContractError, ENTRY_SCHEMA, SUCCESS
from .f01 import Experiment, PREFIX, encoded, sha, validated_message_content
from .f01_flow import TXT
from .f02 import SCOPES
from .f05 import NotificationReadExperiment, file_sha
from .intake import FeishuMockIntake, MockAttachment
from .notification_review import observe_notification
from .notifications import OutboxWorker
from .registry import Registry
from .runtime import DEFAULTS, Runtime, no_symlinks
from .s6 import Sender, PREFIX as NOTICE_PREFIX
from .staging import Staging

LIMITS = dict(auth=5, bot=1, chat=1, members=1, send=1, message=1)
TEXT = NOTICE_PREFIX + SUCCESS
RUNTIME = '.dev-runtime/kb-v21/test-output/f06-live'
FIELDS = ('notification_id', 'provider_request_key', 'target_chat_id', 'scope_type',
          'scope_id', 'kind', 'payload', 'intake_id')


def prepare(worktree, chat_id, *, root=None):
    """Reuse intake/staging/transactional outbox, explicitly without a sender."""
    runtime = Runtime(worktree, DEFAULTS, root or Path(worktree)/RUNTIME)
    registry = Registry(runtime)
    if registry.rows('SELECT * FROM notification_outbox'):
        raise ContractError('F06 preparation already exists; preserve its stable key')
    adapter = FeishuMockIntake(registry, Staging(registry))
    adapter.receive(dict(schema_version=ENTRY_SCHEMA, intake_key='F06-synthetic-local',
        entry_key='fixture.txt', received_filename='fixture.txt',
        lineage={'chat_id':chat_id},
        expected={'size_bytes':len(TXT), 'sha256':sha(TXT)}), MockAttachment(TXT))
    return registry, binding(registry)


def binding(registry):
    """Prove local custody and the immutable notice; never hash a mutable WAL DB."""
    notices = registry.rows('SELECT * FROM notification_outbox')
    entries = registry.rows('SELECT * FROM intake_entries')
    if len(notices)!=1 or len(entries)!=1:
        raise ContractError('exactly one synthetic intake and notification required')
    item, entry = notices[0], registry.entry(entries[0]['entry_id'])
    receipt = registry.receipt(entry['entry_id'])
    if (item['kind']!='success' or item['scope_type']!='single_source' or item['payload']!=SUCCESS
            or item['scope_id']!=entry['acceptance_id'] or item['intake_id']!=entry['intake_id']
            or receipt['source_staging_status']!='accepted' or entry['sha256']!=sha(TXT)
            or entry['size_bytes']!=len(TXT)):
        raise ContractError('accepted synthetic original must precede a success notification')
    with Staging(registry).input_binding(entry['entry_id']) as source:
        if source['stream'].read()!=TXT:
            raise ContractError('frozen synthetic original changed')
    events = registry.rows("SELECT * FROM custody_events WHERE event_type='source_staging_accepted' AND aggregate_id=?",
                           (entry['acceptance_id'],))
    if len(events)!=1:
        raise ContractError('single durable accepted event required')
    return dict(item={k:item[k] for k in FIELDS}, entry_id=entry['entry_id'],
        asset_id=entry['asset_id'], acceptance_id=entry['acceptance_id'],
        source_sha256=sha(TXT), source_bytes=len(TXT), text=TEXT)


class OutboxExperiment(NotificationReadExperiment):
    reference, limits = 'F06', LIMITS

    def __init__(self, base, approval, registry, *, runner, clock=None):
        base = no_symlinks(Path(base).absolute())
        self.registry, self.config, self.runner = registry, approval, runner
        if (approval.get('base')!=str(base) or base.name!='f01-v1'
                or approval.get('runtime_relative')!=str(registry.runtime.root.relative_to(registry.runtime.worktree))
                or approval.get('automatic_retries')!=0 or approval.get('redirects')!=0
                or approval.get('unknown_lookup')!=0 or approval.get('same_key_repeat')!=0
                or approval.get('history_list') is not False or approval.get('logical_notifications')!=1
                or approval.get('scopes')!=SCOPES or approval.get('published_version')!='0.1.1'
                or approval.get('publish_scope')!='only_bound_account'
                or not isinstance(approval.get('cli_profile'),str) or not approval['cli_profile']
                or approval.get('outbox_binding')!=binding(registry)
                or approval['outbox_binding']['item']['target_chat_id']!=approval.get('chat_id')):
            raise ContractError('exact single-outbox F06 approval required')
        prior = approval.get('prior_evidence_sha256',{})
        required = {'f05-approval.local.json', 'f05-requests/result.local.json',
                    'f05-requests/executed-source.local.json', 'f05-requests/http.local.jsonl'}
        if set(prior)!=required:
            raise ContractError('fixed prior F05 evidence manifest required')
        self.frozen = {str(base/n):h for n,h in prior.items()}
        if any(file_sha(Path(p))!=h for p,h in self.frozen.items()):
            raise ContractError('prior evidence changed')
        old = json.loads((base/'f05-approval.local.json').read_text())
        if any(old.get(k)!=approval.get(k) for k in ('app_id','bot_id','chat_id','account_id',
                'administration','scopes','published_version','publish_scope','cli_profile')):
            raise ContractError('dedicated objects differ from previous verified experiment')
        result = json.loads((base/'f05-requests/result.local.json').read_text())
        if result.get('status')!='KNOWN_NOTIFICATION_CONTENT_VERIFIED' or result.get('unfinished_requests')!=0:
            raise ContractError('completed F05 evidence required; never resume its request budget')
        Experiment.__init__(self,base/'f06-requests',approval,clock=clock,sleep=lambda _:None)
        self.config = self.approval  # immutable copy owned by Experiment
        try:
            if any('setup_started' in e or 'intent' in e for e in self.events):
                raise ContractError('F06 already attempted; no repeat or automatic recovery')
            notices = registry.rows('SELECT * FROM notification_outbox')
            if notices[0]['delivery_state']!='pending' or registry.rows('SELECT * FROM notification_attempts'):
                raise ContractError('only the unattempted frozen notice may start F06')
        except Exception:
            self.close()
            raise

    def check(self):
        super().check()
        self.registry.runtime.gate()
        if binding(self.registry)!=self.approval['outbox_binding']:
            raise ContractError('synthetic custody or frozen notification changed')

    def require_frozen(self):
        self.check()
        if self.value('isolation_verified') is not True:
            raise ContractError('current dedicated object checks required before outbox send')

    def send_body(self):
        item = self.approval['outbox_binding']['item']
        return dict(receive_id=item['target_chat_id'],msg_type='text',
                    content=encoded({'text':TEXT}),uuid=item['provider_request_key'])

    def control(self,mode,key='',kind='',path=''):
        result = super().control(mode,key,kind,path)
        result['reference']='F06'
        if key=='send':
            result.update(mode='send',body=encoded(self.send_body()))
        return result

    def paths(self):
        chat = PREFIX+'/im/v1/chats/'+self.approval['chat_id']
        paths = dict(bot=('bot',PREFIX+'/bot/v3/info'),chat=('chat',chat),
            members=('members',chat+'/members?member_id_type=open_id&page_size=100'),
            send=('send',PREFIX+'/im/v1/messages?receive_id_type=chat_id'))
        receipt = self.value('send_receipt')
        if receipt:
            paths['notification-0']=('message',PREFIX+'/im/v1/messages/'+receipt['message_id'])
        return paths

    def call(self,kind,key,path):
        if key=='send':
            self.require_frozen()
            item = self.registry.rows('SELECT * FROM notification_outbox')[0]
            attempts = self.registry.rows('SELECT * FROM notification_attempts')
            if (item['delivery_state']!='sending' or len(attempts)!=1 or attempts[0]['operation']!='send'
                    or attempts[0]['result']!='started' or item['lease_token']!=attempts[0]['attempt_id']):
                raise ContractError('only actual worker may send its claimed immutable notice')
        return super().call(kind,key,path)

    def request(self,kind,method,path,body=None,summary=None):
        """S6 Sender adapter: do not expose an arbitrary CLI or HTTP endpoint."""
        if (kind!='send' or method!='POST' or (kind,path)!=self.paths()['send']
                or body!=self.send_body() or not summary
                or summary.get('provider_request_key')!=body['uuid']):
            raise ContractError('only persisted S6 sender payload is allowed')
        attempts = self.registry.rows('SELECT * FROM notification_attempts')
        if len(attempts)!=1 or summary.get('attempt')!=attempts[0]['attempt_id']:
            raise ContractError('sender attempt differs from worker claim')
        result = self.call('send','send',path)
        receipt = result.get('data')
        validated_message_content(receipt,kind='text',name='F06 outbox',text=TEXT,
            **{k:self.approval[k] for k in ('app_id','bot_id','chat_id')})
        self.bind('send_receipt',receipt)
        return result

    def run(self):
        try:
            self.verify_isolation()
            sender = Sender(self)
            worker = OutboxWorker(self.registry,sender)
            if worker.drain(limit=1)!=1:
                raise ContractError('frozen outbox was not dispatched')
            item = self.registry.rows('SELECT * FROM notification_outbox')[0]
            receipt = self.value('send_receipt')
            if not receipt or item['delivery_state']!='delivered' or item['provider_message_id']!=receipt['message_id']:
                raise ContractError('product outbox lacks a reliable persisted successful receipt')
            self.bind('outbox_delivered',dict(message_id=receipt['message_id'],notification_id=item['notification_id']))
            detail = self.call('message','notification-0',self.paths()['notification-0'][1])
            observation = observe_notification(receipt,detail.get('data',{}).get('items'),text=TEXT,
                **{k:self.approval[k] for k in ('app_id','bot_id','chat_id')})
            self.bind('observation:notification-0',observation)
            # Reopen storage/worker, not the consumed real experiment. A delivered
            # notice is skipped before any sender invocation; no platform lookup.
            reopened = Registry(self.registry.runtime)
            if OutboxWorker(reopened,sender).drain(limit=1)!=0:
                raise ContractError('delivered outbox unexpectedly dispatched on reopen')
            if len(reopened.rows('SELECT * FROM notification_attempts'))!=1:
                raise ContractError('unexpected duplicate outbox attempt')
            self.bind('reopened_worker_skipped_delivered',True)
            self.check()
            return observation
        except Exception as error:
            self.append(dict(stopped='outbox_orchestration',error_type=type(error).__name__))
            raise

    def observations(self):
        return {'0':self.value('observation:notification-0')}
