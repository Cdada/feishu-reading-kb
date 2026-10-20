"""Explicit one-shot B01 composition. Import and prepare never use a platform.

The CLI owns the private human approval and full Git source check. The default
DEV factories remain mock-only. Every HTTP is metered here, including bootstrap;
Archive also keeps its normal per-operation subset of the same attempts.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import threading

from .archive import Archive, instant
from .baidu import BaiduAdapter, ResponseValidationError
from .baidu_credentials import ApplicationCredentials, application_identity
from .baidu_http import BaiduHttpsWire
from .baidu_protocol import (AuthorizationCode, BaiduProtocolTransport, HttpResponse,
                             json_object, slice_contract, create_observation)
from .contracts import ContractError, ENTRY_SCHEMA
from .f01 import encoded, sha
from .f01_flow import TXT
from .intake import FeishuMockIntake, MockAttachment
from .registry import Registry
from .runtime import DEFAULTS, Runtime, no_symlinks
from .staging import Staging

RUNTIME = '.dev-runtime/kb-v21/test-output/baidu-b01'
APPROVAL = '.dev-runtime/kb-v21/real-validation/baidu-b01-approval.local.json'
LIMITS = dict(oauth_token=1, uinfo=2, mkdir=1, quota=1, list=1, precreate=1,
              slice=1, create=1, filemetas=2, download_meta=1, download=1)
ORDER = ['oauth_token', 'uinfo', 'mkdir', 'uinfo', 'quota', 'list', 'precreate',
         'slice', 'create', 'filemetas', 'download_meta', 'download', 'filemetas']


def fixture():
    return dict(name='fixture.txt', sha256=sha(TXT), size_bytes=len(TXT))


def prepare(worktree, *, root=None):
    return prepare_fixture(worktree, root or Path(worktree)/RUNTIME, TXT, 'B01')


def prepare_fixture(worktree, root, content, reference):
    """Shared local custody setup; callers bind their fixed synthetic bytes."""
    registry = Registry(Runtime(worktree, DEFAULTS, root))
    if not registry.rows('SELECT * FROM intake_entries'):
        FeishuMockIntake(registry, Staging(registry)).receive(dict(
            schema_version=ENTRY_SCHEMA, intake_key=reference+'-synthetic-local', entry_key='fixture.txt',
            received_filename='fixture.txt', expected={'size_bytes':len(content), 'sha256':sha(content)}),
            MockAttachment(content))
    return registry, binding(registry, content=content)


def binding(registry, *, content=TXT):
    rows = registry.rows('SELECT entry_id FROM intake_entries')
    if len(rows) != 1:
        raise ContractError('exactly one frozen synthetic intake required')
    entry = registry.entry(rows[0]['entry_id'])
    if (entry['received_filename'], entry['size_bytes'], entry['sha256']) != ('fixture.txt', len(content), sha(content)):
        raise ContractError('B01 local original differs from frozen fixture')
    if registry.receipt(entry['entry_id'])['source_staging_status'] != 'accepted':
        raise ContractError('B01 fixture is not in custody')
    with Staging(registry).input_binding(entry['entry_id']) as source:
        if source['stream'].read(len(content)+1) != content:
            raise ContractError('B01 original bytes changed')
    return {k:entry[k] for k in ('entry_id','asset_id','acceptance_id','sha256','size_bytes')}


@contextmanager
def deadline(seconds):
    """Main-thread POSIX wall limit includes DNS/TLS/headers/body; no retries."""
    if (threading.current_thread() is not threading.main_thread()
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0) or seconds <= 0):
        raise ContractError('exclusive bounded operator thread required')
    def expired(signum, frame):
        raise TimeoutError('B01 physical request deadline exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _Protocol(BaiduProtocolTransport):
    def __init__(self, experiment, credentials, code):
        experiment.check()
        self.experiment = experiment
        self.is_mock = experiment.wire.is_mock
        a = experiment.approval
        self._initialize(experiment.wire, None, a['root'], a['target'],
            authorization=AuthorizationCode(credentials.app_key, credentials.secret_key, code),
            authorized_write=True, bootstrap=True)

    def _exchange(self, count, *args, **kwargs):
        # Bootstrap uses an immediate callback; Archive supplies its own durable
        # operation counter. Both enter the one authoritative global meter.
        def bounded(label, perform):
            return count(label, lambda: self.experiment.perform(label, perform))
        return super()._exchange(bounded, *args, **kwargs)


class _Adapter(BaiduAdapter):
    def __init__(self, transport):
        if not isinstance(transport, _Protocol) or transport.account_ref is None or transport._directory is None:
            raise ContractError('approved B01 identity and directory required')
        transport.experiment.check()
        self._initialize(transport, transport.account_ref, transport.root)
        self.validation_mode = 'offline_mock' if transport.is_mock else 'real_bounded'


class BaiduExperiment:
    # B01 defaults stay fixed; B03 reuses the meter/Archive path with distinct
    # source-bound synthetic bytes and additional predecessor checks.
    reference = 'B01'
    content = TXT
    first_account = 'operator_confirms_observed_uk'

    def __init__(self, registry, approval, *, checked_source, wire=None, clock=None):
        self.registry = registry
        self.approval = json.loads(encoded(approval))
        self.checked_source = checked_source
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events, self.inflight, self.stopped = [], None, False
        a = self.approval
        application_identity(a.get('app_id'), a.get('app_name'))
        fixed = dict(reference=self.reference, synthetic_only=True, limits=LIMITS, max_calls=sum(LIMITS.values()),
            automatic_retries=0, redirects=0, unknown_lookup=0, token_storage='memory_only',
            first_account=self.first_account, scopes=['basic','netdisk'],
            writes=['mkdir_once','upload_once'], fixture=dict(name='fixture.txt',sha256=sha(self.content),size_bytes=len(self.content)))
        if (a.get('approved') is not True or any(encoded(a.get(k)) != encoded(v) for k,v in fixed.items())
                or a.get('root') != '/apps/'+a['app_name']+'/KB-V21-'+self.reference
                or a.get('target') != a['root']+'/fixture.txt'
                or a.get('runtime_relative') != str(registry.runtime.root.relative_to(registry.runtime.worktree))
                or a.get('fixture_binding') != binding(registry, content=self.content)):
            raise ContractError('exact B01 scope and frozen fixture approval required')
        if not 0 < (instant(a['expires_at'])-instant(a['starts_at'])).total_seconds() <= 7200:
            raise ContractError('one B01 window of at most 120 minutes required')
        self.wire = wire if wire is not None else BaiduHttpsWire(self.before_connect)
        if (getattr(self.wire, 'is_mock', None) is not True
                and not isinstance(self.wire, BaiduHttpsWire)):
            raise ContractError('reviewed HTTPS wire or explicit offline fixture required')
        self.root = no_symlinks(self.request_root)
        self.path = self.root/'journal.local.jsonl'
        self.check()
        # Exclusive persistent claim, including failures before the first HTTP.
        # Never delete it or accept a new approval to restart the same B01.
        self.root.mkdir(mode=0o700, exist_ok=False)
        self.append({'started': self.clock().isoformat(), 'approval': a,
                     'validation_mode':'offline_mock' if self.wire.is_mock else 'real_bounded'})
        self.protocol, self.archive, self.session, self.operation_id = None, None, None, None

    @property
    def request_root(self):
        return self.registry.runtime.root/(self.reference.lower()+'-requests')

    def append(self, item):
        fd = os.open(no_symlinks(self.path), os.O_CREAT|os.O_APPEND|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(encoded(item)+'\n')
            output.flush()
            os.fsync(output.fileno())
        for directory in (self.root, self.root.parent):
            fd = os.open(directory, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try: os.fsync(fd)
            finally: os.close(fd)
        self.events.append(item)

    def check(self, *, inflight=False):
        if (self.stopped or not instant(self.approval['starts_at']) <= self.clock() < instant(self.approval['expires_at'])
                or self.inflight is not None and not inflight):
            raise ContractError('B01 window stopped, expired, or has an unfinished request')
        if self.checked_source() is not True:
            raise ContractError('approved source or private approval changed')
        self.registry.runtime.gate()
        if binding(self.registry, content=self.content) != self.approval['fixture_binding']:
            raise ContractError('frozen B01 fixture changed')
        return True

    def before_connect(self, request):
        self.check(inflight=True)
        if self.inflight is None or self.inflight['kind'] != request.label:
            raise ContractError('HTTP has no matching durable B01 intent')
        return True

    def perform(self, kind, perform):
        self.check()
        intents = [e for e in self.events if 'intent' in e]
        kinds = [e['kind'] for e in intents]
        allowed = ORDER[:]
        if kind == 'create' and kinds == ORDER[:7]:
            # Provider may request no slice for a reusable block; return_type=2
            # still stops in BaiduAdapter and does not reach this branch.
            allowed.remove('slice')
        elif 'create' in kinds and 'slice' not in kinds:
            allowed.remove('slice')
        if (kind not in LIMITS or kinds.count(kind) >= LIMITS[kind]
                or len(kinds) >= len(allowed) or kinds != allowed[:len(kinds)]
                or kind != allowed[len(kinds)]):
            raise ContractError('B01 request order or category budget exceeded')
        item = dict(intent=len(intents)+1, kind=kind, at=self.clock().isoformat())
        self.append(item)
        self.inflight = item
        evidence = None
        try:
            remaining = (instant(self.approval['expires_at'])-self.clock()).total_seconds()
            with deadline(min(30, remaining)):
                result = perform()
            if (not isinstance(result, HttpResponse) or not isinstance(result.body, bytes)
                    or type(result.status) is not int):
                raise ContractError('invalid B01 HTTP response')
            evidence = dict(received=item['intent'], http_status=result.status, bytes=len(result.body))
            if kind != 'oauth_token':
                evidence['response_sha256'] = sha(result.body)
                if kind != 'download' and result.status == 200:
                    value = json_object(result.body)
                    if type(value.get('errno')) is int:
                        evidence['api_errno'] = value['errno']
                    if kind == 'slice':
                        evidence['slice_contract'] = slice_contract(value, hashlib.md5(self.content).hexdigest())
                    if kind == 'create':
                        evidence['create_observation'] = create_observation(
                            value, self.approval['target'], len(self.content))
            # Neither raw OAuth, signed links, upload/session IDs nor provider
            # error text enters the journal. Typed identity receipts follow.
            self.append(evidence)
            self.inflight = None
            self.check()
            return result
        except BaseException as error:
            self.stopped = True
            if evidence is not None and self.inflight is not None:
                # Even malformed JSON has arrived. Preserve its bounded receipt
                # before stopping; do not mistake parse failure for no reply.
                self.append(evidence)
                self.inflight = None
            self.append({'stopped':item['intent'], 'error_type':type(error).__name__})
            if evidence is not None and isinstance(error, ContractError):
                raise ResponseValidationError('received HTTP response failed validation') from None
            raise

    def run(self, *, load_credentials, obtain_code, confirm_account):
        if self.stopped:
            raise ContractError('B01 already stopped; no restart')
        outcome = dict(status='STOPPED', started_at=self.clock().isoformat(),
            validation_mode='offline_mock' if self.wire.is_mock else 'real_bounded',
            real_user_entry='NOT_RUN', unknown_recovery='NOT_RUN', end_to_end='NOT_RUN')
        credentials = code = None
        stage = 'initial_check'
        try:
            self.check()
            stage = 'load_credentials'
            credentials = load_credentials(self.approval['app_id'], self.approval['app_name'], self.check)
            if not isinstance(credentials, ApplicationCredentials):
                raise ContractError('exact existing application credentials required')
            stage = 'before_oauth_check'
            self.check()
            stage = 'obtain_oauth_code'
            code = obtain_code(credentials, self.approval)
            stage = 'validate_oauth_code'
            if (not isinstance(code, str) or not 8 <= len(code) <= 512 or not code.isascii()
                    or any(c.isspace() or not c.isprintable() for c in code)):
                raise ContractError('invalid authorization code; no automatic repeat')
            stage = 'after_oauth_check'
            self.check()
            stage = 'initialize_protocol'
            self.protocol = _Protocol(self, credentials, code)
            credentials = code = None
            immediate = lambda label, call: call()
            stage = 'identify_account'
            account = self.protocol.identify_account(immediate)
            stage = 'confirm_account'
            expected = confirm_account(account)
            self.check()
            self.protocol.bind_account(expected)
            self.append({'binding':'account_ref', 'value':account})
            stage = 'create_directory'
            directory = self.protocol.create_test_directory(immediate)
            self.append({'binding':'directory', 'value':directory})
            stage = 'prepare_archive'
            self.archive = Archive(self.registry, _Adapter(self.protocol))
            asset_id = self.archive.register_raw(self.approval['fixture_binding']['entry_id'])
            self.operation_id = self.archive.plan(asset_id, target=self.approval['target'])
            self.append({'binding':'archive_operation', 'value':self.operation_id})
            self.session = self.archive.session(self.reference, [self.operation_id], self.approval['expires_at'], 10,
                starts_at=self.approval['starts_at'], physical_limits={
                    k:v for k,v in dict(LIMITS, uinfo=1).items() if k not in ('oauth_token','mkdir')})
            stage = 'execute_archive'
            result = self.session.execute(self.operation_id)
            stage = 'final_verification'
            self.check()
            if result['verification_level'] != 'content_sha256_verified' or result['integrity_hold']:
                raise ContractError('B01 requires independently downloaded SHA with no integrity hold')
            outcome.update(status='OFFLINE_'+self.reference+'_VERIFIED' if self.wire.is_mock else 'BOUNDED_BAIDU_UPLOAD_VERIFIED',
                           archive_result=result)
        except (Exception, KeyboardInterrupt) as error:
            outcome['error_type'] = type(error).__name__
            outcome['error_stage'] = stage
            self.append({'stopped':'orchestration', 'error_type':type(error).__name__, 'error_stage':stage})
        finally:
            credentials = code = None
            if self.protocol is not None:
                self.protocol._token = self.protocol._authorization = None
            self.stopped = True
            outcome.update(ended_at=self.clock().isoformat(),
                physical_attempts={k:sum(e.get('kind')==k and 'intent' in e for e in self.events) for k in LIMITS},
                unfinished_attempts=len({e['intent'] for e in self.events if 'intent' in e} -
                                        {e['received'] for e in self.events if 'received' in e}))
            self.append({'result':outcome})
        return outcome
