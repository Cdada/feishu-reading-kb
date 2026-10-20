"""A separately approved current-state check of the one failed F02 update.

F01/F02 evidence is read-only. This does not recover messages, rewrite a failed
intent, or restart F02. The new fixed journal may run only once after explicit
approval of both current-state observation and the conditional PUT.
"""
import json
from pathlib import Path

from .contracts import ContractError
from .f01 import PREFIX, encoded, sha
from .f02 import ExistingGroupExperiment, ISOLATION, administration
from .runtime import no_symlinks
from .s6 import instant

LIMITS = dict(auth=1, bot=1, create=0, chat=7, members=7, update=1,
              files=3, images=1, send=6, list=1, message=4, download=4)
LEGACY = dict(add_member_permission='all_members', share_card_permission='allowed')
RECONCILIATION = dict(read_current_group_state=True, if_legacy_state='one_put',
    if_target_state='skip_put', otherwise='stop', previous_intent_remains_unresolved=True)
OLD_KEYS = ['auth:1', 'bot:1', 'chat:before-update', 'members:before-update', 'update:1']
EVIDENCE_FILES = {'journal.local.jsonl', 'result.local.json', 'executed-source.local.json'} | {
    sha(key.encode())+'.response.local' for key in OLD_KEYS[1:]}


class ReconciledGroupExperiment(ExistingGroupExperiment):
    reference, limits = 'F03', LIMITS

    def __init__(self, predecessor_root, approval, **options):
        self.f02_root = no_symlinks(Path(predecessor_root).absolute().parent/'f02-requests')
        self.f02_hashes = dict(approval.get('f02_evidence_sha256', {}))
        if (approval.get('f02_root') != str(self.f02_root)
                or set(self.f02_hashes) != EVIDENCE_FILES
                or approval.get('group_state_reconciliation') != RECONCILIATION
                or approval.get('unknown_lookup') != 0):
            raise ContractError('separate exact F03 reconciliation approval required')
        self._check_f02_bytes()
        self._validate_f02(approval)
        super().__init__(predecessor_root, approval, **options)

    def execution_root(self):
        # Cannot be overridden by a user-provided run ID or output path.
        return self.predecessor.parent/'f03-requests'

    def _check_f02_bytes(self):
        for name, expected in self.f02_hashes.items():
            raw = no_symlinks(self.f02_root/name).read_bytes()
            if sha(raw) != expected:
                raise ContractError('F02 evidence changed; no continuation')

    def _validate_f02(self, approval):
        events = [json.loads(line) for line in (self.f02_root/'journal.local.jsonl').read_text().splitlines()]
        previous = events[0].get('approval', {}) if events else {}
        immutable = ('app_id', 'account_id', 'bot_id', 'chat_id', 'administration', 'fixtures',
                     'notifications', 'scopes', 'published_version', 'publish_scope', 'update_payload',
                     'predecessor_root', 'predecessor_sha256')
        if (previous.get('reference') != 'F02' or previous.get('approved') is not True
                or previous.get('synthetic_only') is not True
                or any(previous.get(k) != approval.get(k) for k in immutable)):
            raise ContractError('F03 must preserve the exact F02 objects and frozen materials')
        intents = [e for e in events if 'intent' in e]
        completed = [e for e in events if 'complete' in e]
        if ([e['intent'] for e in intents] != OLD_KEYS
                or [e.get('kind') for e in intents] != ['auth', 'bot', 'chat', 'members', 'update']
                or [e['complete'] for e in completed] != OLD_KEYS[:-1]
                or any(e.get('http_status') != 200 for e in completed)
                or any(e.get('binding') in ('hardened', 'verified') for e in events)
                or not any(e.get('stopped') == 'update:1' and e.get('error_type') == 'ContractError' for e in events)
                or intents[-1].get('method') != 'PATCH'
                or intents[-1].get('payload_sha256') != sha(encoded(ISOLATION).encode())):
            raise ContractError('only the known five-request F02 stop is eligible')
        for kind, field in [('chat', 'chat_id'), ('bot', 'bot_id')]:
            if [e['value'] for e in events if e.get('binding') == kind] != [approval[field]]:
                raise ContractError('F02 identity binding differs')
        result = json.loads((self.f02_root/'result.local.json').read_text())
        counts = {kind:sum(e['kind'] == kind for e in intents) for kind in self.limits}
        if (result.get('status') != 'STOPPED' or result.get('unfinished_requests') != 1
                or result.get('physical_requests') != counts
                or instant(approval['starts_at']) <= instant(previous['starts_at'])
                or instant(approval['starts_at']) < instant(result['ended_at'])):
            raise ContractError('consumed F02 and one newly approved window required')
        source = json.loads((self.f02_root/'executed-source.local.json').read_text())
        if (source.get('head') != previous.get('source_commit')
                or {k:v for k,v in source.items() if k != 'head'} != previous.get('source_snapshot')):
            raise ContractError('F02 executed source must match its original approval')
        receipts = {}
        for key in OLD_KEYS[1:]:
            raw = (self.f02_root/(sha(key.encode())+'.response.local')).read_bytes()
            if key == 'update:1':
                if raw != b'404 page not found':
                    raise ContractError('unexpected unresolved F02 response')
                continue  # This body is not proof that the remote state did not change.
            receipt = next(e for e in completed if e['complete'] == key)
            value = json.loads(raw)
            if sha(raw) != receipt.get('response_sha256') or value.get('code') != 0:
                raise ContractError('F02 successful receipt differs')
            receipts[key] = value
        members = receipts['members:before-update'].get('data', {})
        people = members.get('items', [])
        if (receipts['bot:1'].get('bot', {}).get('open_id') != approval['bot_id']
                or administration(receipts['chat:before-update'].get('data', {})) != approval['administration']
                or members.get('has_more') is not False or members.get('trigger_security_conf_limit') is True
                or len(people) != 1 or people[0].get('member_id') != approval['account_id']
                or people[0].get('member_id_type') != 'open_id'):
            raise ContractError('F02 receipt identities are not the approved singular group')

    def check(self):
        super().check()
        self._check_f02_bytes()

    def allowed_paths(self):
        paths = super().allowed_paths()
        if self.value('group_state_before') != LEGACY:
            paths.pop('update', None)
        return paths

    def establish(self):
        self.bot()
        info = self.inspect_group('before-update', strict=False)
        state = {key:info.get(key) for key in ISOLATION}
        if state not in (LEGACY, ISOLATION):
            self.append({'stopped':'unexpected current group settings'})
            raise ContractError('current state outside approved conditional branches')
        self.bind('group_state_before', state)
        self.bind('prechecked', True)
        if state == LEGACY:
            self.request('update', 'PUT', PREFIX+'/im/v1/chats/'+self.approval['chat_id'],
                         dict(ISOLATION), key='update:1')
        self.inspect_group('after-update', strict=True)
        self.bind('hardened', True)
        self.bind('verified', True)
        self.bind('group_state_reconciliation', 'updated_and_verified' if state == LEGACY else 'already_target_verified')
