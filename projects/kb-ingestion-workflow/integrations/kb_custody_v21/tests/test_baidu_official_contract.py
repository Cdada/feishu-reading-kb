"""Official SDK create-response shapes through the existing offline Archive.

The pinned official SDK's optional-parameter and multi-block tests omit isdir
and md5 from successful create receipts. Use newly synthesized values here;
these responses are not real-platform observations. These synthetic contracts do not enable real cloud synchronization.
"""
import json
import unittest

from kb_custody_v21.contracts import ContractError
from test_baidu_protocol import response
import test_baidu_protocol
import test_b03


class OfficialContractTests(unittest.TestCase):
    def fixture(self):
        f = test_baidu_protocol.BaiduProtocolTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.setup_protocol()
        f.steps[5] = ('create', response(dict(errno=0, **{
            key: f.item[key] for key in ('fs_id', 'path', 'size')})))
        return f

    def assert_hold(self, f):
        self.assertEqual(f.archive.operation(f.oid)['state'], 'unknown')
        location = f.archive.location(f.aid)
        self.assertEqual(location['integrity_hold'], 1)
        self.assertIsNone(location['first_content_verified_at'])
        with f.staging.input_binding(f.entry) as binding:
            self.assertEqual(binding['stream'].read(), f.content)

    def test_sparse_official_shape_requires_details_and_download_before_success(self):
        f = self.fixture()
        respond = f.wire.request.side_effect

        def observe(request):
            if request.label in ('filemetas', 'download_meta', 'download'):
                self.assertEqual(f.archive.operation(f.oid)['state'], 'running')
                self.assertIsNone(f.archive.location(f.aid)['first_content_verified_at'])
            return respond(request)

        f.wire.request.side_effect = observe
        result = f.session(f.oid).execute(f.oid)
        self.assertEqual(result['verification_level'], 'content_sha256_verified')
        self.assertFalse(result['integrity_hold'])
        self.assertEqual([r.label for r in f.calls], [name for name, _ in f.steps])
        self.assertEqual(len(f.calls), 10)  # No added request or automatic retry.

    def test_present_invalid_create_type_still_stops_before_details(self):
        for value in (None, False, True, '0', 1):
            with self.subTest(value=value):
                f = self.fixture()
                item = json.loads(f.steps[5][1].body)
                item['isdir'] = value
                f.steps[5] = ('create', response(item))
                with self.assertRaises(ContractError):
                    f.session(f.oid).execute(f.oid)
                self.assertEqual(len(f.calls), 6)
                self.assert_hold(f)

    def test_sparse_receipt_still_requires_identity_fields(self):
        for field in ('path', 'size', 'fs_id'):
            with self.subTest(field=field):
                f = self.fixture()
                item = json.loads(f.steps[5][1].body)
                item.pop(field)
                f.steps[5] = ('create', response(item))
                with self.assertRaises(ContractError):
                    f.session(f.oid).execute(f.oid)
                self.assertEqual(len(f.calls), 6)
                self.assert_hold(f)

    def test_detail_must_supply_file_type_and_same_identity(self):
        for field, value in (('isdir', None), ('isdir', False), ('isdir', 1),
                             ('path', '/apps/synthetic/test/other.md'),
                             ('size', 1), ('fs_id', 987654321)):
            with self.subTest(field=field, value=value):
                f = self.fixture()
                item = dict(f.item)
                if value is None:
                    item.pop(field)
                else:
                    item[field] = value
                f.steps[6] = ('filemetas', response(dict(errno=0, list=[item])))
                with self.assertRaises(ContractError):
                    f.session(f.oid).execute(f.oid)
                self.assertEqual(len(f.calls), 7)
                self.assert_hold(f)

    def test_sparse_receipt_does_not_bypass_downloaded_sha(self):
        f = self.fixture()
        f.steps[8] = ('download', response(b'X' * len(f.content)))
        with self.assertRaisesRegex(ContractError, 'does not match'):
            f.session(f.oid).execute(f.oid)
        self.assertEqual(len(f.calls), 9)
        self.assert_hold(f)

    def test_budget_after_sparse_create_preserves_unknown_and_blocks_second_plan(self):
        f = self.fixture()
        second = f.archive.plan(f.aid, target=f.root+'/other.md')
        with self.assertRaisesRegex(ContractError, 'budget exhausted'):
            f.session(f.oid, calls=6).execute(f.oid)
        self.assertEqual(len(f.calls), 6)
        self.assert_hold(f)
        with self.assertRaisesRegex(ContractError, 'unresolved upload'):
            f.session(second).execute(second)
        self.assertEqual(len(f.calls), 6)

    def test_b03_sparse_observation_remains_absent_without_fabricating_type(self):
        f = test_b03.B03Tests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        value = json.loads(f.steps[8][1].body)
        value.pop('isdir')
        value.pop('md5')
        f.steps[8] = ('create', response(value))
        result = f.run_experiment()
        self.assertEqual(result['status'], 'OFFLINE_B03_VERIFIED', result)
        self.assertEqual(result['archive_result']['verification_level'], 'content_sha256_verified')
        self.assertEqual(len(f.calls), 13)
        events = [json.loads(line) for line in
                  (f.root/'b03-requests/journal.local.jsonl').read_bytes().splitlines()]
        receipt = next(e for e in events if e.get('received') == 9)
        self.assertEqual(receipt['create_observation']['isdir'], 'absent')
        self.assertEqual(receipt['create_observation']['md5'], 'absent')
        f.assert_old_unchanged()
