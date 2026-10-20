"""HTTP-boundary fixtures reuse the existing synthetic intake and Archive."""
import hashlib
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from kb_custody_v21.archive import Archive
from kb_custody_v21.baidu import BaiduAdapter
from kb_custody_v21.baidu_protocol import (AuthorizationCode, BaiduProtocolTransport,
    HttpResponse, JSON_LIMIT, PAN, PCS)
from kb_custody_v21.contracts import ContractError
import test_archive_v21
from test_custody import WORKTREE


def response(value, status=200):
    return HttpResponse(status, value if isinstance(value, bytes) else json.dumps(value).encode())


class BaiduProtocolTests(unittest.TestCase):
    setUp = test_archive_v21.ArchiveTests.setUp
    receive = test_archive_v21.ArchiveTests.receive
    session = test_archive_v21.ArchiveTests.session

    def setup_protocol(self, *, auth=False, write=True):
        self.entry = self.receive()
        self.wire = Mock(is_mock=True)
        self.root = '/apps/synthetic/test'
        self.path = self.root + '/report.md'
        self.transport = BaiduProtocolTransport(self.wire, '1234567', self.root, self.path,
            token=None if auth else 'synthetic-token',
            authorization=AuthorizationCode('synthetic-key', 'synthetic-secret', 'synthetic-code') if auth else None,
            authorized_write=write)
        self.baidu = BaiduAdapter(self.transport, '1234567', self.root)
        self.archive = Archive(self.registry, self.baidu)
        self.aid = self.archive.register_raw(self.entry)
        self.oid = self.archive.plan(self.aid, target=self.path)
        self.item = dict(fs_id=123456789, path=self.path, size=len(self.content),
            isdir=0, md5=hashlib.md5(self.content).hexdigest())
        self.steps = []
        if auth:
            self.steps.append(('oauth_token', response(dict(access_token='synthetic-token',
                expires_in=1000, scope='basic netdisk', refresh_token='synthetic-refresh'))))
        self.steps += [
            ('uinfo', response(dict(errno=0, uk=1234567))),
            # free is NOT available bytes; total-used is the capacity check.
            ('quota', response(dict(errno=0, total=10**8, used=0, free=0))),
            ('list', response(dict(errno=0, list=[]))),
            ('precreate', response(dict(errno=0, return_type=1, uploadid='synthetic-upload', block_list=[0]))),
            ('slice', response(dict(errno=0, md5=self.item['md5']))),
            ('create', response(dict(errno=0, **self.item))),
            ('filemetas', response(dict(errno=0, list=[self.item]))),
            ('download_meta', response(dict(errno=0, list=[dict(self.item, dlink='https://d.pcs.baidu.com/file/synthetic?sign=synthetic%2Bsigned&empty=&access_token=synthetic-stale')]))),
            ('download', response(self.content)),
            ('filemetas', response(dict(errno=0, list=[self.item]))),
        ]
        self.calls = []
        def respond(req):
            index = len(self.calls)
            self.assertLess(index, len(self.steps), 'unexpected HTTP attempt')
            label, result = self.steps[index]
            self.assertEqual(req.label, label)
            self.calls.append(req)
            if isinstance(result, Exception):
                raise result
            return result
        self.wire.request.side_effect = respond

    def fail_at(self, label, value):
        index = next(i for i, (name, _) in enumerate(self.steps) if name == label)
        self.steps[index] = (label, value)
        return index + 1

    def attempts(self):
        return self.registry.rows('SELECT * FROM archive_attempts WHERE operation_id=? ORDER BY rowid', (self.oid,))

    def test_oauth_upload_download_actual_wire_chain_and_durable_count(self):
        self.setup_protocol(auth=True)
        session = self.session(self.oid, calls=11)
        result = session.execute(self.oid)
        self.assertEqual(result['verification_level'], 'content_sha256_verified')
        self.assertEqual(result['validation_mode'], 'offline_mock')
        self.assertEqual(len(self.calls), 11)
        self.assertEqual([r['method'] for r in self.attempts()], [r.label for r in self.calls])
        self.assertEqual(self.registry.rows('SELECT used_calls FROM archive_windows WHERE confirmation_ref=?', (session.ref,))[0]['used_calls'], 11)
        self.assertEqual(session.execute(self.oid), result)
        self.assertEqual(len(self.calls), 11)
        auth = self.calls[0]
        self.assertEqual((auth.method, auth.host), ('GET', 'openapi.baidu.com'))
        self.assertEqual(parse_qs(urlsplit(auth.target).query)['grant_type'], ['authorization_code'])
        self.assertEqual(self.calls[2].target.split('?')[0], '/api/quota')
        listing = parse_qs(urlsplit(self.calls[3].target).query)
        self.assertEqual((listing['start'], listing['limit'], listing['dir']), (['0'], ['100'], [self.root]))
        pre, chunk, create = self.calls[4:7]
        self.assertEqual((pre.method, pre.host), ('POST', PAN))
        self.assertEqual(parse_qs(pre.body.decode())['rtype'], ['0'])
        self.assertEqual(parse_qs(create.body.decode())['is_revision'], ['0'])
        self.assertEqual((chunk.method, chunk.host), ('POST', PCS))
        for request in (pre, chunk, create):
            self.assertEqual(dict(request.headers)['Content-Length'], str(len(request.body)))
        multipart = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + dict(chunk.headers)['Content-Type'] + '\r\n\r\n').encode() + chunk.body)
        parts = list(multipart.iter_parts())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_param('name', header='content-disposition'), 'file')
        self.assertEqual(parts[0].get_payload(decode=True), self.content)
        download = self.calls[9]
        self.assertEqual(dict(download.headers)['User-Agent'], PAN)
        self.assertEqual(urlsplit(download.target).path, '/file/synthetic')
        self.assertEqual(parse_qs(urlsplit(download.target).query, keep_blank_values=True),
            {'sign': ['synthetic+signed'], 'empty': [''], 'access_token': ['synthetic-token']})
        journal = json.dumps(self.attempts())
        for secret in ('synthetic-token', 'synthetic-secret', 'synthetic-code', 'synthetic+signed', 'synthetic-stale'):
            self.assertNotIn(secret, journal)
            self.assertNotIn(secret, repr(self.calls))

    def test_budget_stops_between_download_meta_and_bytes(self):
        self.setup_protocol(auth=True)
        session = self.session(self.oid, calls=9)
        with self.assertRaisesRegex(ContractError, 'budget exhausted'):
            session.execute(self.oid)
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(self.calls[-1].label, 'download_meta')
        self.assertEqual(self.archive.operation(self.oid)['state'], 'unknown')
        with self.assertRaises(ContractError):
            self.archive.session(session.ref, [self.oid], session.expires.isoformat(), 9)

    def test_auth_attempt_consumes_budget_before_account_query(self):
        self.setup_protocol(auth=True)
        with self.assertRaisesRegex(ContractError, 'budget exhausted'):
            self.session(self.oid, calls=1).execute(self.oid)
        self.assertEqual([r.label for r in self.calls], ['oauth_token'])

    def test_oauth_error_no_polling_refresh_or_api_calls(self):
        self.setup_protocol(auth=True)
        self.fail_at('oauth_token', response(dict(error='authorization_pending')))
        with self.assertRaisesRegex(ContractError, 'OAuth exchange'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), 1)

    def test_scope_mismatch_stops_before_account_query(self):
        self.setup_protocol(auth=True)
        self.fail_at('oauth_token', response(dict(access_token='synthetic-token', expires_in=100, scope='basic netdisk extra')))
        with self.assertRaisesRegex(ContractError, 'token/scope'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), 1)

    def test_wrong_account_stops_before_capacity_or_write(self):
        self.setup_protocol()
        self.fail_at('uinfo', response(dict(errno=0, uk=7654321)))
        with self.assertRaisesRegex(ContractError, 'account differs'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), 1)

    def test_conflict_on_second_page_blocks_write(self):
        self.setup_protocol()
        page = [dict(fs_id=10000+i, path=self.root+f'/synthetic-{i}.md') for i in range(100)]
        index = self.fail_at('list', response(dict(errno=0, list=page)))
        self.steps.insert(index, ('list', response(dict(errno=0, list=[self.item]))))
        with self.assertRaisesRegex(ContractError, 'conflict'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(parse_qs(urlsplit(self.calls[-1].target).query)['start'], ['100'])

    def test_directory_listing_not_permission_to_write(self):
        self.setup_protocol(write=False)
        with self.assertRaisesRegex(ContractError, 'preconditions'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual([r.label for r in self.calls], ['uinfo', 'quota', 'list'])

    def test_repeat_page_not_treated_as_complete(self):
        self.setup_protocol()
        page = [dict(fs_id=10000+i, path=self.root+f'/synthetic-{i}.md') for i in range(100)]
        index = self.fail_at('list', response(dict(errno=0, list=page)))
        self.steps.insert(index, ('list', response(dict(errno=0, list=page))))
        with self.assertRaisesRegex(ContractError, 'incomplete'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), 4)

    def test_remote_instant_completion_does_not_create_again(self):
        self.setup_protocol()
        self.fail_at('precreate', response(dict(errno=0, return_type=2, uploadid='', block_list=[])))
        with self.assertRaisesRegex(ContractError, 'instant upload'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual([r.label for r in self.calls], ['uinfo', 'quota', 'list', 'precreate'])
        self.assertEqual(self.archive.operation(self.oid)['state'], 'unknown')
        with self.assertRaisesRegex(ContractError, 'unknown prior upload'):
            self.archive.plan(self.aid, target=self.root+'/another.md')

    def test_timeout_after_create_no_retry_and_second_plan_blocked(self):
        self.setup_protocol()
        second = self.archive.plan(self.aid, target=self.root+'/second.md')
        expected = self.fail_at('create', TimeoutError('synthetic reply lost'))
        with self.assertRaises(TimeoutError):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), expected)
        self.assertEqual(self.attempts()[-1]['outcome'], 'unknown')
        with self.assertRaisesRegex(ContractError, 'unresolved upload'):
            self.session(second).execute(second)
        self.assertEqual(len(self.calls), expected)

    def test_http_redirect_does_not_follow_or_resend(self):
        self.setup_protocol()
        expected = self.fail_at('download', response(b'synthetic redirect', status=302))
        with self.assertRaisesRegex(ContractError, 'HTTP status 302'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), expected)
        self.assertEqual(self.attempts()[-1]['outcome'], 'response')

    def test_http_rate_limit_not_retried(self):
        self.setup_protocol()
        expected = self.fail_at('precreate', response(b'synthetic rate limit', status=429))
        with self.assertRaisesRegex(ContractError, 'HTTP status 429'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), expected)

    def test_unsigned_or_unreviewed_download_host_rejected(self):
        self.setup_protocol()
        expected = self.fail_at('download_meta', response(dict(errno=0, list=[dict(self.item,
            dlink='https://d.pcs.baidu.com.example.invalid/file/synthetic')])) )
        with self.assertRaisesRegex(ContractError, 'destination'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), expected)

    def test_download_identity_change_stops_before_bytes(self):
        self.setup_protocol()
        expected = self.fail_at('download_meta', response(dict(errno=0, list=[dict(self.item,
            fs_id=7654321, dlink='https://d.pcs.baidu.com/file/synthetic')])) )
        with self.assertRaisesRegex(ContractError, 'identity'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(len(self.calls), expected)

    def test_download_same_length_wrong_bytes_remains_hold(self):
        self.setup_protocol()
        self.fail_at('download', response(b'x'*len(self.content)))
        with self.assertRaisesRegex(ContractError, 'does not match'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(self.archive.location(self.aid)['integrity_hold'], 1)
        self.assertIsNone(self.archive.location(self.aid)['first_content_verified_at'])
        with self.staging.input_binding(self.entry) as binding:
            self.assertEqual(binding['stream'].read(), self.content)

    def test_bounded_json_rejects_oversized_and_duplicate_fields(self):
        self.setup_protocol()
        self.fail_at('uinfo', response(b'x'*(JSON_LIMIT+1)))
        with self.assertRaisesRegex(ContractError, 'bounded byte'):
            self.session(self.oid).execute(self.oid)
        from kb_custody_v21.baidu_protocol import json_object
        with self.assertRaisesRegex(ContractError, 'duplicate'):
            json_object(b'{"errno":1,"errno":0}')

    def test_malformed_precreate_list_is_controlled_failure(self):
        self.setup_protocol()
        self.fail_at('precreate', response(dict(errno=0, return_type=1, uploadid='synthetic-upload', block_list=[{}])))
        with self.assertRaisesRegex(ContractError, 'invalid precreate'):
            self.session(self.oid).execute(self.oid)
        self.assertEqual(self.archive.operation(self.oid)['state'], 'unknown')

    def test_live_wire_direct_call_and_extra_operations_are_disabled(self):
        self.setup_protocol()
        with self.assertRaisesRegex(ContractError, 'live Baidu'):
            BaiduProtocolTransport(Mock(is_mock=False), '1234567', self.root, self.path, token='synthetic-token')
        with self.assertRaisesRegex(ContractError, 'physical accounting'):
            self.transport.request('preflight', {})
        for name in ('mkdir', 'filemanager', 'recover_candidates'):
            with self.assertRaisesRegex(ContractError, 'not enabled'):
                self.transport.compose(name, {}, Mock(side_effect=AssertionError('unexpected wire')))
        self.wire.request.assert_not_called()

    def test_real_process_death_keeps_physical_intent_and_second_plan_blocked(self):
        self.setup_protocol()
        second = self.archive.plan(self.aid, target=self.root+'/second.md')
        child = subprocess.run([sys.executable, str(Path(__file__).with_name('archive_crash_worker.py')),
            str(WORKTREE), str(self.runtime.root), self.oid, 'protocol'], capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual((self.runtime.root/'test-output/remote-created.bin').read_bytes(), self.content)
        self.assertEqual([r['method'] for r in self.attempts()], ['uinfo','quota','list','precreate','slice','create'])
        self.assertEqual(self.attempts()[-1]['outcome'], 'started')
        self.assertEqual(self.archive.operation(self.oid)['state'], 'running')
        self.assertTrue(self.registry.rows('SELECT 1 FROM asset_leases WHERE released_at IS NULL'))
        with self.assertRaisesRegex(ContractError, 'unresolved upload'):
            self.session(second).execute(second)
        self.wire.request.assert_not_called()
