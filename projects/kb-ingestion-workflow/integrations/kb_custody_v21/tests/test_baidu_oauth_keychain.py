"""Security.framework mock plus real local file locks; no credential access."""
import ctypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.baidu_credentials import MAX_RECORD
from kb_custody_v21.baidu_oauth_keychain import OAuthKeychainStore, SERVICE
from kb_custody_v21.contracts import ContractError
import test_baidu_oauth as fixtures


class KeychainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.security, self.core = Mock(), Mock()
        self.check = Mock(return_value=True)
        with patch('kb_custody_v21.baidu_credentials.sys.platform', 'darwin'), \
                patch.object(Path, 'is_file', return_value=True), \
                patch('kb_custody_v21.baidu_credentials.ctypes.CDLL', side_effect=[self.security, self.core]):
            self.store = OAuthKeychainStore(fixtures.IDENTITY, self.root, before_access=self.check)
        self.assertEqual(self.security.mock_calls, [])
        self.saved = None
        self.buffer = None
        def open_keychain(path, result):
            result._obj.value = 123
            return 0
        def find(chain, ns, service, na, account, size, content, item):
            self.assertEqual((ns, service, na, account),
                (len(SERVICE), SERVICE, 64, fixtures.IDENTITY.key.encode()))
            if self.saved is None:
                return -25300
            self.buffer = ctypes.create_string_buffer(self.saved)
            size._obj.value = len(self.saved)
            content._obj.value = ctypes.addressof(self.buffer)
            item._obj.value = 456
            return 0
        def add(chain, ns, service, na, account, length, content, item):
            self.assertEqual((service, account), (SERVICE, fixtures.IDENTITY.key.encode()))
            if self.saved is not None:
                return -25299
            self.saved = ctypes.string_at(content, length)
            return 0
        def modify(item, attributes, length, content):
            self.assertEqual(item.value, 456)
            self.assertIsNone(attributes)
            self.saved = ctypes.string_at(content, length)
            return 0
        self.security.SecKeychainOpen.side_effect = open_keychain
        self.security.SecKeychainFindGenericPassword.side_effect = find
        self.security.SecKeychainAddGenericPassword.side_effect = add
        self.security.SecKeychainItemModifyAttributesAndData.side_effect = modify
        self.security.SecKeychainItemFreeContent.return_value = 0

    def pending(self):
        return fixtures.encode(dict(schema_version=1, identity=fixtures.asdict(fixtures.IDENTITY), state='refresh_pending'))

    def test_exact_separate_service_add_modify_verify_and_empty_lock(self):
        with self.store.transaction():
            self.assertIsNone(self.store.read())
            self.store.compare_and_swap(None, self.pending())
            self.store.compare_and_swap(self.pending(), fixtures.ready())
            self.assertEqual(self.store.read(), fixtures.ready())
        self.security.SecKeychainAddGenericPassword.assert_called_once()
        self.security.SecKeychainItemModifyAttributesAndData.assert_called_once()
        self.assertEqual(list(self.root.glob('*.lock'))[0].read_bytes(), b'')
        self.assertFalse(any('Delete' in c[0] or 'Access' in c[0] for c in self.security.mock_calls))

    def test_constructor_and_denied_transaction_do_not_read_or_save(self):
        self.check.return_value = False
        with self.assertRaises(ContractError), self.store.transaction():
            self.store.read()
        self.assertEqual(self.security.mock_calls, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_read_and_save_require_lock_and_correct_record_identity(self):
        with self.assertRaises(ContractError):
            self.store.read()
        with self.assertRaises(ContractError):
            self.store.compare_and_swap(None, fixtures.ready())
        with self.store.transaction(), self.assertRaises(ContractError):
            self.store.compare_and_swap(None, fixtures.ready(identity={}))
        self.assertEqual(self.security.mock_calls, [])

    def test_waiting_prompt_expiry_frees_buffer_and_prevents_save(self):
        self.saved = fixtures.ready()
        original = self.security.SecKeychainFindGenericPassword.side_effect
        def find(*args):
            result = original(*args)
            self.check.return_value = False
            return result
        self.security.SecKeychainFindGenericPassword.side_effect = find
        with self.store.transaction(), self.assertRaises(ContractError):
            self.store.read()
        self.security.SecKeychainItemFreeContent.assert_called_once()
        self.assertEqual(self.core.CFRelease.call_count, 2)
        self.security.SecKeychainAddGenericPassword.assert_not_called()
        self.security.SecKeychainItemModifyAttributesAndData.assert_not_called()

    def test_compare_mismatch_never_overwrites_existing_record(self):
        self.saved = fixtures.ready()
        with self.store.transaction(), self.assertRaisesRegex(ContractError, 'no overwrite'):
            self.store.compare_and_swap(None, self.pending())
        self.assertEqual(self.saved, fixtures.ready())
        self.security.SecKeychainAddGenericPassword.assert_not_called()
        self.security.SecKeychainItemModifyAttributesAndData.assert_not_called()

    def test_missing_differs_from_cancelled_or_locked_without_fallback(self):
        for status in (-128, -25308):
            self.security.SecKeychainFindGenericPassword.side_effect = None
            self.security.SecKeychainFindGenericPassword.return_value = status
            with self.store.transaction(), self.assertRaisesRegex(ContractError, 'cancelled or unavailable'):
                self.store.read()
        self.security.SecKeychainAddGenericPassword.assert_not_called()
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_bounded_native_copy_and_release_on_bad_length(self):
        self.saved = b'x'*(MAX_RECORD+1)
        with patch('kb_custody_v21.baidu_oauth_keychain.ctypes.string_at') as copy:
            with self.store.transaction(), self.assertRaisesRegex(ContractError, 'bounded'):
                self.store.read()
            copy.assert_not_called()
        self.security.SecKeychainItemFreeContent.assert_called_once()
        self.assertEqual(self.core.CFRelease.call_count, 2)

    def test_failed_or_unverifiable_save_does_not_retry_or_clear_pending(self):
        for mode in ('failed', 'not_saved'):
            self.saved = self.pending()
            self.security.SecKeychainItemModifyAttributesAndData.side_effect = None
            self.security.SecKeychainItemModifyAttributesAndData.return_value = -50 if mode == 'failed' else 0
            with self.store.transaction(), self.assertRaises(ContractError):
                self.store.compare_and_swap(self.pending(), fixtures.ready())
            self.assertEqual(self.saved, self.pending())
        self.assertEqual(self.security.SecKeychainItemModifyAttributesAndData.call_count, 2)

    def test_concurrent_independent_store_is_rejected_before_keychain(self):
        other = object.__new__(OAuthKeychainStore)
        other.__dict__ = dict(self.store.__dict__)
        with self.store.transaction():
            with self.assertRaisesRegex(ContractError, 'another credential operation'), other.transaction():
                other.read()
        self.assertEqual(self.security.mock_calls, [])
        # Acquiring a released lock alone never clears a persisted pending state.
        self.saved = self.pending()
        with other.transaction():
            self.assertEqual(other.read(), self.pending())

    def test_lock_symlink_permission_and_hardlink_are_rejected(self):
        link = self.root/'link'
        link.symlink_to(self.root, target_is_directory=True)
        self.store.lock_directory = link
        with self.assertRaises(OSError), self.store.transaction():
            pass
        self.store.lock_directory = self.root
        os.chmod(self.root, 0o755)
        with self.assertRaises(ContractError), self.store.transaction():
            pass
        os.chmod(self.root, 0o700)
        lock = self.root/('oauth-'+fixtures.IDENTITY.key+'.lock')
        lock.touch(mode=0o600)
        os.link(lock, self.root/'other.lock')
        with self.assertRaises(ContractError), self.store.transaction():
            pass
        self.assertEqual(self.security.mock_calls, [])

    def test_whole_session_with_native_mock_storage_reopens_and_reuses(self):
        # Only this test explicitly marks the mocked native backend offline.
        self.store.is_mock = True
        wire = Mock(is_mock=True)
        wire.request.side_effect = [fixtures.grant(), fixtures.account(), fixtures.account()]
        labels = []
        def meter(label, call):
            labels.append(label)
            return call()
        options = dict(check=lambda: True, count=meter, clock=lambda: fixtures.NOW)
        first = fixtures.OAuthSession(fixtures.IDENTITY, self.store, wire, **options)
        first.access_token(credentials=lambda: fixtures.APP, code='synthetic-code')
        second = fixtures.OAuthSession(fixtures.IDENTITY, self.store, wire, **options)
        self.assertEqual(second.access_token(), 'synthetic-access-new')
        self.assertEqual(labels, ['oauth_token', 'uinfo', 'uinfo'])
        self.assertEqual(list(self.root.glob('*.lock'))[0].read_bytes(), b'')
