"""Separately approved one-shot enrollment: three requests, no netdisk files.

Reuse existing private journal/fsync and wall-deadline implementation. The old
experiments and their consumed approvals are never modified or invoked.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .archive import instant
from .b01 import BaiduExperiment, deadline
from .baidu_credentials import ApplicationCredentials, application_identity
from .baidu_http import BaiduHttpsWire
from .baidu_oauth import OAuthIdentity, OAuthSession
from .baidu_protocol import HttpResponse
from .contracts import ContractError
from .f01 import encoded
from .runtime import no_symlinks

RUNTIME = '.dev-runtime/kb-v21/test-output/baidu-oauth-enrollment'
APPROVAL = '.dev-runtime/kb-v21/real-validation/baidu-oauth-enrollment.local.json'
LOCKS = '.dev-runtime/kb-v21/credentials/oauth-locks'
ORDER = ['oauth_token', 'uinfo', 'uinfo']
LIMITS = dict(oauth_token=1, oauth_refresh=0, uinfo=2)


class _ApprovedSession(OAuthSession):
    def __init__(self, experiment, identity, store):
        experiment.check()
        self._initialize(identity, store, experiment.wire, check=experiment.check,
            count=experiment.perform, clock=lambda: experiment.clock().timestamp())


class OAuthEnrollment:
    # Same owner-only append + file/directory fsync as existing Baidu experiments.
    append = BaiduExperiment.append

    def __init__(self, worktree, approval, *, checked_source, wire=None, clock=None):
        self.worktree = Path(worktree)
        self.approval = json.loads(encoded(approval))
        self.checked_source = checked_source
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events, self.inflight, self.stopped = [], None, False
        a = self.approval
        application_identity(a.get('app_id'), a.get('app_name'))
        fixed = dict(reference='BAUTH01', approved=True, runtime_relative=RUNTIME, limits=LIMITS,
            max_calls=3, scopes=['basic', 'netdisk'], token_storage='macos_login_keychain',
            persistent_use='authorized_windows_only', writes=['oauth_record_only'],
            automatic_retries=0, redirects=0, netdisk_file_operations=0,
            browser_traffic_in_budget=False)
        if any(encoded(a.get(k)) != encoded(v) for k, v in fixed.items()):
            raise ContractError('exact separate OAuth enrollment approval required')
        # Validate account without accepting an observed replacement at runtime.
        OAuthIdentity(a['app_id'], a['app_name'], a.get('account_ref'), '0'*64)
        if not 0 < (instant(a['expires_at'])-instant(a['starts_at'])).total_seconds() <= 7200:
            raise ContractError('OAuth enrollment needs a new bounded window')
        self.root = no_symlinks(self.worktree/RUNTIME)
        self.path = self.root/'journal.local.jsonl'
        self.wire = wire if wire is not None else BaiduHttpsWire(self.before_connect)
        if not isinstance(self.wire, BaiduHttpsWire) and getattr(self.wire, 'is_mock', False) is not True:
            raise ContractError('reviewed HTTPS or offline mock required')
        self.check()
        self.root.mkdir(mode=0o700, exist_ok=False)
        self.append(dict(started=self.clock().isoformat(), approval=a,
            validation_mode='offline_mock' if self.wire.is_mock else 'real_bounded'))

    def _check_access_state(self, *, inflight):
        if (self.stopped or self.inflight is not None and not inflight
                or not instant(self.approval['starts_at']) <= self.clock() < instant(self.approval['expires_at'])):
            raise ContractError('OAuth enrollment scope, source, evidence or window changed')

    def check(self, *, inflight=False):
        self._check_access_state(inflight=inflight)
        if self.checked_source() is not True:
            raise ContractError('OAuth enrollment scope, source, evidence or window changed')
        # Hashing the bound source/evidence may outlast the approved window.
        # Recheck after that work before permitting the next native/HTTP access.
        self._check_access_state(inflight=inflight)
        return True

    def before_connect(self, request):
        self.check(inflight=True)
        if self.inflight is None or self.inflight['kind'] != request.label:
            raise ContractError('OAuth HTTP requires matching durable intent')
        return True

    def perform(self, kind, operation):
        self.check()
        previous = [e['kind'] for e in self.events if 'intent' in e]
        if previous != ORDER[:len(previous)] or len(previous) >= len(ORDER) or kind != ORDER[len(previous)]:
            raise ContractError('OAuth request order or category budget exceeded')
        item = dict(intent=len(previous)+1, kind=kind, at=self.clock().isoformat())
        self.append(item)
        self.inflight = item
        try:
            with deadline(min(30, (instant(self.approval['expires_at'])-self.clock()).total_seconds())):
                result = operation()
            if (not isinstance(result, HttpResponse) or type(result.status) is not int
                    or not isinstance(result.body, bytes)):
                raise ContractError('OAuth physical response invalid')
            # No token, raw body, session field, response hash or URL in journal.
            self.append(dict(received=item['intent'], http_status=result.status, bytes=len(result.body)))
            self.inflight = None
            self.check()
            return result
        except BaseException as error:
            self.stopped = True
            self.append(dict(stopped=kind, error_type=type(error).__name__,
                             unfinished_intent=self.inflight is not None))
            raise

    def run(self, *, load_credentials, store_factory, obtain_code):
        result = dict(status='STOPPED', stage='credentials', netdisk_file_operations=0,
            automatic_refresh_real='NOT_RUN', unknown_recovery='NOT_RUN', end_to_end='NOT_RUN')
        try:
            self.check()
            app = load_credentials()
            self.check()
            if not isinstance(app, ApplicationCredentials):
                raise ContractError('OAuth application credential type invalid')
            identity = OAuthIdentity(self.approval['app_id'], self.approval['app_name'],
                self.approval['account_ref'], hashlib.sha256(app.app_key.encode()).hexdigest())
            result['stage'] = 'existing_record'
            store = store_factory(identity)
            with store.transaction():
                if store.read() is not None:
                    raise ContractError('OAuth enrollment record already exists; never overwrite')
            self.check()
            result['stage'] = 'initial_consent'
            code = obtain_code(app, self.approval)
            self.check()
            result['stage'] = 'exchange_and_save'
            token = _ApprovedSession(self, identity, store).access_token(credentials=lambda: app, code=code)
            code = None
            result['stage'] = 'reopen_and_reuse'
            reopened = store_factory(identity)
            reused = _ApprovedSession(self, identity, reopened).access_token()
            if reused != token:
                raise ContractError('OAuth reopened credential differs')
            self.check()
            result.update(status='OAUTH_STORED_AND_REUSED', stage='completed')
        except (ContractError, OSError, EOFError, KeyboardInterrupt) as error:
            self.stopped = True
            result['error_type'] = type(error).__name__
        finally:
            result['physical_requests'] = sum('intent' in e for e in self.events)
            self.append(dict(result=result, ended_at=self.clock().isoformat()))
        return result
