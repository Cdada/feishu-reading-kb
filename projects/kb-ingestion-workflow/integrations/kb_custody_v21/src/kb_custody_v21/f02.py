"""F02: one approved hardening of the existing F01 group, never a new group.

The predecessor journal remains immutable. Its byte hash, successful receipts
and exact identities anchor this separate, fixed-location request journal.
"""
import json
from pathlib import Path

from .contracts import ContractError
from .f01 import Experiment, PREFIX, identity, sha
from .runtime import no_symlinks
from .s6 import instant

LIMITS = dict(auth=1, bot=1, create=0, chat=7, members=8, update=1,
              files=3, images=1, send=6, list=2, message=4, download=4)
SCOPES = sorted(['im:chat:create', 'im:chat:read', 'im:chat.members:read',
    'im:message:send_as_bot', 'im:message:readonly', 'im:message.group_msg', 'im:resource', 'im:chat:update'])
ISOLATION = dict(add_member_permission='only_owner', share_card_permission='not_allowed')
PREVIOUS_COUNTS = dict(auth=1, bot=1, create=1, chat=1)


def administration(info):
    # Bot-created F01 group returned no owner_id and two empty manager lists.
    # Preserve that exact response contract; never invent a human/bot owner.
    return {key: info.get(key) for key in ('owner_id', 'bot_manager_id_list', 'user_manager_id_list')}


class ExistingGroupExperiment(Experiment):
    reference, limits = 'F02', LIMITS

    def __init__(self, predecessor_root, approval, **options):
        self.predecessor = no_symlinks(Path(predecessor_root).absolute())
        if approval.get('predecessor_root') != str(self.predecessor):
            raise ContractError('approved predecessor location required; no fresh budget location')
        self.previous_path = no_symlinks(self.predecessor / 'journal.local.jsonl')
        self.previous_sha = approval.get('predecessor_sha256')
        raw = self.previous_path.read_bytes()
        if sha(raw) != self.previous_sha:
            raise ContractError('F01 evidence identity changed')
        self._validate_predecessor(raw, approval)
        if (approval.get('scopes') != SCOPES or approval.get('published_version') != '0.1.1'
                or approval.get('publish_scope') != 'only_bound_account'
                or approval.get('update_method') != 'PUT'
                or approval.get('automatic_retries') != 0
                or approval.get('update_payload') != ISOLATION):
            raise ContractError('exact F02 permissions, publication and update approval required')
        for key, prefix in [('chat_id', 'oc_'), ('bot_id', 'ou_')]:
            identity(approval.get(key), prefix)
        admins = approval.get('administration', {})
        if (set(admins) != {'owner_id', 'bot_manager_id_list', 'user_manager_id_list'}
                or admins['owner_id'] not in (None, approval['account_id'], approval['bot_id'])
                or admins['bot_manager_id_list'] != [] or admins['user_manager_id_list'] != []):
            raise ContractError('approved singular group must not have additional managers')
        self._validate_receipts(raw, approval)
        # Fixed sibling path prevents an alternate run directory resetting the
        # budget for this predecessor. The old F01 journal is never appended.
        super().__init__(self.execution_root(), approval, **options)
        try:
            if any('intent' in event for event in self.events):
                raise ContractError(self.reference + ' already attempted; no restart or fresh authentication')
            self.bind('chat', approval['chat_id'])
        except Exception:
            self.close()
            raise

    def execution_root(self):
        return self.predecessor.parent / 'f02-requests'

    @staticmethod
    def _validate_predecessor(raw, approval):
        events = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
        previous = events[0].get('approval', {}) if events else {}
        if (previous.get('reference') != 'F01' or previous.get('approved') is not True
                or previous.get('synthetic_only') is not True
                or any(previous.get(key) != approval.get(key)
                       for key in ('app_id', 'account_id', 'fixtures', 'notifications'))
                or instant(approval['starts_at']) < instant(previous['expires_at'])):
            raise ContractError('F02 must preserve F01 objects and fixtures in a new window')
        intents = [e for e in events if 'intent' in e]
        completed = [e['complete'] for e in events if 'complete' in e and e.get('http_status') == 200]
        counts = {kind: sum(e.get('kind') == kind for e in intents) for kind in PREVIOUS_COUNTS}
        if (len(intents) != 4 or counts != PREVIOUS_COUNTS or len(completed) != 4
                or len(set(completed)) != 4 or {e['intent'] for e in intents} != set(completed)
                or not any(e.get('stopped') == 'group identity/membership mismatch' for e in events)):
            raise ContractError('F01 must have exactly four completed requests and the known isolation stop')
        for name in ('chat', 'bot'):
            bindings = [e['value'] for e in events if e.get('binding') == name]
            if bindings != [approval[name + '_id']]:
                raise ContractError('F01 successful object binding differs from F02')

    def check(self):
        super().check()
        if sha(self.previous_path.read_bytes()) != self.previous_sha:
            raise ContractError('F01 evidence changed; F02 stopped before network')

    def _validate_receipts(self, raw, approval):
        events = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
        receipts = {}
        for event in events:
            if 'intent' not in event or event['kind'] == 'auth':
                continue  # Authentication response is intentionally never stored.
            key = event['intent']
            content = no_symlinks(self.predecessor / (sha(key.encode()) + '.response.local')).read_bytes()
            completed = next(e for e in events if e.get('complete') == key)
            if sha(content) != completed.get('response_sha256'):
                raise ContractError('F01 successful receipt bytes changed')
            response = json.loads(content)
            if response.get('code') != 0:
                raise ContractError('F01 receipt is not a successful provider response')
            receipts[event['kind']] = response
        group = receipts['chat'].get('data', {})
        if (receipts['bot'].get('bot', {}).get('open_id') != approval['bot_id']
                or receipts['create'].get('data', {}).get('chat_id') != approval['chat_id']
                or administration(group) != approval['administration']):
            raise ContractError('F01 successful receipt identity differs from approval')

    def allowed_paths(self):
        paths = super().allowed_paths()
        if self.value('prechecked') and not self.value('hardened'):
            paths['update'] = {('PUT', PREFIX + '/im/v1/chats/' + self.approval['chat_id'])}
        # A stale/forged local verified flag cannot bypass the hardening phase.
        if not self.value('hardened'):
            for kind in ('files', 'images', 'send', 'list', 'message', 'download'):
                paths.pop(kind, None)
        return paths

    def request(self, kind, method, path, data=None, *, key, **options):
        if kind == 'update' and (method != 'PUT' or data != ISOLATION or key != 'update:1'):
            raise ContractError('only the exact two-field update is approved')
        return super().request(kind, method, path, data, key=key, **options)

    def bot(self):
        bot = super().bot()
        if bot['open_id'] != self.approval['bot_id']:
            self.append({'stopped': 'bot differs from F01 receipt'})
            raise ContractError('bound bot identity mismatch')
        return bot

    def inspect_group(self, batch, *, strict):
        if self.value('bot') != self.approval['bot_id']:
            raise ContractError('bound bot must be verified first')
        path = PREFIX + '/im/v1/chats/' + self.approval['chat_id']
        info = self.request('chat', 'GET', path, key='chat:' + batch).get('data', {})
        if (info.get('chat_mode') != 'group' or info.get('chat_type') != 'private'
                or info.get('external') is not False or str(info.get('user_count')) != '1'
                or str(info.get('bot_count')) != '1' or administration(info) != self.approval['administration']
                or strict and any(info.get(k) != v for k, v in ISOLATION.items())):
            self.append({'stopped': 'group identity/isolation mismatch'})
            raise ContractError('dedicated group isolation check failed')
        members = self.request('members', 'GET', path + '/members?member_id_type=open_id&page_size=100',
                               key='members:' + batch).get('data', {})
        items = members.get('items', [])
        if (members.get('has_more') is not False or members.get('trigger_security_conf_limit') is True
                or len(items) != 1 or items[0].get('member_id') != self.approval['account_id']
                or items[0].get('member_id_type') != 'open_id'):
            self.append({'stopped': 'member binding mismatch'})
            raise ContractError('complete singular user membership required')
        return info

    def establish(self):
        self.bot()
        self.inspect_group('before-update', strict=False)
        self.bind('prechecked', True)
        self.request('update', 'PUT', PREFIX + '/im/v1/chats/' + self.approval['chat_id'],
                     dict(ISOLATION), key='update:1')
        self.inspect_group('after-update', strict=True)
        self.bind('hardened', True)
        self.bind('verified', True)

    def verify(self, batch):
        if not self.value('hardened'):
            raise ContractError('verified hardening required before transfer')
        self.inspect_group(batch, strict=True)
