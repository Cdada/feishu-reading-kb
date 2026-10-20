"""F05: exact-ID reads of two acknowledged notifications, no sends or search."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .contracts import ContractError
from .f01 import Experiment, PREFIX, sha
from .f02 import ISOLATION, SCOPES, administration
from .notification_review import acknowledged_notifications, observe_notification
from .runtime import no_symlinks
from .s6 import instant

# A bounded large attachment may take minutes on a slow connection.
DOWNLOAD_TIMEOUT_SECONDS = 20 * 60

LIMITS = dict(auth=5, bot=1, chat=1, members=1, message=2)
RECEIPT_FILES = {'journal.local.jsonl'} | {
    sha(('send:notification-'+str(i)).encode())+'.response.local' for i in range(2)}
EVIDENCE_FILES = RECEIPT_FILES | {'result.local.json', 'executed-source.local.json'}


def file_sha(path):
    with no_symlinks(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def known_f04(root, approval):
    """Expected hashes come from the separately approved F05 evidence manifest."""
    hashes = approval.get('f04_evidence_sha256', {})
    if set(hashes) != EVIDENCE_FILES or any(file_sha(root/n) != h for n,h in hashes.items()):
        raise ContractError('exact frozen F04 source evidence required')
    events = [json.loads(line) for line in (root/'journal.local.jsonl').read_text().splitlines()]
    previous = events[0]['approval']
    for key in ('app_id', 'bot_id', 'chat_id', 'account_id', 'administration',
                'notifications', 'scopes', 'published_version', 'publish_scope'):
        if previous.get(key) != approval.get(key):
            raise ContractError('F05 objects must preserve the F04 binding')
    source = json.loads((root/'executed-source.local.json').read_text())
    if (source.get('head') != previous.get('source_commit')
            or {k:v for k,v in source.items() if k!='head'} != previous.get('source_snapshot')):
        raise ContractError('original executed source differs from F04 approval')
    result = json.loads((root/'result.local.json').read_text())
    counts = {k:sum(e.get('kind') == k and 'intent' in e for e in events)
              for k in previous['limits']}
    if (result.get('status') != 'STOPPED' or result.get('unfinished_requests') != 0
            or result.get('physical_requests') != counts
            or instant(approval['starts_at']) <= instant(result['ended_at'])):
        raise ContractError('new window after a completed known F04 stop required')
    return acknowledged_notifications(root, {n:hashes[n] for n in RECEIPT_FILES},
        texts=approval['notifications'],
        **{k:approval[k] for k in ('app_id','bot_id','chat_id')})


class CliProcess:
    """Official CLI embedding only; no plaintext credential fallback."""
    def __init__(self, path, expected_sha):
        self.path, self.expected_sha = no_symlinks(Path(path).absolute()), expected_sha
        if file_sha(self.path) != expected_sha:
            raise ContractError('approved CLI guard binary required')

    def __call__(self, control):
        self.check()
        env = {k:os.environ[k] for k in ('HOME','USER','LOGNAME','TMPDIR','LANG') if k in os.environ}
        env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', GOMAXPROCS='1', GOMEMLIMIT='128MiB',
            GODEBUG='http2client=0', LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
            LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
        remaining = (instant(control['expires_at'])-datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise ContractError('window expired before CLI launch')
        # Keep ordinary calls short; downloads still obey the approved deadline.
        timeout = DOWNLOAD_TIMEOUT_SECONDS if control.get('kind')=='download' and control['mode']=='read' else 45
        result = subprocess.run([str(self.path)], input=json.dumps(control), capture_output=True,
            text=True, env=env, timeout=min(timeout, remaining))
        self.check()
        # Wrapper emits only a tiny nonsecret summary. Never persist raw CLI stderr.
        if result.returncode != 0 or len(result.stdout)>4096:
            raise ContractError('bounded CLI call failed; no retry')
        value = json.loads(result.stdout)
        if value.get('ok') is not True or value.get('mode') != control['mode']:
            raise ContractError('CLI output contract rejected')
        return value

    def check(self):
        if file_sha(self.path) != self.expected_sha:
            raise ContractError('CLI guard changed')


class NotificationReadExperiment(Experiment):
    reference, limits = 'F05', LIMITS

    def __init__(self, f04_root, approval, *, runner, clock=None):
        self.f04_root = no_symlinks(Path(f04_root).absolute())
        if (approval.get('f04_root') != str(self.f04_root) or self.f04_root.name != 'f04-requests'
                or approval.get('automatic_retries') != 0 or approval.get('redirects') != 0
                or approval.get('unknown_lookup') != 0 or approval.get('history_list') is not False
                or approval.get('writes') != 0 or approval.get('scopes') != SCOPES
                or approval.get('publish_scope') != 'only_bound_account'
                or approval.get('published_version') != '0.1.1'
                or not isinstance(approval.get('cli_profile'), str) or not approval['cli_profile']):
            raise ContractError('exact dedicated read-only F05 approval required')
        self.receipts = known_f04(self.f04_root, approval)
        self.frozen = {str(self.f04_root/n):h for n,h in approval['f04_evidence_sha256'].items()}
        self.runner = runner
        super().__init__(self.f04_root.parent/'f05-requests', approval, clock=clock, sleep=lambda _:None)
        try:
            if any('intent' in e or 'setup_started' in e for e in self.events):
                raise ContractError('F05 already started; never reset its budget')
        except Exception:
            self.close()
            raise

    def check(self):
        super().check()
        if any(file_sha(Path(p)) != h for p,h in self.frozen.items()):
            raise ContractError('F04 evidence changed before F05 request')

    def request(self, *args, **kwargs):
        raise ContractError('F05 only permits the guarded official CLI read path')

    def control(self, mode, key='', kind='', path=''):
        return dict(mode=mode, root=str(self.root), profile=self.approval['cli_profile'],
            app_id=self.approval['app_id'], key=key, kind=kind, path=path,
            starts_at=self.approval['starts_at'], expires_at=self.approval['expires_at'], frozen=self.frozen)

    def http_events(self):
        p = self.root/'http.local.jsonl'
        return [json.loads(line) for line in no_symlinks(p).read_text().splitlines()] if p.exists() else []

    def call(self, kind, key, path):
        self.check()
        expected = self.paths()
        if expected.get(key) != (kind,path):
            raise ContractError('F05 only reads its bound bot, group, member and two message IDs')
        prerequisite = {'bot':'profile_verified', 'chat':'bot_verified', 'members':'chat_verified'}.get(key)
        if prerequisite and self.value(prerequisite) is not True:
            raise ContractError('ordered profile/application/group verification required')
        if key.startswith('notification-') and self.value('isolation_verified') is not True:
            raise ContractError('current isolation check required before notification reads')
        intent = 'command:'+key
        if any(e.get('intent') == intent for e in self.events):
            raise ContractError('CLI command already attempted')
        self.append(dict(intent=intent, kind='cli_command', path=path))
        try:
            summary = self.runner(self.control('read', key, kind, path))
            if summary.get('ok') is not True:
                raise ContractError('CLI did not report success')
            events = self.http_events()
            started = [e['key'] for e in events if e.get('type')=='intent']
            completed = [e['key'] for e in events if e.get('type')=='complete']
            if (any(e.get('type') not in ('intent','complete') for e in events)
                    or any(e.get('kind') not in self.limits for e in events)
                    or len(set(started)) != len(started) or len(set(completed)) != len(completed)
                    or set(started) != set(completed)):
                raise ContractError('physical HTTP attempt unresolved or stopped')
            intents = {e['key']:e for e in events if e['type']=='intent'}
            if any(e.get('kind')!=intents[e['key']]['kind'] or e.get('http_status')!=200
                   for e in events if e['type']=='complete'):
                raise ContractError('physical HTTP completion differs from intent')
            if any(sum(e.get('type')=='intent' and e.get('kind')==k for e in events)>v for k,v in self.limits.items()):
                raise ContractError('physical request budget exceeded')
            completion = [e for e in events if e.get('type')=='complete' and e.get('key')==key+':'+kind]
            raw = no_symlinks(self.root/(key+'.response.local')).read_bytes()
            if (len(completion)!=1 or completion[0].get('http_status')!=200
                    or completion[0].get('response_sha256')!=sha(raw)):
                raise ContractError('successful HTTP receipt hash required')
            value = json.loads(raw)
            if value.get('code') != 0:
                raise ContractError('successful API receipt required')
            self.append(dict(complete=intent, response_sha256=sha(raw)))
            return value
        except Exception as error:
            self.append(dict(stopped=intent, error_type=type(error).__name__))
            raise

    def paths(self):
        chat = PREFIX+'/im/v1/chats/'+self.approval['chat_id']
        return {'bot':('bot',PREFIX+'/bot/v3/info'), 'chat':('chat',chat),
            'members':('members',chat+'/members?member_id_type=open_id&page_size=100'),
            **{'notification-'+str(i):('message',PREFIX+'/im/v1/messages/'+r['message_id'])
               for i,r in enumerate(self.receipts)}}

    def run(self):
        try:
            self.verify_isolation()
            for i,receipt in enumerate(self.receipts):
                key = 'notification-'+str(i)
                detail = self.call('message',key,self.paths()[key][1]).get('data',{}).get('items')
                observed = observe_notification(receipt,detail,text=self.approval['notifications'][i],
                    **{k:self.approval[k] for k in ('app_id','bot_id','chat_id')})
                self.bind('observation:'+key, observed)
            self.check()
            return self.observations()
        except Exception as error:
            self.append(dict(stopped='orchestration',error_type=type(error).__name__))
            raise

    def verify_isolation(self):
        """Shared fixed profile/bot/private-group/sole-member preflight."""
        self.check()
        if any('setup_started' in e for e in self.events):
            raise ContractError('F05 already started; no repeated setup')
        self.append({'setup_started':True})
        try:
            summary = self.runner(self.control('profile'))
            if summary.get('ok') is not True:
                raise ContractError('dedicated CLI profile/application differs')
            self.bind('profile_verified',True)
            bot = self.call('bot','bot',self.paths()['bot'][1]).get('bot',{})
            if bot.get('open_id') != self.approval['bot_id']:
                raise ContractError('bound bot identity differs')
            self.bind('bot_verified',True)
            info = self.call('chat','chat',self.paths()['chat'][1]).get('data',{})
            if (info.get('chat_mode')!='group' or info.get('chat_type')!='private'
                    or info.get('external') is not False or str(info.get('user_count'))!='1'
                    or str(info.get('bot_count'))!='1' or administration(info)!=self.approval['administration']
                    or any(info.get(k)!=v for k,v in ISOLATION.items())):
                raise ContractError('dedicated group isolation differs')
            self.bind('chat_verified',True)
            members = self.call('members','members',self.paths()['members'][1]).get('data',{})
            items = members.get('items',[])
            if (members.get('has_more') is not False or members.get('trigger_security_conf_limit') is True
                    or len(items)!=1 or items[0].get('member_id')!=self.approval['account_id']
                    or items[0].get('member_id_type')!='open_id'):
                raise ContractError('complete sole authorized member required')
            self.bind('isolation_verified',True)
        except Exception as error:
            self.append(dict(stopped='orchestration',error_type=type(error).__name__))
            raise

    def observations(self):
        return {str(i):self.value('observation:notification-'+str(i)) for i in range(2)}
