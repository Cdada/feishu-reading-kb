"""Synthetic keychain and HTTP boundaries only; never read a real credential."""
import ctypes
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.archive import Archive
from kb_custody_v21.baidu import BaiduAdapter
from kb_custody_v21.baidu_credentials import (ApplicationCredentials, LoginKeychainReader,
    MAX_RECORD, SERVICE, parse_application_record)
from kb_custody_v21.baidu_protocol import AuthorizationCode, BaiduProtocolTransport
from kb_custody_v21.contracts import ContractError
import test_baidu_protocol


def saved_record(**changes):
    data = dict(schema_version=1, provider='baidu_netdisk', app_id='1234567',
        app_name='synthetic', app_key='synthetic-app-key', secret_key='synthetic-secret-key',
        sign_key='synthetic-sign-key')
    data.update(changes)
    return json.dumps(data).encode()


class CredentialReuseTests(unittest.TestCase):
    def reader(self, data=None, *, status=0, length=None):
        reader = object.__new__(LoginKeychainReader)
        reader.path = Path('/synthetic/login.keychain-db')
        reader.security, reader.core = Mock(), Mock()
        buffer = ctypes.create_string_buffer(data or saved_record())
        def open_keychain(path, result):
            result._obj.value = 123
            return 0
        def find(keychain, service_length, service, account_length, account, size, result, item):
            self.assertEqual((service_length, service), (len(SERVICE), SERVICE))
            self.assertEqual((account_length, account), (7, b'1234567'))
            self.assertEqual(keychain.value, 123)
            self.assertIsNone(item)
            size._obj.value = len(buffer.raw) - 1 if length is None else length
            result._obj.value = ctypes.addressof(buffer)
            return status
        reader.security.SecKeychainOpen.side_effect = open_keychain
        reader.security.SecKeychainFindGenericPassword.side_effect = find
        reader.security.SecKeychainItemFreeContent.return_value = 0
        return reader

    def test_exact_existing_record_read_only_and_sign_key_not_exposed(self):
        reader, check = self.reader(), Mock(return_value=True)
        result = reader.read_application('1234567', 'synthetic', before_read=check)
        self.assertIsInstance(result, ApplicationCredentials)
        self.assertEqual((result.app_key, result.secret_key), ('synthetic-app-key', 'synthetic-secret-key'))
        self.assertFalse(hasattr(result, 'sign_key'))
        for secret in ('synthetic-app-key', 'synthetic-secret-key', 'synthetic-sign-key'):
            self.assertNotIn(secret, repr(result))
        self.assertEqual(check.call_count, 3)
        self.assertEqual([c[0] for c in reader.security.mock_calls],
            ['SecKeychainOpen', 'SecKeychainFindGenericPassword', 'SecKeychainItemFreeContent'])
        reader.core.CFRelease.assert_called_once()

    def test_callback_must_explicitly_confirm_before_keychain(self):
        for value in (False, None, 1):
            with self.subTest(value=value):
                reader = self.reader()
                with self.assertRaises(ContractError):
                    reader.read_application('1234567', 'synthetic', before_read=lambda: value)
                self.assertEqual(reader.security.mock_calls, [])

    def test_window_or_source_change_during_system_prompt_rejects_and_frees(self):
        reader = self.reader()
        with self.assertRaisesRegex(ContractError, 'expired or changed'):
            reader.read_application('1234567', 'synthetic', before_read=Mock(side_effect=[True, True, False]))
        reader.security.SecKeychainItemFreeContent.assert_called_once()
        reader.core.CFRelease.assert_called_once()

    def test_cancel_missing_or_locked_record_does_not_fallback_or_retry(self):
        for status in (-128, -25300, -25308):
            with self.subTest(status=status):
                reader = self.reader(status=status)
                with self.assertRaisesRegex(ContractError, 'unavailable'):
                    reader.read_application('1234567', 'synthetic', before_read=lambda: True)
                reader.security.SecKeychainFindGenericPassword.assert_called_once()
                reader.security.SecKeychainItemFreeContent.assert_not_called()
                reader.core.CFRelease.assert_called_once()

    def test_application_schema_and_field_types_checked_without_secret_echo(self):
        changes = [dict(app_id='7654321'), dict(app_name='another'), dict(provider='other'),
            dict(schema_version=True), dict(secret_key=8), dict(app_key=' bad'),
            dict(sign_key='bad\nsecret'), dict(extra='synthetic-token')]
        for change in changes:
            with self.subTest(fields=list(change)):
                with self.assertRaises(ContractError) as error:
                    parse_application_record(saved_record(**change), '1234567', 'synthetic')
                self.assertNotIn('synthetic-secret-key', str(error.exception))
        self.assertIsInstance(parse_application_record(saved_record(sign_key=''), '1234567', 'synthetic'), ApplicationCredentials)

    def test_duplicate_invalid_json_and_oversized_records_fail_closed(self):
        data = saved_record()[:-1] + b',"app_id":"7654321"}'
        for value in (data, b'not-json', b'x' * (MAX_RECORD + 1), b'', b'[]'):
            with self.subTest(size=len(value)), self.assertRaises(ContractError):
                parse_application_record(value, '1234567', 'synthetic')

    def test_buffer_limit_precedes_copy_and_always_releases(self):
        reader = self.reader(length=MAX_RECORD + 1)
        with patch('kb_custody_v21.baidu_credentials.ctypes.string_at') as copy:
            with self.assertRaises(ContractError):
                reader.read_application('1234567', 'synthetic', before_read=lambda: True)
            copy.assert_not_called()
        reader.security.SecKeychainItemFreeContent.assert_called_once()
        reader.core.CFRelease.assert_called_once()

    def test_malformed_saved_record_also_frees_security_buffer(self):
        reader = self.reader(data=saved_record(app_id='7654321'))
        with self.assertRaises(ContractError):
            reader.read_application('1234567', 'synthetic', before_read=lambda: True)
        reader.security.SecKeychainItemFreeContent.assert_called_once()
        reader.core.CFRelease.assert_called_once()

    def test_native_constructor_only_loads_apis_and_never_searches(self):
        security, core = Mock(), Mock()
        with patch('kb_custody_v21.baidu_credentials.sys.platform', 'darwin'), \
                patch.object(Path, 'is_file', return_value=True), \
                patch('kb_custody_v21.baidu_credentials.ctypes.CDLL', side_effect=[security, core]):
            reader = LoginKeychainReader()
        self.assertEqual(security.mock_calls, [])
        self.assertEqual(core.mock_calls, [])
        self.assertEqual(reader.security.SecKeychainFindGenericPassword.restype, ctypes.c_int32)


class FirstAccountTests(unittest.TestCase):
    setUp = test_baidu_protocol.BaiduProtocolTests.setUp
    receive = test_baidu_protocol.BaiduProtocolTests.receive
    session = test_baidu_protocol.BaiduProtocolTests.session
    setup_protocol = test_baidu_protocol.BaiduProtocolTests.setup_protocol

    def startup(self, *, write=True):
        self.setup_protocol(auth=True)
        self.transport = BaiduProtocolTransport(self.wire, None, self.root, self.path,
            authorization=AuthorizationCode('synthetic-app-key', 'synthetic-secret-key', 'synthetic-code'),
            authorized_write=write, bootstrap=True)
        self.labels = []
        def count(label, perform):
            self.labels.append(label)
            return perform()
        self.count = count

    def bind(self):
        self.assertEqual(self.transport.identify_account(self.count), '1234567')
        self.transport.bind_account('1234567')

    def mkdir_step(self, **fields):
        data = dict(errno=0, fs_id=2345678, path=self.root, isdir=1)
        data.update(fields)
        self.steps.insert(2, ('mkdir', test_baidu_protocol.response(data)))

    def test_observe_bind_mkdir_archive_sha_chain_uses_same_protocol(self):
        self.startup()
        self.mkdir_step()
        self.steps.insert(3, ('uinfo', test_baidu_protocol.response(dict(errno=0, uk=1234567))))
        self.bind()
        directory = self.transport.create_test_directory(self.count)
        self.assertEqual(directory, dict(fs_id='2345678', path=self.root))
        self.baidu = BaiduAdapter(self.transport, '1234567', self.root)
        self.archive = Archive(self.registry, self.baidu)
        self.aid = self.archive.register_raw(self.entry)
        self.oid = self.archive.plan(self.aid, target=self.path)
        result = self.session(self.oid, calls=10).execute(self.oid)
        self.assertEqual(result['verification_level'], 'content_sha256_verified')
        self.assertEqual(result['validation_mode'], 'offline_mock')
        self.assertEqual(len(self.calls), 13)
        self.assertEqual(self.labels, ['oauth_token', 'uinfo', 'mkdir'])
        from urllib.parse import parse_qs
        self.assertEqual(parse_qs(self.calls[2].body.decode()),
            {'path': [self.root], 'isdir': ['1'], 'rtype': ['0']})

    def test_observation_alone_cannot_create_directory_or_upload(self):
        self.startup()
        self.transport.identify_account(self.count)
        with self.assertRaises(ContractError):
            self.transport.create_test_directory(self.count)
        with self.assertRaises(ContractError):
            self.transport.compose('precreate', {}, self.count)
        self.assertEqual(len(self.calls), 2)

    def test_account_binding_requires_exact_observation(self):
        self.startup()
        with self.assertRaises(ContractError):
            self.transport.bind_account('1234567')
        self.transport.identify_account(self.count)
        with self.assertRaises(ContractError):
            self.transport.bind_account('7654321')
        self.transport.bind_account('1234567')
        with self.assertRaises(ContractError):
            self.transport.bind_account('7654321')
        self.assertEqual(len(self.calls), 2)

    def test_noncanonical_or_ambiguous_account_id_rejected(self):
        for value in (True, 123.0, '01', 0, '18446744073709551616'):
            with self.subTest(value=value):
                self.startup()
                self.steps[1] = ('uinfo', test_baidu_protocol.response(dict(errno=0, uk=value)))
                with self.assertRaises(ContractError):
                    self.transport.identify_account(self.count)
                with self.assertRaises(ContractError):
                    self.transport.identify_account(self.count)
                self.assertEqual(len(self.calls), 2)

    def test_bound_account_still_requires_explicit_write_scope(self):
        self.startup(write=False)
        self.bind()
        with self.assertRaises(ContractError):
            self.transport.create_test_directory(self.count)
        self.assertEqual(len(self.calls), 2)

    def test_directory_conflict_wrong_receipt_and_timeout_never_retry(self):
        for fields in (dict(errno=-8), dict(path='/apps/synthetic/other'),
                       dict(isdir=True), dict(fs_id='0')):
            with self.subTest(fields=fields):
                self.startup()
                self.mkdir_step(**fields)
                self.bind()
                with self.assertRaises(ContractError):
                    self.transport.create_test_directory(self.count)
                with self.assertRaises(ContractError):
                    self.transport.create_test_directory(self.count)
                with self.assertRaises(ContractError):
                    self.transport.compose('precreate', {}, self.count)
                self.assertEqual(len(self.calls), 3)
        self.startup()
        self.steps.insert(2, ('mkdir', TimeoutError('synthetic dropped receipt')))
        self.bind()
        with self.assertRaises(TimeoutError):
            self.transport.create_test_directory(self.count)
        with self.assertRaises(ContractError):
            self.transport.create_test_directory(self.count)
        self.assertEqual(len(self.calls), 3)

    def test_upload_rechecks_bound_account_before_file_mutation(self):
        self.startup()
        self.mkdir_step()
        self.steps.insert(3, ('uinfo', test_baidu_protocol.response(dict(errno=0, uk=7654321))))
        self.bind()
        self.transport.create_test_directory(self.count)
        with self.assertRaisesRegex(ContractError, 'differs from bound account'):
            self.transport.compose('preflight', dict(account_ref='1234567', root=self.root, target=self.path), self.count)
        self.assertEqual(len(self.calls), 4)

    def test_bootstrap_rejects_live_wire_prebound_identity_or_nested_root(self):
        for wire, account, root in ((Mock(is_mock=False), None, '/apps/synthetic/test'),
                (Mock(is_mock=True), '1234567', '/apps/synthetic/test'),
                (Mock(is_mock=True), None, '/apps/synthetic/a/b')):
            with self.subTest(root=root, account=account), self.assertRaises(ContractError):
                BaiduProtocolTransport(wire, account, root, root+'/report.md',
                    token='synthetic-token', authorized_write=True, bootstrap=True)


if __name__ == '__main__':
    unittest.main()
