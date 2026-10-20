"""Offline three-request enrollment; no netdisk operation or real Keychain."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.baidu_oauth_enrollment import OAuthEnrollment, LIMITS, RUNTIME
from kb_custody_v21.baidu_oauth import OAuthIdentity
from kb_custody_v21.baidu_protocol import HttpResponse
from kb_custody_v21.contracts import ContractError
import baidu_oauth_execute
import test_baidu_oauth as fixtures
from test_baidu_boundaries import http_response


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/Path(RUNTIME).parent).mkdir(parents=True)
        self.now = datetime.fromtimestamp(fixtures.NOW, timezone.utc)
        self.approval = dict(reference='BAUTH01', approved=True, runtime_relative=RUNTIME,
            limits=LIMITS, max_calls=3, scopes=['basic', 'netdisk'], token_storage='macos_login_keychain',
            persistent_use='authorized_windows_only', writes=['oauth_record_only'],
            automatic_retries=0, redirects=0, netdisk_file_operations=0, browser_traffic_in_budget=False,
            app_id=fixtures.IDENTITY.app_id, app_name=fixtures.IDENTITY.app_name,
            account_ref=fixtures.IDENTITY.account_ref,
            starts_at=self.now.isoformat(), expires_at=(self.now+timedelta(hours=2)).isoformat())
        self.check = Mock(return_value=True)
        self.wire = Mock(is_mock=True)
        self.wire.request.side_effect = [fixtures.grant(), fixtures.account(), fixtures.account()]
        self.store = fixtures.Store()
        self.factory = Mock(return_value=self.store)
        self.load = Mock(return_value=fixtures.APP)
        self.code = Mock(return_value='synthetic-code')

    def create(self):
        return OAuthEnrollment(self.root, self.approval, checked_source=self.check,
            wire=self.wire, clock=lambda: self.now)

    def run_experiment(self, experiment=None):
        self.experiment = experiment or self.create()
        return self.experiment.run(load_credentials=self.load, store_factory=self.factory, obtain_code=self.code)

    def test_enrollment_three_physical_requests_saved_then_reopened_and_no_secrets_in_log(self):
        result = self.run_experiment()
        self.assertEqual(result['status'], 'OAUTH_STORED_AND_REUSED')
        self.assertEqual(result['physical_requests'], 3)
        self.assertEqual(result['automatic_refresh_real'], 'NOT_RUN')
        self.assertEqual(result['netdisk_file_operations'], 0)
        self.assertEqual([c.args[0].label for c in self.wire.request.call_args_list], ['oauth_token', 'uinfo', 'uinfo'])
        self.load.assert_called_once()
        self.code.assert_called_once()
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.factory.call_args.args[0], fixtures.IDENTITY)
        text = self.experiment.path.read_text()
        for sensitive in ('synthetic-code', fixtures.APP.app_key, fixtures.APP.secret_key,
                          'synthetic-access-new', 'synthetic-refresh-new', 'access_token=', 'refresh_token='):
            self.assertNotIn(sensitive, text)
        self.assertEqual(len([e for e in self.experiment.events if 'received' in e]), 3)
        with self.assertRaises(FileExistsError):
            self.create()

    def test_unapproved_changed_scope_and_expired_window_stop_before_claim(self):
        original = dict(self.approval)
        for change in (dict(approved=False), dict(max_calls=50), dict(scopes=['basic']),
                       dict(persistent_use='unlimited'), dict(writes=['upload']),
                       dict(expires_at=self.now.isoformat()), dict(limits=dict(LIMITS, oauth_refresh=1))):
            self.approval = dict(original, **change)
            with self.assertRaises(ContractError):
                self.create()
            self.assertFalse((self.root/RUNTIME).exists())
        self.wire.request.assert_not_called()

    def test_extra_request_or_refresh_cannot_borrow_budget(self):
        experiment = self.create()
        callback = Mock()
        for kind in ('oauth_refresh', 'create', 'uinfo'):
            with self.assertRaises(ContractError):
                experiment.perform(kind, callback)
        callback.assert_not_called()
        self.run_experiment(experiment)
        with self.assertRaises(ContractError):
            experiment.perform('uinfo', callback)
        callback.assert_not_called()

    def test_existing_record_refuses_before_code_and_preserves_tokens(self):
        self.store.data = fixtures.ready()
        result = self.run_experiment()
        self.assertEqual((result['status'], result['stage']), ('STOPPED', 'existing_record'))
        self.code.assert_not_called()
        self.wire.request.assert_not_called()
        self.assertEqual(self.store.data, fixtures.ready())

    def test_final_reuse_failure_keeps_saved_grant_but_never_marks_pass(self):
        self.wire.request.side_effect = [fixtures.grant(), fixtures.account(), fixtures.account(uk=98765)]
        result = self.run_experiment()
        self.assertEqual((result['status'], result['stage']), ('STOPPED', 'reopen_and_reuse'))
        self.assertEqual(result['physical_requests'], 3)
        self.assertEqual(json.loads(self.store.data)['state'], 'ready')
        with self.assertRaises(FileExistsError):
            self.create()

    def test_final_source_or_evidence_change_prevents_success_record(self):
        calls = []
        def wire(request):
            calls.append(request.label)
            if len(calls) == 3:
                self.check.return_value = False
            return fixtures.grant() if len(calls) == 1 else fixtures.account()
        self.wire.request.side_effect = wire
        result = self.run_experiment()
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(len(calls), 3)
        self.assertNotIn('OAUTH_STORED_AND_REUSED', self.experiment.path.read_text())

    def test_expiry_waiting_for_code_stops_before_oauth(self):
        def obtain(*args):
            self.now += timedelta(hours=3)
            return 'synthetic-code'
        self.code.side_effect = obtain
        result = self.run_experiment()
        self.assertEqual((result['status'], result['stage']), ('STOPPED', 'initial_consent'))
        self.assertEqual(result['physical_requests'], 0)
        self.assertIsNone(self.store.data)

    def test_failed_oauth_has_one_durable_intent_and_no_reexecution(self):
        self.wire.request.side_effect = OSError('synthetic-sensitive-error')
        result = self.run_experiment()
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(result['physical_requests'], 1)
        self.assertEqual(json.loads(self.store.data)['state'], 'enrollment_pending')
        self.assertNotIn('synthetic-sensitive-error', self.experiment.path.read_text())
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(self.wire.request.call_count, 1)

    def test_before_connect_refuses_unmetered_or_wrong_label(self):
        experiment = self.create()
        with self.assertRaises(ContractError):
            experiment.before_connect(Mock(label='oauth_token'))
        experiment.inflight = dict(intent=1, kind='oauth_token')
        with self.assertRaises(ContractError):
            experiment.before_connect(Mock(label='create'))

    def test_operator_guard_binds_source_approval_and_previous_evidence(self):
        prior = self.root/'synthetic-previous-evidence.json'
        prior.write_bytes(b'synthetic previous stop')
        approval = dict(self.approval, worktree=str(self.root),
            protected_files={prior.name:hashlib.sha256(prior.read_bytes()).hexdigest()})
        path = self.root/'approval.local.json'
        frozen = fixtures.encode(approval)
        path.write_bytes(frozen); path.chmod(0o600)
        with patch.object(baidu_oauth_execute, 'checked_source', return_value={}) as source:
            self.assertTrue(baidu_oauth_execute.source_check(self.root, path, frozen))
            source.assert_called_once_with(self.root, approval)
            prior.write_bytes(b'changed')
            with self.assertRaisesRegex(ContractError, 'predecessor evidence changed'):
                baidu_oauth_execute.source_check(self.root, path, frozen)
            prior.write_bytes(b'synthetic previous stop')
            path.write_bytes(fixtures.encode(dict(approval, max_calls=50)))
            with self.assertRaisesRegex(ContractError, 'approval changed'):
                baidu_oauth_execute.source_check(self.root, path, frozen)

    def test_native_entry_unapproved_stops_before_keychain_or_prompt(self):
        with patch.object(baidu_oauth_execute, 'private_approval', return_value=fixtures.encode(dict(self.approval, approved=False))), \
                patch.object(baidu_oauth_execute.subprocess, 'check_output', return_value=str(self.root)+'\n'), \
                patch.object(baidu_oauth_execute, 'LoginKeychainReader') as reader, \
                patch.object(baidu_oauth_execute, 'OAuthKeychainStore') as store, \
                patch.object(baidu_oauth_execute, 'obtain_code') as prompt:
            with self.assertRaises(ContractError):
                baidu_oauth_execute.execute()
            reader.assert_not_called(); store.assert_not_called(); prompt.assert_not_called()

    def test_live_composition_with_patched_tls_counts_each_connection_once(self):
        connection = Mock()
        connection.getresponse.side_effect = [http_response(v.body) for v in (
            fixtures.grant(), fixtures.account(), fixtures.account())]
        with patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection', return_value=connection) as tls:
            experiment = OAuthEnrollment(self.root, self.approval, checked_source=self.check,
                clock=lambda: self.now)
            result = self.run_experiment(experiment)
        self.assertEqual(result['status'], 'OAUTH_STORED_AND_REUSED')
        self.assertEqual(result['physical_requests'], 3)
        self.assertEqual(tls.call_count, 3)
        self.assertEqual(connection.request.call_count, 3)
