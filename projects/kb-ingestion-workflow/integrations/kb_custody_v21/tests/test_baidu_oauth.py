"""Synthetic OAuth/store tests, no real Keychain, account or HTTP calls."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from kb_custody_v21.baidu_credentials import ApplicationCredentials
from kb_custody_v21.baidu_http import BaiduHttpsWire
from kb_custody_v21.baidu_oauth import OAuthIdentity, OAuthSession, encode, parse_record
from kb_custody_v21.baidu_protocol import HttpResponse
from kb_custody_v21.contracts import ContractError

APP = ApplicationCredentials('synthetic-app-key', 'synthetic-secret-key')
IDENTITY = OAuthIdentity('1234567', 'synthetic', '12345', hashlib.sha256(APP.app_key.encode()).hexdigest())
NOW = 1800000000


def ready(**changes):
    result = dict(schema_version=1, identity=asdict(IDENTITY), state='ready',
        access_token='synthetic-access-old', refresh_token='synthetic-refresh-old',
        issued_at=NOW-4000, expires_at=NOW+3600)
    result.update(changes)
    return encode(result)


def grant(**changes):
    result = dict(access_token='synthetic-access-new', refresh_token='synthetic-refresh-new',
                  scope='basic netdisk', expires_in=3600)
    result.update(changes)
    return HttpResponse(200, encode(result))


def account(**changes):
    result = dict(errno=0, uk=12345)
    result.update(changes)
    return HttpResponse(200, encode(result))


class Store:
    is_mock = True
    identity = IDENTITY

    def __init__(self, data=None):
        self.data = data
        self.writes = []
        self.active = False

    @contextmanager
    def transaction(self):
        if self.active:
            raise ContractError('synthetic concurrent operation')
        self.active = True
        try:
            yield self
        finally:
            self.active = False

    def read(self):
        return self.data

    def compare_and_swap(self, old, new):
        if self.data != old:
            raise ContractError('synthetic compare failure')
        parse_record(new, self.identity)
        self.data = new
        self.writes.append(new)


class OAuthTests(unittest.TestCase):
    def setup_session(self, data=None, responses=None, **kwargs):
        self.store = Store(data)
        self.wire = Mock(is_mock=True)
        self.wire.request.side_effect = responses if responses is not None else [grant(), account()]
        self.counts = []
        def count(label, operation):
            self.counts.append(label)
            return operation()
        self.check = Mock(return_value=True)
        self.credentials = Mock(return_value=APP)
        self.options = dict(check=self.check, count=count, clock=lambda: NOW)
        self.options.update(kwargs)
        return OAuthSession(IDENTITY, self.store, self.wire, **self.options)

    def test_first_grant_then_new_session_reuses_without_code_or_application_secret(self):
        session = self.setup_session()
        self.assertEqual(session.access_token(credentials=self.credentials, code='synthetic-code'), 'synthetic-access-new')
        self.assertEqual(self.counts, ['oauth_token', 'uinfo'])
        self.assertEqual(json.loads(self.store.writes[0])['state'], 'enrollment_pending')
        self.wire.request.side_effect = [account()]
        reopened = OAuthSession(IDENTITY, self.store, self.wire, **self.options)
        self.assertEqual(reopened.access_token(), 'synthetic-access-new')
        self.credentials.assert_called_once()
        self.assertEqual(self.counts, ['oauth_token', 'uinfo', 'uinfo'])
        self.assertEqual(len(self.store.writes), 2)
        for call in self.wire.request.call_args_list:
            BaiduHttpsWire._validate(call.args[0])
            self.assertNotIn('synthetic-', repr(call.args[0]))

    def test_expired_grant_refreshes_once_rotates_and_rechecks_account(self):
        session = self.setup_session(ready(expires_at=NOW-1))
        token = session.access_token(credentials=self.credentials)
        self.assertEqual(token, 'synthetic-access-new')
        self.assertEqual(self.counts, ['oauth_refresh', 'uinfo'])
        req = self.wire.request.call_args_list[0].args[0]
        BaiduHttpsWire._validate(req)
        query = parse_qs(urlsplit(req.target).query)
        self.assertEqual(query['refresh_token'], ['synthetic-refresh-old'])
        self.assertNotIn('code', query)
        self.assertNotIn(b'synthetic-refresh-old', self.store.writes[0])
        self.assertEqual(json.loads(self.store.writes[0])['state'], 'refresh_pending')
        self.assertEqual(parse_record(self.store.data, IDENTITY)['refresh_token'], 'synthetic-refresh-new')

    def test_pending_survives_new_session_without_request_or_credential_read(self):
        for state in ('enrollment_pending', 'refresh_pending'):
            with self.subTest(state=state):
                session = self.setup_session(encode(dict(schema_version=1, identity=asdict(IDENTITY), state=state)))
                with self.assertRaisesRegex(ContractError, 'unresolved'):
                    session.access_token(credentials=self.credentials, code='synthetic-new-code')
                self.assertEqual(self.counts, [])
                self.credentials.assert_not_called()
                self.assertEqual(self.store.writes, [])

    def test_http_failure_and_unknown_stop_without_retry_and_preserve_pending(self):
        failures = [OSError('synthetic-secret-echo'), HttpResponse(302, b''), HttpResponse(500, b''),
            HttpResponse(200, b'{'), HttpResponse(200, b'{"expires_in":' + b'9'*5000 + b'}'),
            HttpResponse(200, encode(dict(error='invalid_grant', error_description='synthetic-secret-echo')))]
        for response in failures:
            with self.subTest(kind=type(response).__name__):
                session = self.setup_session(ready(expires_at=NOW), [response])
                with self.assertRaises(ContractError) as error:
                    session.access_token(credentials=self.credentials)
                self.assertNotIn('synthetic-secret-echo', str(error.exception))
                self.assertEqual(self.counts, ['oauth_refresh'])
                self.assertEqual(json.loads(self.store.data)['state'], 'refresh_pending')
                reopened = OAuthSession(IDENTITY, self.store, self.wire, **self.options)
                with self.assertRaises(ContractError):
                    reopened.access_token(credentials=self.credentials)
                self.assertEqual(self.counts, ['oauth_refresh'])

    def test_token_scope_lifetime_and_account_mismatch_never_commit_ready(self):
        responses = [[grant(scope='basic netdisk extra')], [grant(scope='basic netdisk basic')],
            [grant(expires_in=True)], [grant(expires_in=30)], [grant(refresh_token=None)],
            [grant(), account(uk=54321)], [grant(), account(errno=True)], [grant(), account(uk=1.2)],
            [grant(), account(uk='9'*5000)]]
        for values in responses:
            with self.subTest(responses=len(values)):
                session = self.setup_session(responses=values)
                with self.assertRaises(ContractError):
                    session.access_token(credentials=self.credentials, code='synthetic-code')
                self.assertEqual(json.loads(self.store.data)['state'], 'enrollment_pending')
                self.assertEqual(len(self.counts), len(values))

    def test_application_mismatch_and_scope_denial_make_no_request_or_write(self):
        session = self.setup_session()
        self.credentials.return_value = ApplicationCredentials('synthetic-other-key', APP.secret_key)
        with self.assertRaisesRegex(ContractError, 'credential differs'):
            session.access_token(credentials=self.credentials, code='synthetic-code')
        self.assertEqual(self.counts, [])
        self.assertEqual(self.store.writes, [])
        session = self.setup_session()
        def credential_wait():
            self.check.return_value = False
            return APP
        self.credentials.side_effect = credential_wait
        with self.assertRaises(ContractError):
            session.access_token(credentials=self.credentials, code='synthetic-code')
        self.assertEqual(self.counts, [])
        self.assertEqual(self.store.writes, [])

    def test_final_source_change_after_account_preserves_pending(self):
        session = self.setup_session()
        def wire(request):
            if request.label == 'oauth_token':
                return grant()
            self.check.return_value = False
            return account()
        self.wire.request.side_effect = wire
        with self.assertRaises(ContractError):
            session.access_token(credentials=self.credentials, code='synthetic-code')
        self.assertEqual(self.counts, ['oauth_token', 'uinfo'])
        self.assertEqual(json.loads(self.store.data)['state'], 'enrollment_pending')

    def test_storage_failure_before_exchange_or_after_response_has_no_extra_request(self):
        for failed_write in (1, 2):
            session = self.setup_session()
            original = self.store.compare_and_swap
            calls = []
            def save(old, new):
                calls.append(1)
                if len(calls) == failed_write:
                    raise ContractError('synthetic storage failure')
                original(old, new)
            self.store.compare_and_swap = save
            with self.assertRaisesRegex(ContractError, 'storage failure'):
                session.access_token(credentials=self.credentials, code='synthetic-code')
            self.assertEqual(len(self.counts), 0 if failed_write == 1 else 2)
            if failed_write == 2:
                self.assertEqual(json.loads(self.store.data)['state'], 'enrollment_pending')

    def test_cached_token_expiry_during_query_and_clock_rollback_stop_without_refresh(self):
        times = [NOW]
        session = self.setup_session(ready(), [account()], clock=lambda: times[0])
        def wire(request):
            times[0] = NOW + 3600
            return account()
        self.wire.request.side_effect = wire
        with self.assertRaisesRegex(ContractError, 'expired'):
            session.access_token()
        self.assertEqual(self.counts, ['uinfo'])
        self.assertEqual(self.store.writes, [])
        session = self.setup_session(ready(issued_at=NOW+1))
        with self.assertRaisesRegex(ContractError, 'backwards'):
            session.access_token()
        self.assertEqual(self.counts, [])

    def test_no_overwrite_and_no_business_retry_from_saved_credential(self):
        session = self.setup_session(ready())
        with self.assertRaisesRegex(ContractError, 'cannot be replaced'):
            session.access_token(code='synthetic-code', credentials=self.credentials)
        self.assertEqual(self.counts, [])
        session = self.setup_session(ready(), [account(errno=111)])
        with self.assertRaises(ContractError):
            session.access_token(credentials=self.credentials)
        self.assertEqual(self.counts, ['uinfo'])
        self.credentials.assert_not_called()
        with self.assertRaisesRegex(ContractError, 'already attempted'):
            session.access_token()
        self.assertEqual(self.store.data, ready())

    def test_record_corruption_and_wrong_owner_fail_without_io(self):
        wrong = asdict(IDENTITY); wrong['account_ref'] = '67890'
        for data in (ready(identity=wrong), ready(schema_version=True), ready(expires_at=True),
                     ready(extra='synthetic-extra'), b'{}', b'x'*8193):
            session = self.setup_session(data)
            with self.assertRaises(ContractError):
                session.access_token()
            self.assertEqual(self.counts, [])
            self.assertEqual(self.store.writes, [])

    def test_default_factory_rejects_real_store_or_wire(self):
        for real in ('store', 'wire'):
            self.setup_session()
            setattr(getattr(self, real), 'is_mock', False)
            with self.assertRaisesRegex(ContractError, 'separately approved'):
                OAuthSession(IDENTITY, self.store, self.wire, **self.options)
            self.assertEqual(self.counts, [])

    def test_refresh_route_rejects_parameter_expansion_before_connection(self):
        session = self.setup_session(ready(expires_at=NOW))
        session.access_token(credentials=self.credentials)
        req = self.wire.request.call_args_list[0].args[0]
        for target in (req.target+'&scope=extra', req.target+'&refresh_token=other',
                       req.target.replace('grant_type=refresh_token', 'grant_type=authorization_code')):
            with self.assertRaises(ContractError):
                BaiduHttpsWire._validate(replace(req, target=target))

    def test_actual_child_death_after_refresh_response_blocks_restart(self):
        # Only a tiny synthetic persistent-store fixture; no emulated platform.
        # Child uses explicit PYTHONPATH and the same network audit hook as CI.
        with tempfile.TemporaryDirectory() as root:
            record_path = Path(root)/'synthetic-record.json'
            record_path.write_bytes(ready(expires_at=NOW))
            script = Path(root)/'die.py'
            script.write_text('''import os, sys
from pathlib import Path
from dev import offline_guard
sys.addaudithook(offline_guard)
from test_baidu_oauth import *
class Persistent(Store):
    def compare_and_swap(self, old, new):
        super().compare_and_swap(old, new)
        with path.open('wb') as output:
            output.write(new); output.flush(); os.fsync(output.fileno())
path = Path(sys.argv[1]); store = Persistent(path.read_bytes())
def wire(request):
    assert request.label == 'oauth_refresh'
    # Model provider reply having arrived, before validation or ready save.
    response = grant()
    os._exit(76)
OAuthSession(IDENTITY, store, Mock(is_mock=True, request=wire), check=lambda: True,
    count=lambda label, call: call(), clock=lambda: NOW).access_token(credentials=lambda: APP)
''')
            package = Path(__file__).resolve().parents[1]
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                PYTHONPATH=os.pathsep.join(map(str, [package, package/'src', package/'tests'])))
            result = subprocess.run([sys.executable, '-B', str(script), str(record_path)],
                env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 76, result.stderr.decode())
            session = self.setup_session(record_path.read_bytes())
            with self.assertRaisesRegex(ContractError, 'unresolved'):
                session.access_token(credentials=self.credentials)
            self.assertEqual(self.counts, [])
            self.assertNotIn(b'synthetic-refresh-old', record_path.read_bytes())
