"""Expiry during costly checks: native/TLS boundaries use existing offline mocks."""
from datetime import datetime
import json
from unittest.mock import Mock, patch
import unittest

from kb_custody_v21.baidu_credentials import LoginKeychainReader
from kb_custody_v21.contracts import ContractError
import test_baidu_oauth as oauth
import test_baidu_oauth_enrollment as enrollment
import test_baidu_oauth_keychain as keychain


class WindowBoundaryTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixtures without inheriting/re-discovering their test methods.
        self.flow = enrollment.EnrollmentTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.native = keychain.KeychainTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.native.store.path = self.native.root/'synthetic-login.keychain-db'

    def expire(self, *args):
        self.flow.now = datetime.fromisoformat(self.flow.approval['expires_at'])
        return True

    def experiment(self):
        result = self.flow.create()
        self.native.store.before_access = result.check
        return result

    def test_source_check_crosses_deadline_before_startup_claim(self):
        self.flow.check.side_effect = self.expire
        with self.assertRaises(ContractError):
            self.flow.create()
        self.assertFalse((self.flow.root/enrollment.RUNTIME).exists())
        self.flow.wire.request.assert_not_called()
        self.assertEqual(self.native.security.mock_calls, [])

    def test_source_check_crosses_deadline_before_application_native_read(self):
        experiment = self.experiment()
        reader = object.__new__(LoginKeychainReader)
        reader.__dict__ = dict(self.native.store.__dict__)
        self.native.security.SecKeychainFindGenericPassword.side_effect = None
        self.native.security.SecKeychainFindGenericPassword.return_value = -25300
        self.flow.check.side_effect = self.expire
        with self.assertRaises(ContractError):
            reader.read_application(oauth.IDENTITY.app_id, oauth.IDENTITY.app_name,
                                    before_read=experiment.check)
        self.native.security.SecKeychainOpen.assert_not_called()
        self.native.security.SecKeychainFindGenericPassword.assert_not_called()
        self.flow.wire.request.assert_not_called()

    def test_source_check_crosses_deadline_before_oauth_native_read(self):
        self.experiment()
        with self.native.store.transaction():
            self.flow.check.side_effect = self.expire
            with self.assertRaises(ContractError):
                self.native.store.read()
        self.native.security.SecKeychainOpen.assert_not_called()
        self.native.security.SecKeychainFindGenericPassword.assert_not_called()
        self.flow.wire.request.assert_not_called()

    def expire_at_write_check(self):
        # After the item lookup: one check protects its result, the next is
        # immediately before Add/Modify. Expire inside that final source check.
        count = None
        original = self.native.security.SecKeychainFindGenericPassword.side_effect
        def find(*args):
            nonlocal count
            result = original(*args)
            count = 0
            return result
        def check():
            nonlocal count
            if count is not None:
                count += 1
                if count == 2:
                    self.expire()
            return True
        self.native.security.SecKeychainFindGenericPassword.side_effect = find
        self.flow.check.side_effect = check

    def test_source_check_expiry_prevents_native_pending_add(self):
        self.experiment()
        with self.native.store.transaction():
            self.expire_at_write_check()
            with self.assertRaises(ContractError):
                self.native.store.compare_and_swap(None, self.native.pending())
        self.native.security.SecKeychainAddGenericPassword.assert_not_called()
        self.native.security.SecKeychainItemModifyAttributesAndData.assert_not_called()
        self.assertIsNone(self.native.saved)
        self.flow.wire.request.assert_not_called()

    def test_source_check_expiry_prevents_native_ready_modify_preserving_pending(self):
        self.experiment()
        pending = self.native.pending()
        self.native.saved = pending
        with self.native.store.transaction():
            self.expire_at_write_check()
            with self.assertRaises(ContractError):
                self.native.store.compare_and_swap(pending, oauth.ready())
        self.native.security.SecKeychainAddGenericPassword.assert_not_called()
        self.native.security.SecKeychainItemModifyAttributesAndData.assert_not_called()
        self.assertEqual(self.native.saved, pending)
        self.native.security.SecKeychainItemFreeContent.assert_called_once()
        self.flow.wire.request.assert_not_called()

    def test_open_keychain_wait_crosses_deadline_before_password_lookup(self):
        experiment = self.experiment()
        native_open = self.native.security.SecKeychainOpen.side_effect
        def slow_open(*args):
            result = native_open(*args)
            self.expire()
            return result
        self.native.security.SecKeychainOpen.side_effect = slow_open
        self.native.security.SecKeychainFindGenericPassword.side_effect = None
        self.native.security.SecKeychainFindGenericPassword.return_value = -25300
        for role in ('application', 'oauth'):
            with self.subTest(role=role):
                self.flow.now = datetime.fromisoformat(self.flow.approval['starts_at'])
                self.native.security.reset_mock()
                self.native.core.reset_mock()
                with self.assertRaises(ContractError):
                    if role == 'application':
                        reader = object.__new__(LoginKeychainReader)
                        reader.__dict__ = dict(self.native.store.__dict__)
                        reader.read_application(oauth.IDENTITY.app_id, oauth.IDENTITY.app_name,
                                                before_read=experiment.check)
                    else:
                        with self.native.store.transaction():
                            self.native.store.read()
                self.native.security.SecKeychainOpen.assert_called_once()
                self.native.security.SecKeychainFindGenericPassword.assert_not_called()
                self.native.core.CFRelease.assert_called_once()
        self.flow.wire.request.assert_not_called()

    def test_enrollment_expiry_at_ready_save_preserves_pending_and_stops_at_two_requests(self):
        experiment = self.experiment()
        self.flow.factory.return_value = self.native.store
        original = self.native.store.compare_and_swap
        def save(expected, replacement):
            if json.loads(replacement)['state'] == 'ready':
                self.expire_at_write_check()
            return original(expected, replacement)
        with patch.object(self.native.store, 'compare_and_swap', side_effect=save):
            result = self.flow.run_experiment(experiment)
        self.assertEqual((result['status'], result['stage']), ('STOPPED', 'exchange_and_save'))
        self.assertEqual(result['physical_requests'], 2)
        self.assertEqual(self.flow.wire.request.call_count, 2)
        self.assertEqual(json.loads(self.native.saved)['state'], 'enrollment_pending')
        self.native.security.SecKeychainAddGenericPassword.assert_called_once()
        self.native.security.SecKeychainItemModifyAttributesAndData.assert_not_called()
        self.flow.now = datetime.fromisoformat(self.flow.approval['starts_at'])
        self.flow.check.side_effect = None
        with self.assertRaises(FileExistsError):
            self.flow.create()
        self.assertEqual(self.flow.wire.request.call_count, 2)

    def test_source_check_crosses_deadline_at_network_boundary(self):
        from kb_custody_v21.baidu_oauth_enrollment import OAuthEnrollment
        experiment = OAuthEnrollment(self.flow.root, self.flow.approval,
            checked_source=self.flow.check, clock=lambda: self.flow.now)
        before_connect = experiment.before_connect
        def delayed_boundary(request):
            self.flow.check.side_effect = self.expire
            return before_connect(request)
        experiment.wire.before_connect = delayed_boundary
        with patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection') as tls:
            result = self.flow.run_experiment(experiment)
        self.assertEqual(result['status'], 'STOPPED')
        # An intent was already persisted, so it remains unresolved even though
        # this synthetic boundary proves no connection was actually attempted.
        self.assertEqual(result['physical_requests'], 1)
        tls.assert_not_called()
        self.assertEqual(json.loads(self.flow.store.data)['state'], 'enrollment_pending')
        with self.assertRaises(ContractError):
            experiment.perform('oauth_token', Mock())

    def test_tls_setup_crosses_deadline_before_request_bytes(self):
        from kb_custody_v21.baidu_oauth_enrollment import OAuthEnrollment
        experiment = OAuthEnrollment(self.flow.root, self.flow.approval,
            checked_source=self.flow.check, clock=lambda: self.flow.now)
        connection = Mock()
        def setup(*args, **kwargs):
            self.expire()
            return connection
        with patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection', side_effect=setup) as tls:
            result = self.flow.run_experiment(experiment)
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(result['physical_requests'], 1)
        tls.assert_called_once()
        connection.request.assert_not_called()
        connection.close.assert_called_once()
        self.assertEqual(json.loads(self.flow.store.data)['state'], 'enrollment_pending')
