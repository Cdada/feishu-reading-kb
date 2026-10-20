"""Separately approved continuation of one known F03 message, never a retry.

Original journals and their bad legacy key binding stay immutable. A new fixed
journal imports a mapping only from verified successful send/detail receipts.
No history search, group update, unknown lookup, or repeated TXT send is allowed.
"""
import json
from pathlib import Path

from .contracts import ContractError
from .f01 import PREFIX, identity, sha, validated_message_content
from .f02 import ExistingGroupExperiment
from .f03 import EVIDENCE_FILES as F02_FILES
from .runtime import no_symlinks
from .s6 import instant

LIMITS = dict(auth=1, bot=1, create=0, chat=6, members=6, update=0,
              files=2, images=1, send=5, list=0, message=6, download=4)
REUSE = dict(name='fixture.txt', source='completed_f03_send_and_detail',
             upload=False, send=False, current_detail=True, download=True)
OLD_KEYS = ['auth:1', 'bot:1', 'chat:before-update', 'members:before-update',
            'update:1', 'chat:after-update', 'members:after-update', 'chat:input-0',
            'members:input-0', 'upload:fixture.txt', 'send:fixture.txt', 'detail:fixture.txt']
EVIDENCE_FILES = {'journal.local.jsonl', 'result.local.json', 'executed-source.local.json'} | {
    sha(key.encode()) + '.response.local' for key in OLD_KEYS[1:]}


def check_files(root, hashes, expected):
    if set(hashes) != expected:
        raise ContractError('complete predecessor evidence inventory required')
    for name, digest in hashes.items():
        if sha(no_symlinks(root/name).read_bytes()) != digest:
            raise ContractError('predecessor evidence changed')


def known_f03_message(root, approval):
    """Read-only preflight; safe to run without creating a client or journal."""
    check_files(root, approval.get('f03_evidence_sha256', {}), EVIDENCE_FILES)
    events = [json.loads(line) for line in (root/'journal.local.jsonl').read_text().splitlines()]
    previous = events[0].get('approval', {}) if events else {}
    immutable = ('app_id', 'account_id', 'bot_id', 'chat_id', 'administration', 'fixtures',
                 'notifications', 'scopes', 'published_version', 'publish_scope',
                 'predecessor_root', 'predecessor_sha256', 'f02_root', 'f02_evidence_sha256')
    if (previous.get('reference') != 'F03' or previous.get('approved') is not True
            or previous.get('synthetic_only') is not True
            or any(previous.get(k) != approval.get(k) for k in immutable)):
        raise ContractError('F04 must preserve the exact approved F03 objects and evidence chain')
    f02 = no_symlinks(root.parent/'f02-requests')
    if approval.get('f02_root') != str(f02):
        raise ContractError('fixed F02 evidence location required')
    check_files(f02, approval['f02_evidence_sha256'], F02_FILES)
    intents = [e for e in events if 'intent' in e]
    completed = [e for e in events if 'complete' in e]
    kinds = ['auth', 'bot', 'chat', 'members', 'update', 'chat', 'members',
             'chat', 'members', 'files', 'send', 'message']
    stops = [e.get('stopped') for e in events if 'stopped' in e]
    if ([e['intent'] for e in intents] != OLD_KEYS
            or [e.get('kind') for e in intents] != kinds
            or [e['complete'] for e in completed] != OLD_KEYS
            or any(e.get('http_status') != 200 for e in completed)
            or stops != ['resource identity mismatch', 'orchestration']):
        raise ContractError('only the known completed twelve-request F03 stop is eligible')
    result = json.loads((root/'result.local.json').read_text())
    if (result.get('status') != 'STOPPED' or result.get('unfinished_requests') != 0
            or result.get('physical_requests') != {k:kinds.count(k) for k in LIMITS}
            or result.get('group_state_reconciliation') != 'updated_and_verified'
            or instant(approval['starts_at']) <= instant(result['ended_at'])):
        raise ContractError('new window after completed F03 required; no unknown continuation')
    source = json.loads((root/'executed-source.local.json').read_text())
    if (source.get('head') != previous.get('source_commit')
            or {k:v for k,v in source.items() if k != 'head'} != previous.get('source_snapshot')):
        raise ContractError('F03 executed source differs from its approval')
    receipts = {}
    for key in OLD_KEYS[1:]:
        raw = (root/(sha(key.encode())+'.response.local')).read_bytes()
        completion = next(e for e in completed if e['complete'] == key)
        value = json.loads(raw)
        if sha(raw) != completion.get('response_sha256') or value.get('code') != 0:
            raise ContractError('F03 successful receipt differs')
        receipts[key] = value
    upload_key = identity(receipts['upload:fixture.txt']['data'].get('file_key'), 'file_')
    item = receipts['send:fixture.txt']['data']
    options = dict(app_id=approval['app_id'], bot_id=approval['bot_id'], chat_id=approval['chat_id'],
                   kind='file', name='fixture.txt')
    content = validated_message_content(item, **options)
    details = receipts['detail:fixture.txt']['data'].get('items', [])
    if len(details) != 1:
        raise ContractError('one historical message detail required')
    detail = validated_message_content(details[0], message_id=item['message_id'], **options)
    if detail['file_key'] != content['file_key'] or content['file_key'] == upload_key:
        raise ContractError('known distinct upload key and matching send/detail keys required')
    digest = approval['fixtures']['fixture.txt']['sha256']
    upload = dict(resource_key=upload_key, resource_type='file', sha256=digest)
    def binding(name):
        return [e['value'] for e in events if e.get('binding') == name]
    legacy = dict(message_id=item['message_id'], **upload)
    if (binding('upload:fixture.txt') != [upload] or binding('message:fixture.txt') != [legacy]
            or binding('chat') != [approval['chat_id']] or binding('bot') != [approval['bot_id']]):
        raise ContractError('original F03 bindings differ; no inferred migration')
    return upload, dict(legacy, upload_resource_key=upload_key, resource_key=content['file_key'])


class KnownMessageExperiment(ExistingGroupExperiment):
    reference, limits = 'F04', LIMITS

    def __init__(self, predecessor_root, approval, **options):
        self.f03_root = no_symlinks(Path(predecessor_root).absolute().parent/'f03-requests')
        self.f03_hashes = dict(approval.get('f03_evidence_sha256', {}))
        self.f02_root = self.f03_root.parent/'f02-requests'
        self.f02_hashes = dict(approval.get('f02_evidence_sha256', {}))
        if (approval.get('f03_root') != str(self.f03_root) or approval.get('known_message_reuse') != REUSE
                or approval.get('unknown_lookup') != 0 or approval.get('group_update') is not False
                or approval.get('history_list') is not False):
            raise ContractError('exact known-message-only F04 continuation approval required')
        self.known_upload, self.known_message = known_f03_message(self.f03_root, approval)
        super().__init__(predecessor_root, approval, **options)

    def execution_root(self):
        return self.predecessor.parent/'f04-requests'

    def check(self):
        super().check()
        check_files(self.f03_root, self.f03_hashes, EVIDENCE_FILES)
        check_files(self.f02_root, self.f02_hashes, F02_FILES)

    def allowed_paths(self):
        paths = super().allowed_paths()
        for kind in ('create', 'update', 'list'):
            paths.pop(kind, None)
        return paths

    def establish(self):
        self.bot()
        self.inspect_group('initial', strict=True)
        self.bind('hardened', True)
        self.bind('verified', True)
        self.bind('upload:fixture.txt', self.known_upload)
        self.bind('message:fixture.txt', self.known_message)
        self.bind('reused_known_message', 'fixture.txt')

    def upload(self, name, data, *, image=False):
        if name == 'fixture.txt':
            raise ContractError('known TXT cannot be uploaded again')
        return super().upload(name, data, image=image)

    def send(self, name, *, text=None):
        if name == 'fixture.txt':
            raise ContractError('known TXT cannot be sent again')
        return super().send(name, text=text)

    def request(self, kind, method, path, data=None, *, key, **options):
        # Enforce the exclusion even if a caller bypasses upload()/send().
        if kind == 'files' and isinstance(data, bytes) and b'filename="fixture.txt"' in data:
            raise ContractError('known TXT upload forbidden')
        if kind == 'send' and isinstance(data, dict):
            content = json.loads(data.get('content', '{}'))
            if content.get('file_key') in (self.known_upload['resource_key'], self.known_message['resource_key']):
                raise ContractError('known TXT send forbidden')
        return super().request(kind, method, path, data, key=key, **options)

    def list_sent(self):
        """Read only two newly acknowledged notifications by exact ID, not history."""
        for index, text in enumerate(self.approval['notifications']):
            name = 'notification-' + str(index)
            message = self.value('message:' + name)
            if not message:
                raise ContractError('confirmed notification receipt required')
            result = self.request('message', 'GET', PREFIX+'/im/v1/messages/'+message['message_id'],
                                  key='detail:'+name).get('data', {}).get('items', [])
            if len(result) != 1:
                self.append({'stopped':'notification detail cardinality mismatch'})
                raise ContractError('one known notification required')
            self.message_content(result[0], kind='text', name=name, text=text, message_id=message['message_id'])
