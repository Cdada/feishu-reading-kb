"""One-shot read-only observation of the frozen B01 unknown, never a retry.

No old registry is opened for writing. Even a matching final file is an
observation; this experiment cannot resolve the old upload or release a hold.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3

from .archive import instant
from .b01 import BaiduExperiment, LIMITS as B01_LIMITS, deadline, fixture
from .baidu import fs_id, remote_path, RemoteFailure
from .baidu_credentials import ApplicationCredentials, application_identity
from .baidu_http import BaiduHttpsWire
from .baidu_protocol import AuthorizationCode, BaiduProtocolTransport, HttpResponse, PAN, FILE, json_object
from .contracts import ContractError
from .f01 import encoded, sha
from .f01_flow import TXT
from .runtime import DEFAULTS, Runtime, no_symlinks

RUNTIME = '.dev-runtime/kb-v21/test-output/baidu-b02-observation'
APPROVAL = '.dev-runtime/kb-v21/real-validation/baidu-b02-approval.local.json'
ORDER = ['oauth_token', 'uinfo', 'list', 'filemetas', 'download_meta', 'download', 'filemetas']
LIMITS = dict(oauth_token=1, uinfo=1, list=1, filemetas=2, download_meta=1, download=1)
PRIOR_FILES = {'approval', 'journal', 'database', 'original', 'current_original', 'current_journal', 'current_database'}


def frozen_files(worktree, bindings):
    if not isinstance(bindings, dict) or set(bindings) != PRIOR_FILES:
        raise ContractError('exact B01 frozen and current evidence set required')
    paths = {}
    for kind, item in bindings.items():
        if not isinstance(item, dict) or set(item) != {'relative_path', 'sha256'}:
            raise ContractError('invalid prior evidence binding')
        relative = Path(item['relative_path'])
        if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] not in (('.dev-runtime',), ('tmp',)):
            raise ContractError('prior evidence must remain in private DEV storage')
        path = no_symlinks(worktree/relative)
        if path.stat().st_size > 16*1024*1024:
            raise ContractError('prior evidence exceeds bounded size')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                raise ContractError('frozen B01 evidence changed')
        paths[kind] = path
    if paths['database'].suffix != '.sqlite3' or paths['database'] == worktree/'.dev-runtime/kb-v21/test-output/baidu-b01/cloud-assets/registry.sqlite3':
        raise ContractError('a separate consistent B01 database snapshot is required')
    if paths['database'] == paths['current_database']:
        raise ContractError('frozen database must be separate from the live predecessor')
    for kind in ('database', 'current_database'):
        for suffix in ('-wal', '-journal'):
            sidecar = no_symlinks(str(paths[kind])+suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ContractError('uncheckpointed predecessor database must not be observed')
    return paths


def prior_binding(worktree, approval):
    """Validate provenance and the exact existing unknown; never infer new IDs."""
    paths = frozen_files(worktree, approval['prior_files'])
    old = json_object(paths['approval'].read_bytes())
    events = [json_object(line) for line in paths['journal'].read_bytes().splitlines()]
    if (not events or events[0].get('approval') != old or old.get('approved') is not True
            or old.get('reference') != 'B01' or old.get('fixture') != fixture()
            or old.get('source_commit') != approval['prior_commit']
            or any(old.get(k) != approval[k] for k in ('app_id', 'app_name', 'root', 'target'))):
        raise ContractError('B01 approval provenance or identity differs')
    def bound(name):
        values = [e['value'] for e in events if e.get('binding') == name]
        if len(values) != 1:
            raise ContractError('one original B01 binding required')
        return values[0]
    source = bound('approved_source')
    if source != dict(commit=old['source_commit'], source_sha256=old['source_snapshot']['sha256'],
                      approval_sha256=sha(paths['approval'].read_bytes())):
        raise ContractError('B01 source and approved bytes are not bound')
    intents = [e for e in events if 'intent' in e]
    received = [e for e in events if 'received' in e]
    expected = ['oauth_token','uinfo','mkdir','uinfo','quota','list','precreate','slice']
    results = [e['result'] for e in events if 'result' in e]
    if ([e.get('kind') for e in intents] != expected
            or [e.get('intent') for e in intents] != list(range(1,9))
            or [e.get('received') for e in received] != list(range(1,9))
            or len(results) != 1 or results[0].get('status') != 'STOPPED'
            or results[0].get('unfinished_attempts') != 0
            or results[0].get('physical_attempts') != {k:expected.count(k) for k in B01_LIMITS}):
        raise ContractError('B02 requires the exact stopped B01 slice history')
    account = fs_id(bound('account_ref'))
    if account != fs_id(approval['account_ref']) or bound('directory').get('path') != approval['root']:
        raise ContractError('approved account or directory differs from B01')
    if any(paths[k].read_bytes() != TXT for k in ('original', 'current_original')):
        raise ContractError('frozen or current original differs from synthetic fixture')
    if paths['current_journal'].read_bytes() != paths['journal'].read_bytes():
        raise ContractError('current B01 history differs from frozen history')
    # immutable=1 is safe only for the separately frozen consistent copy above;
    # it prevents SQLite sidecar creation and never changes the original DB.
    with closing(sqlite3.connect(paths['database'].as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        op = db.execute('SELECT * FROM archive_operations WHERE operation_id=?', (bound('archive_operation'),)).fetchone()
        plan = json_object(op['plan_json']) if op is not None else {}
        identity = old['fixture_binding']
        row = db.execute('''SELECT r.status,f.custody_health,f.cleanup_hold,a.sha256,a.size_bytes
            FROM staging_receipts r JOIN staging_files f USING(asset_id) JOIN assets a USING(asset_id)
            WHERE r.entry_id=? AND r.asset_id=? AND r.acceptance_id=?''',
            tuple(identity[k] for k in ('entry_id','asset_id','acceptance_id'))).fetchone()
        loc = db.execute('SELECT * FROM asset_locations WHERE asset_id=?', (identity['asset_id'],)).fetchone()
        if (op is None or plan.get('asset_id') != identity['asset_id'] or op['state'] != 'unknown' or plan.get('operation') != 'upload'
                or row is None or tuple(row) != ('accepted','healthy',1,sha(TXT),len(TXT))
                or loc is None or loc['integrity_hold'] != 1 or loc['verification_level'] != 'none'
                or loc['planned_path'] != approval['target'] or loc['fs_id'] is not None):
            raise ContractError('B01 frozen registry no longer proves the protected unknown')
        local = db.execute('SELECT relative_path FROM staging_files WHERE asset_id=?', (identity['asset_id'],)).fetchone()[0]
    previous_runtime = no_symlinks(worktree/old['runtime_relative'])
    expected_current = dict(current_original=previous_runtime/local,
        current_database=previous_runtime/'cloud-assets/registry.sqlite3',
        current_journal=previous_runtime/'b01-requests/journal.local.jsonl')
    if any(paths[k] != p for k,p in expected_current.items()):
        raise ContractError('current evidence paths must identify the original B01 runtime')
    return dict(account_ref=account, operation_id=bound('archive_operation'), fixture_binding=identity)


class _ReadProtocol(BaiduProtocolTransport):
    def __init__(self, experiment, credentials, code):
        experiment.check()
        a = experiment.approval
        self.is_mock = experiment.wire.is_mock
        self._initialize(experiment.wire, a['account_ref'], a['root'], a['target'],
            authorization=AuthorizationCode(credentials.app_key, credentials.secret_key, code), authorized_write=False)

    def candidate(self, count):
        self._authenticate(count)
        user = self._json(count, 'uinfo', 'GET', PAN, '/rest/2.0/xpan/nas', [('method','uinfo')])
        if fs_id(user.get('uk')) != self.account_ref:
            raise RemoteFailure('authenticated account differs from original B01')
        value = self._json(count, 'list', 'GET', PAN, FILE,
            [('method','list'),('dir',self.root),('start',0),('limit',100),('order','name'),('desc',0)])
        rows = value.get('list')
        if not isinstance(rows, list) or len(rows) >= 100 or value.get('has_more', False) not in (False, 0):
            raise RemoteFailure('complete single directory page required; no pagination')
        candidates, identifiers, names = [], set(), set()
        for row in rows:
            if not isinstance(row, dict):
                raise RemoteFailure('invalid directory candidate')
            path, identifier = remote_path(row.get('path'), self.root), fs_id(row.get('fs_id'))
            if str(PurePosixPath(path).parent) != self.root or identifier in identifiers or path in names:
                raise RemoteFailure('ambiguous or out-of-scope directory candidates')
            identifiers.add(identifier); names.add(path)
            if path == self.target:
                if type(row.get('size')) is not int or row['size'] != len(TXT) or type(row.get('isdir')) is not int or row['isdir'] != 0:
                    raise RemoteFailure('target candidate size or type differs')
                candidates.append(identifier)
        if not candidates:
            return None
        identifier = candidates[0]
        # Candidate provenance is a complete exact-path listing, NOT a create
        # receipt. The inherited detail/download code checks this frozen pair.
        self._objects[identifier] = (self.target, len(TXT))
        return identifier

    def compose(self, method, payload, count):
        if method not in ('filemetas','download'):
            raise ContractError('B02 only permits bound candidate detail and download')
        return super().compose(method, payload, count)


class BaiduObservation:
    append = BaiduExperiment.append  # Existing fsync journal, no new simulator.

    def __init__(self, worktree, approval, *, checked_source, wire=None, clock=None, root=None):
        self.approval = json.loads(encoded(approval))
        self.checked_source, self.clock = checked_source, clock or (lambda: datetime.now(timezone.utc))
        self.runtime = Runtime(worktree, DEFAULTS, root or Path(worktree)/RUNTIME)
        self.events, self.inflight, self.stopped = [], None, False
        a = self.approval
        application_identity(a.get('app_id'), a.get('app_name'))
        fixed = dict(reference='B02', synthetic_only=True, limits=LIMITS, max_calls=7,
            automatic_retries=0, redirects=0, writes=[], token_storage='memory_only', scopes=['basic','netdisk'],
            fixture=fixture(), purpose='observe_existing_unknown_without_resolving_it')
        if (a.get('approved') is not True or any(encoded(a.get(k))!=encoded(v) for k,v in fixed.items())
                or a.get('root')!='/apps/'+a['app_name']+'/KB-V21-B01' or a.get('target')!=a['root']+'/fixture.txt'
                or a.get('runtime_relative')!=str(self.runtime.root.relative_to(self.runtime.worktree))
                or not 0 < (instant(a['expires_at'])-instant(a['starts_at'])).total_seconds() <= 7200):
            raise ContractError('exact independent B02 scope and new window required')
        self.prior = prior_binding(self.runtime.worktree, a)
        self.wire = wire if wire is not None else BaiduHttpsWire(self.before_connect)
        if getattr(self.wire,'is_mock',None) is not True and not isinstance(self.wire,BaiduHttpsWire):
            raise ContractError('reviewed bounded HTTPS or offline fixture required')
        self.root = no_symlinks(self.runtime.root/'b02-requests')
        self.path = self.root/'journal.local.jsonl'
        self.check()
        self.runtime.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.mkdir(mode=0o700, exist_ok=False)
        self.append(dict(started=self.clock().isoformat(), approval=a))

    def check(self, *, inflight=False):
        if (self.stopped or self.inflight is not None and not inflight
                or not instant(self.approval['starts_at']) <= self.clock() < instant(self.approval['expires_at'])
                or self.checked_source() is not True):
            raise ContractError('B02 stopped, unbound, expired or has an unfinished request')
        self.runtime.gate()
        frozen_files(self.runtime.worktree, self.approval['prior_files'])
        return True

    def before_connect(self, request):
        self.check(inflight=True)
        if self.inflight is None or self.inflight['kind'] != request.label or request.label not in LIMITS:
            raise ContractError('B02 HTTP lacks a read-only durable intent')
        return True

    def perform(self, kind, call):
        self.check()
        kinds = [e['kind'] for e in self.events if 'intent' in e]
        if len(kinds)>=len(ORDER) or kinds != ORDER[:len(kinds)] or kind != ORDER[len(kinds)]:
            raise ContractError('B02 fixed order or category budget exceeded')
        item = dict(intent=len(kinds)+1,kind=kind,at=self.clock().isoformat())
        self.append(item); self.inflight=item
        try:
            with deadline(min(30,(instant(self.approval['expires_at'])-self.clock()).total_seconds())):
                response = call()
            if not isinstance(response,HttpResponse) or type(response.status) is not int or not isinstance(response.body,bytes):
                raise ContractError('invalid B02 HTTP response')
            event = dict(received=item['intent'],http_status=response.status,bytes=len(response.body))
            if kind!='oauth_token':
                event['response_sha256']=sha(response.body)
                if kind!='download' and response.status==200:
                    value=json_object(response.body)
                    if type(value.get('errno')) is int: event['api_errno']=value['errno']
            self.append(event); self.inflight=None
            self.check()
            return response
        except BaseException as error:
            self.stopped=True
            self.append(dict(stopped=item['intent'],error_type=type(error).__name__))
            raise

    def run(self, *, load_credentials, obtain_code):
        if self.stopped: raise ContractError('B02 cannot restart')
        result=dict(status='STOPPED',unknown_recovery='NOT_RUN',old_archive_state='unknown',
            end_to_end='NOT_RUN',validation_mode='offline_mock' if self.wire.is_mock else 'real_bounded')
        credentials=code=protocol=None
        try:
            self.check()
            credentials=load_credentials(self.approval['app_id'],self.approval['app_name'],self.check)
            if not isinstance(credentials,ApplicationCredentials): raise ContractError('bound existing application required')
            self.check(); code=obtain_code(credentials,self.approval); self.check()
            if not isinstance(code,str) or not 8<=len(code)<=512 or not code.isascii() or any(c.isspace() or not c.isprintable() for c in code):
                raise ContractError('invalid authorization code; no retry')
            protocol=_ReadProtocol(self,credentials,code); credentials=code=None
            identifier=protocol.candidate(self.perform)
            if identifier is None:
                result['status']='NO_FINAL_FILE_OBSERVED_UNKNOWN_REMAINS'
            else:
                payload=dict(fsids=json.dumps([int(identifier)]),dlink=0)
                protocol.compose('filemetas',payload,self.perform)
                stream=protocol.compose('download',{'fs_id':identifier},self.perform)['stream']
                with stream:
                    data=stream.read(len(TXT)+1)
                    observed_sha=sha(data)
                    if data!=TXT or observed_sha!=self.approval['fixture']['sha256']:
                        raise RemoteFailure('candidate bytes differ from original SHA')
                protocol.compose('filemetas',payload,self.perform)
                result.update(status='CANDIDATE_BYTES_VERIFIED_UNKNOWN_REMAINS',candidate_fs_id=identifier,
                              observed_sha256=observed_sha,observed_bytes=len(data))
            self.check()  # Source/approval/current original/old evidence before success.
        except (Exception,KeyboardInterrupt) as error:
            result.update(status='STOPPED',error_type=type(error).__name__)
        finally:
            credentials=code=None
            if protocol is not None: protocol._token=protocol._authorization=None
            self.stopped=True
            result.update(ended_at=self.clock().isoformat(),
                physical_attempts={k:sum(e.get('kind')==k and 'intent' in e for e in self.events) for k in LIMITS},
                unfinished_attempts=len({e['intent'] for e in self.events if 'intent' in e}-
                                        {e['received'] for e in self.events if 'received' in e}))
            self.append({'result':result})
        return result
