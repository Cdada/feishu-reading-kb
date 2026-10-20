"""PCS-specific receipts, through existing protocol and B01 mock chains."""
import hashlib
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01_flow import TXT
import test_baidu_protocol
import test_b01
from test_baidu_protocol import response
from test_baidu_boundaries import http_response
from b01_fixture import callbacks


class SliceProtocolTests(unittest.TestCase):
    setUp = test_baidu_protocol.BaiduProtocolTests.setUp
    receive = test_baidu_protocol.BaiduProtocolTests.receive
    session = test_baidu_protocol.BaiduProtocolTests.session
    setup_protocol = test_baidu_protocol.BaiduProtocolTests.setup_protocol
    fail_at = test_baidu_protocol.BaiduProtocolTests.fail_at

    def reject(self, value, *, matching=False):
        self.setUp()  # Each variant owns its own custody/unknown state.
        self.setup_protocol()
        if matching:
            value = dict(value, md5=self.item['md5'])
        self.fail_at('slice', response(value))
        with self.assertRaises(ContractError):
            self.session(self.oid).execute(self.oid)
        self.assertEqual([r.label for r in self.calls],
                         ['uinfo', 'quota', 'list', 'precreate', 'slice'])
        self.assertEqual(self.archive.operation(self.oid)['state'], 'unknown')
        self.assertTrue(self.archive.location(self.aid)['integrity_hold'])
        with self.assertRaises(ContractError):
            self.archive.plan(self.aid, target=self.path)
        self.assertEqual(len(self.calls), 5)

    def test_missing_errno_matching_md5_reaches_download_sha(self):
        self.setup_protocol()
        self.fail_at('slice', response(dict(md5=self.item['md5'], request_id='synthetic-request')))
        result = self.session(self.oid).execute(self.oid)
        self.assertEqual(result['verification_level'], 'content_sha256_verified')
        self.assertEqual(len(self.calls), 10)

    def test_present_status_must_be_integer_zero(self):
        for key in ('errno', 'error_code', 'error_no'):
            for value in ('0', None, False, 0.0, [], {}, -6):
                with self.subTest(field=key, value=value):
                    self.reject({key: value}, matching=True)

    def test_conflicting_error_codes_or_text_cannot_be_masked_by_md5(self):
        for field in ('error_code', 'error_no', 'error', 'error_msg', 'errmsg', 'error_description'):
            with self.subTest(field=field):
                self.reject(dict(errno=0, **{field: -6 if field in ('error_code', 'error_no') else 'synthetic-error'}), matching=True)

    def test_no_success_without_exact_chunk_hash(self):
        for value in ({}, {'errno': 0}, {'md5': None}, {'md5': False},
                      {'md5': 'not-a-hash'}, {'md5': '0' * 32}):
            with self.subTest(value=value):
                self.reject(value)

    def test_zero_statuses_and_empty_error_text_accept_matching_chunk(self):
        self.setup_protocol()
        self.fail_at('slice', response(dict(md5=self.item['md5'], errno=0, error_code=0,
                                           error_no=0, error='', error_msg='', errmsg='', error_description='')))
        self.assertEqual(self.session(self.oid).execute(self.oid)['verification_level'],
                         'content_sha256_verified')

    def test_xpan_missing_errno_still_stops_before_upload(self):
        self.setup_protocol()
        self.fail_at('precreate', response(dict(return_type=1, uploadid='synthetic-upload', block_list=[0])))
        with self.assertRaises(ContractError):
            self.session(self.oid).execute(self.oid)
        self.assertEqual([r.label for r in self.calls], ['uinfo', 'quota', 'list', 'precreate'])


class SliceExperimentTests(unittest.TestCase):
    setUp = test_b01.B01Tests.setUp
    experiment = test_b01.B01Tests.experiment
    fail_at = test_b01.B01Tests.fail_at

    def test_b01_missing_errno_evidence_is_allowlisted_and_chain_verified(self):
        digest = hashlib.md5(TXT).hexdigest()
        self.fail_at('slice', response(dict(md5=digest, request_id='synthetic-private-id',
                                           access_token='synthetic-token',
                                           extra='synthetic-arbitrary-field')))
        exp = self.experiment()
        result = exp.run(**callbacks())
        self.assertEqual(result['status'], 'OFFLINE_B01_VERIFIED')
        evidence = next(e for e in exp.events if e.get('received') == 8)['slice_contract']
        self.assertEqual(evidence['errno'], {'state': 'absent'})
        self.assertEqual(evidence['md5'], 'match')
        self.assertTrue(evidence['accepted'])
        journal = exp.path.read_text()
        for value in (digest, 'synthetic-private-id', 'synthetic-token',
                      'synthetic-arbitrary-field'):
            self.assertNotIn(value, journal)
        self.assertEqual(len(self.calls), 13)

    def test_rejected_slice_evidence_distinguishes_typed_provider_error(self):
        self.fail_at('slice', response(dict(error_code=31066, error_msg='synthetic-sensitive-value')))
        exp = self.experiment()
        result = exp.run(**callbacks())
        self.assertEqual(result['status'], 'STOPPED')
        evidence = next(e for e in exp.events if e.get('received') == 8)['slice_contract']
        self.assertEqual(evidence['error_code'], {'state': 'nonzero', 'code': 31066})
        self.assertEqual(evidence['error_msg'], 'present')
        self.assertEqual(evidence['md5'], 'absent')
        self.assertFalse(evidence['accepted'])
        self.assertNotIn('synthetic-sensitive-value', exp.path.read_text())
        self.assertEqual(len(self.calls), 8)
        with self.assertRaises(FileExistsError):
            self.experiment()

    def test_unknown_real_composition_keeps_real_mode_without_verification(self):
        # Real HTTPS composition with patched connections: strictly offline.
        self.fail_at('slice', response(dict(md5='0' * 32)))
        replies = [http_response(r.body, status=r.status) for _, r in self.steps]
        connection = Mock()
        connection.getresponse.side_effect = replies
        exp = self.experiment(wire=None)
        with patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection', return_value=connection):
            result = exp.run(**callbacks())
        self.assertEqual(result['status'], 'STOPPED')
        loc = exp.archive.location(self.a['fixture_binding']['asset_id'])
        self.assertEqual(loc['validation_mode'], 'real_bounded')
        self.assertEqual(loc['verification_level'], 'none')
        self.assertTrue(loc['integrity_hold'])
        self.assertEqual(exp.archive.operation(exp.operation_id)['state'], 'unknown')
        self.assertEqual(connection.request.call_count, 8)
