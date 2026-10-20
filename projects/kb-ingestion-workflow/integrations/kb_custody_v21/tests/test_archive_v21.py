from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import tempfile
import subprocess
import sys
import selectors
from pathlib import Path
import unittest
from unittest.mock import Mock

from kb_custody_v21.archive import Archive
from kb_custody_v21.baidu import BaiduAdapter, DETAIL_ENDPOINT, fs_id
from kb_custody_v21.contracts import ContractError, new_id
from kb_custody_v21.retention import IndexBackup, Retention, AssetCache
from kb_custody_v21.runtime import Runtime, DEFAULTS
import test_conversion_v21
from test_custody import BASE, WORKTREE


class ArchiveTests(unittest.TestCase):
    setUp = test_conversion_v21.ConversionTests.setUp
    receive = test_conversion_v21.ConversionTests.receive
    convert = test_conversion_v21.ConversionTests.convert

    def setup_archive(self, qualified=True):
        self.entry = self.receive()
        self.transport = Mock(is_mock=True)
        evidence = dict(endpoint=DETAIL_ENDPOINT, kind='content_md5', validation_mode='offline_mock', evidence_ref='synthetic-endpoint-contract') if qualified else None
        self.baidu = BaiduAdapter(self.transport, 'synthetic-account', '/apps/synthetic/test', evidence)
        self.archive = Archive(self.registry, self.baidu)
        self.aid = self.archive.register_raw(self.entry)
        self.path = self.baidu.root + '/' + self.aid + '-synthetic.md'
        self.item = dict(fs_id='18446744073709551614', path=self.path, size=len(self.content),
                         md5=hashlib.md5(self.content).hexdigest(), isdir=0)

    def script(self, steps):
        calls = []
        def respond(method, payload):
            self.assertLess(len(calls), len(steps), 'unexpected physical call')
            expected, result = steps[len(calls)]
            self.assertEqual(method, expected)
            calls.append((method, payload))
            if isinstance(result, Exception):
                raise result
            return result(payload) if callable(result) else result
        self.transport.request.side_effect = respond
        return calls

    def preflight(self, exists=False):
        return dict(errno=0, account_ref='synthetic-account', root=self.baidu.root, writable=True,
                    free_bytes=10**8, target_exists=exists)

    def detail(self, item=None):
        return {'errno': 0, 'list': [item or self.item]}

    def upload_steps(self):
        return [('preflight', self.preflight()), ('precreate', dict(errno=0, uploadid='synthetic-upload', return_type=1, block_list=[0])),
                ('slice', dict(errno=0, md5=self.item['md5'])), ('create', dict(errno=0, **self.item)),
                ('filemetas', self.detail())]

    def session(self, *oids, calls=30):
        return self.archive.session(new_id('confirmation'), oids,
                                    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), calls)

    def upload(self):
        oid = self.archive.plan(self.aid)
        self.script(self.upload_steps())
        self.session(oid).execute(oid)
        return oid

    def test_O_normal_content_md5_no_download_and_idempotent_plan(self):
        self.setup_archive()
        oid = self.archive.plan(self.aid)
        self.assertEqual(oid, self.archive.plan(self.aid, batch_id='different'))
        calls = self.script(self.upload_steps())
        session = self.session(oid)
        result = session.execute(oid)
        self.assertEqual(result['verification_level'], 'content_md5_verified')
        self.assertEqual(session.execute(oid), result)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[1][1]['rtype'], 0)
        self.assertEqual(calls[3][1]['is_revision'], 0)
        loc = self.archive.location(self.aid)
        self.assertEqual(loc['fs_id'], self.item['fs_id'])
        first = loc['first_content_verified_at']
        verify = self.archive.plan(self.aid, 'verify')
        self.script([('preflight', self.preflight(True)), ('filemetas', self.detail())])
        self.session(verify).execute(verify)
        self.assertEqual(self.archive.location(self.aid)['first_content_verified_at'], first)
        self.assertEqual(self.registry.entry(self.entry)['sha256'], hashlib.sha256(self.content).hexdigest())

    def test_P_unknown_hash_semantics_upgrade_stream_sha(self):
        self.setup_archive(qualified=False)
        oid = self.archive.plan(self.aid)
        self.script(self.upload_steps() + [('download', dict(errno=0, stream=io.BytesIO(self.content))), ('filemetas', self.detail())])
        result = self.session(oid).execute(oid)
        self.assertEqual(result['verification_level'], 'content_sha256_verified')
        self.assertEqual(self.archive.location(self.aid)['cloud_md5_kind'], 'unknown')

    def test_P_persistent_details_conflict_holds_even_after_sha(self):
        self.setup_archive()
        self.item['md5'] = '0' * 32
        oid = self.archive.plan(self.aid)
        steps = self.upload_steps()
        steps[2] = ('slice', dict(errno=0, md5=hashlib.md5(self.content).hexdigest()))
        self.script(steps + [('download', dict(errno=0, stream=io.BytesIO(self.content))), ('filemetas', self.detail())])
        result = self.session(oid).execute(oid)
        self.assertTrue(result['integrity_hold'])
        self.assertIsNone(self.archive.location(self.aid)['first_content_verified_at'])

    def test_Q_timeout_after_remote_create_does_not_reupload(self):
        self.setup_archive()
        oid = self.archive.plan(self.aid)
        calls = self.script(self.upload_steps()[:3] + [('create', TimeoutError('synthetic lost reply'))])
        with self.assertRaises(TimeoutError):
            self.session(oid).execute(oid)
        self.assertEqual(self.archive.operation(oid)['state'], 'unknown')
        with self.assertRaises(ContractError):
            self.session(oid).execute(oid)
        self.assertEqual(len(calls), 4)
        with self.assertRaisesRegex(ContractError, 'unknown prior upload'):
            self.archive.plan(self.aid)
        recover = self.archive.plan(self.aid, 'recover')
        self.script([('preflight', self.preflight(True)), ('recover_candidates', {'errno': 0, 'list': [self.item]}),
            ('download', dict(errno=0, stream=io.BytesIO(self.content))), ('filemetas', self.detail())])
        self.assertEqual(self.session(recover).execute(recover)['verification_level'], 'content_sha256_verified')

    def test_Q_conflict_partial_batch_preserves_first_success(self):
        self.setup_archive()
        first = self.upload()
        other = self.receive(entry_key='2')
        aid2 = self.archive.register_raw(other)
        second = self.archive.plan(aid2)
        self.script([('preflight', self.preflight(True))])
        with self.assertRaisesRegex(ContractError, 'conflict'):
            self.session(second).execute(second)
        self.assertEqual(self.archive.operation(first)['state'], 'succeeded')
        self.assertEqual(self.archive.location(self.aid)['archive_status'], 'cloud_archived_verified')
        self.assertEqual(self.archive.location(aid2)['integrity_hold'], 1)

    def test_Q_preplanned_second_upload_blocked_after_process_dies_at_remote_create(self):
        self.setup_archive()
        first = self.archive.plan(self.aid)
        second = self.archive.plan(self.aid, target=self.baidu.root + '/second.md')
        child = subprocess.run([sys.executable, str(Path(__file__).with_name('archive_crash_worker.py')),
                                str(WORKTREE), str(self.runtime.root), first],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual((self.runtime.root / 'test-output/remote-created.bin').read_bytes(), self.content)
        self.assertEqual(self.archive.operation(first)['state'], 'running')
        self.assertTrue(self.registry.rows('SELECT 1 FROM asset_leases WHERE released_at IS NULL'))
        calls = self.script([])
        with self.assertRaisesRegex(ContractError, 'unresolved upload'):
            self.session(second).execute(second)
        self.assertEqual(calls, [])
        remote_calls = [json.loads(s)['method'] for s in
                        (self.runtime.root / 'test-output/remote-calls.jsonl').read_text().splitlines()]
        self.assertEqual(remote_calls, ['preflight', 'precreate', 'slice', 'create'])
        self.assertEqual(self.archive.operation(second)['state'], 'planned')
        self.assertEqual(self.archive.location(self.aid)['planned_path'], self.path)

    def test_Q_custom_filename_recovers_only_after_strong_byte_verification(self):
        self.setup_archive()
        self.item['path'] = self.baidu.root + '/report.md'
        oid = self.archive.plan(self.aid, target=self.item['path'])
        self.script(self.upload_steps()[:3] + [('create', TimeoutError())])
        with self.assertRaises(TimeoutError):
            self.session(oid).execute(oid)
        recover = self.archive.plan(self.aid, 'recover')
        calls = self.script([('preflight', self.preflight(True)),
            ('recover_candidates', {'errno': 0, 'list': [self.item]}),
            ('download', dict(errno=0, stream=io.BytesIO(self.content))), ('filemetas', self.detail())])
        result = self.session(recover).execute(recover)
        self.assertEqual(result['verification_level'], 'content_sha256_verified')
        self.assertEqual(self.archive.location(self.aid)['current_path'], self.item['path'])
        self.assertEqual([m for m, _ in calls], ['preflight', 'recover_candidates', 'download', 'filemetas'])

    def test_Q_missing_candidate_never_permits_new_upload(self):
        self.setup_archive()
        oid = self.archive.plan(self.aid, target=self.baidu.root + '/report.md')
        self.script([('preflight', self.preflight()), ('precreate', TimeoutError())])
        with self.assertRaises(TimeoutError):
            self.session(oid).execute(oid)
        recover = self.archive.plan(self.aid, 'recover')
        calls = self.script([('preflight', self.preflight())] +
                            [('recover_candidates', {'errno': 0, 'list': []})] * 3)
        with self.assertRaisesRegex(ContractError, 'unique candidate'):
            self.session(recover).execute(recover)
        with self.assertRaisesRegex(ContractError, 'unknown prior upload'):
            self.archive.plan(self.aid, target=self.baidu.root + '/new.md')
        self.assertFalse(any(m in ('precreate', 'slice', 'create') for m, _ in calls))
        self.assertEqual(self.archive.location(self.aid)['integrity_hold'], 1)

    def test_Q_custom_filename_wrong_bytes_cannot_be_adopted(self):
        self.setup_archive()
        self.item['path'] = self.baidu.root + '/report.md'
        oid = self.archive.plan(self.aid, target=self.item['path'])
        self.script([('preflight', self.preflight()), ('precreate', TimeoutError())])
        with self.assertRaises(TimeoutError):
            self.session(oid).execute(oid)
        recover = self.archive.plan(self.aid, 'recover')
        calls = self.script([('preflight', self.preflight(True)),
            ('recover_candidates', {'errno': 0, 'list': [self.item]}),
            ('download', dict(errno=0, stream=io.BytesIO(b'x' * len(self.content))))])
        with self.assertRaisesRegex(ContractError, 'does not match original'):
            self.session(recover).execute(recover)
        self.assertIsNone(self.archive.location(self.aid)['fs_id'])
        self.assertEqual(len(calls), 3)

    def test_Q_custom_filename_historical_path_and_multiple_candidates(self):
        self.setup_archive()
        self.item['path'] = self.baidu.root + '/report.md'
        oid = self.archive.plan(self.aid, target=self.item['path'])
        self.script(self.upload_steps())
        self.session(oid).execute(oid)
        recover = self.archive.plan(self.aid, 'recover')
        calls = self.script([('preflight', self.preflight(True)), ('filemetas', {'errno': 0, 'list': []}),
            ('recover_candidates', {'errno': 0, 'list': [self.item], 'next_cursor': 'more'}),
            ('recover_candidates', {'errno': 0, 'list': [{**self.item, 'fs_id': '123'}]})])
        with self.assertRaisesRegex(ContractError, 'unique candidate'):
            self.session(recover).execute(recover)
        self.assertEqual(calls[2][1]['tier'], 'paths')
        self.assertEqual(calls[2][1]['queries'], [self.item['path']])
        self.assertEqual(len(calls), 4)  # Pagination completed; no candidate downloaded/adopted.

    def test_Q_recovery_ambiguous_candidates_and_pagination(self):
        self.setup_archive()
        oid = self.archive.plan(self.aid)
        self.script([('preflight', self.preflight()), ('precreate', TimeoutError())])
        with self.assertRaises(TimeoutError):
            self.session(oid).execute(oid)
        recover = self.archive.plan(self.aid, 'recover')
        other = {**self.item, 'fs_id': '123'}
        calls = self.script([('preflight', self.preflight(True)),
            ('recover_candidates', {'errno': 0, 'list': [self.item], 'next_cursor': 'second-page'}),
            ('recover_candidates', {'errno': 0, 'list': [other]})])
        with self.assertRaisesRegex(ContractError, 'unique candidate'):
            self.session(recover).execute(recover)
        self.assertEqual(len(calls), 3)

    def test_limits_scope_expiry_stale_plan_and_real_transport_rejected(self):
        self.setup_archive()
        oid = self.archive.plan(self.aid)
        calls = self.script([('preflight', self.preflight())])
        with self.assertRaisesRegex(ContractError, 'budget'):
            self.session(oid, calls=1).execute(oid)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ContractError):
            BaiduAdapter(Mock(is_mock=False), 'synthetic', self.baidu.root)
        with self.assertRaises(ContractError):
            self.archive.plan(self.aid, target='/personal/file')
        with self.assertRaises(ContractError):
            self.archive.session('expired', [oid], '2000-01-01T00:00:00+00:00', 10)
        with self.assertRaises(ContractError):
            fs_id(float(2**63))

    def test_R_index_backup_restore_captures_committed_wal(self):
        self.setup_archive()
        self.upload()
        backup = IndexBackup(self.registry)
        bid = backup.create()
        backup.verify(bid)
        root = Path(tempfile.mkdtemp(prefix='restored-index-', dir=BASE / 'test-output'))
        runtime = Runtime(WORKTREE, DEFAULTS, root)
        path = backup.restore_to_new_runtime(bid, runtime)
        import sqlite3
        with sqlite3.connect(path) as db:
            loc = db.execute('SELECT fs_id FROM asset_locations WHERE asset_id=?', (self.aid,)).fetchone()[0]
        self.assertEqual(loc, self.item['fs_id'])
        with self.assertRaises(ContractError):
            backup.restore_to_new_runtime(bid, runtime)
        row = backup.verify(bid)
        (self.runtime.root / row['export_path']).write_text('damaged')
        with self.assertRaises(ContractError):
            backup.verify(bid)

    def eligible(self):
        self.setup_archive()
        self.upload()
        self.retention = Retention(self.archive)
        self.retention.hold(self.aid, False, 'synthetic-release-hold')
        with self.registry.transaction() as db:
            db.execute('UPDATE asset_locations SET first_content_verified_at=? WHERE asset_id=?',
                       ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), self.aid))

    def test_S_retention_seven_days_config_and_independent_feishu_flag(self):
        self.setup_archive()
        self.upload()
        retention = Retention(self.archive)
        retention.hold(self.aid, False, 'test')
        status = retention.eligibility(self.aid)
        self.assertEqual(status['status'], 'grace_period')
        self.assertEqual(self.registry.receipt(self.entry)['feishu_attachment_retention'], 'cleanup_eligible')
        fourteen = Retention(self.archive, 14, 'policy-14', 'synthetic-policy-change')
        self.assertGreater(fourteen.eligibility(self.aid)['cleanup_not_before'], status['cleanup_not_before'])
        with self.assertRaises(ContractError):
            Retention(self.archive, 3, 'policy-14', 'synthetic-policy-change')

    def test_T_cleanup_exact_list_recheck_preserves_acceptance(self):
        self.eligible()
        plan = self.retention.plan([self.aid])
        verify = self.archive.plan(self.aid, 'verify')
        self.script([('preflight', self.preflight(True)), ('filemetas', self.detail())])
        before = self.registry.rows('SELECT * FROM notification_outbox')
        results = self.retention.execute_synthetic(plan, plan['digest'], 'synthetic-delete', self.session(verify))
        self.assertEqual(results[0]['state'], 'cleaned')
        self.assertEqual(self.retention.eligibility(self.aid)['status'], 'cleaned')
        self.assertFalse((self.runtime.root / self.registry.entry(self.entry)['relative_path']).exists())
        self.assertEqual(self.registry.receipt(self.entry)['source_staging_status'], 'accepted')
        self.assertEqual(before, self.registry.rows('SELECT * FROM notification_outbox'))
        with self.assertRaisesRegex(ContractError, 'intentionally cleaned'):
            with self.staging.input_binding(self.entry):
                pass
        self.assertEqual(self.registry.receipt(self.entry)['custody_health'], 'healthy')

    def test_T_state_change_and_active_lease_block_cleanup(self):
        self.eligible()
        plan = self.retention.plan([self.aid])
        with self.staging.input_binding(self.entry):
            self.assertIn('active_lease', self.retention.eligibility(self.aid)['reasons'])
        self.archive.reference(self.aid, 'synthetic-task', 'staging_dependency')
        verify = self.archive.plan(self.aid, 'verify')
        results = self.retention.execute_synthetic(plan, plan['digest'], 'synthetic-delete', self.session(verify))
        self.assertEqual(results[0]['state'], 'skipped')
        self.assertTrue((self.runtime.root / self.registry.entry(self.entry)['relative_path']).exists())
        self.archive.reference(self.aid, 'synthetic-task', 'staging_dependency', release=True)
        self.assertEqual(self.retention.eligibility(self.aid)['status'], 'cleanup_eligible')

    def hold_process_lease(self, kind, identifier):
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('lease_worker.py')),
                                  str(WORKTREE), str(self.runtime.root), kind, identifier],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def finish():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.addCleanup(finish)
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=15), 'child did not acquire a lease')
        message = child.stdout.readline().strip()
        if message != 'leased':
            child.wait(timeout=10)
            self.fail('lease child failed: ' + child.stderr.read())
        return child

    def test_T_old_and_process_orphaned_staging_lease_never_expires_into_cleanup(self):
        self.eligible()
        child = self.hold_process_lease('staging', self.entry)
        with self.registry.transaction() as db:
            db.execute("UPDATE asset_leases SET acquired_at='2000-01-01T00:00:00+00:00' WHERE released_at IS NULL")
        future = datetime.now(timezone.utc) + timedelta(days=365)
        self.assertIn('active_lease', self.retention.eligibility(self.aid, at=future)['reasons'])
        child.kill()
        child.wait(timeout=10)
        # OS locks end at process death; unresolved DB ownership does not.
        restarted = Archive(self.registry, self.baidu)
        self.assertIn('active_lease', Retention(restarted).eligibility(self.aid, at=future)['reasons'])
        with self.assertRaisesRegex(ContractError, 'qualify'):
            Retention(restarted).plan([self.aid])
        self.assertEqual((self.runtime.root / self.registry.entry(self.entry)['relative_path']).read_bytes(), self.content)
        self.assertTrue(self.registry.rows('SELECT 1 FROM asset_leases WHERE released_at IS NULL'))

    def test_V_old_and_process_orphaned_cache_lease_never_expires_into_prune(self):
        self.setup_archive()
        self.upload()
        path = self.runtime.root / 'cache/synthetic-retained'
        path.write_bytes(self.content)
        with self.registry.transaction() as db:
            db.execute('INSERT INTO asset_cache VALUES (?,?,?,?,?)', (self.aid,
                str(path.relative_to(self.runtime.root)), hashlib.sha256(self.content).hexdigest(),
                len(self.content), (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
        child = self.hold_process_lease('cache', self.aid)
        with self.registry.transaction() as db:
            db.execute("UPDATE cache_leases SET acquired_at='2000-01-01T00:00:00+00:00' WHERE released_at IS NULL")
            db.execute("UPDATE asset_cache SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(AssetCache(self.archive).prune(), [])
        self.assertEqual(path.read_bytes(), self.content)
        child.kill()
        child.wait(timeout=10)
        self.assertEqual(AssetCache(Archive(self.registry, self.baidu)).prune(), [])
        self.assertEqual(path.read_bytes(), self.content)
        self.assertTrue(self.registry.rows('SELECT 1 FROM cache_leases WHERE released_at IS NULL'))

    def test_U_cleanup_crash_after_unlink_reconciles_history(self):
        self.eligible()
        plan = self.retention.plan([self.aid])
        verify = self.archive.plan(self.aid, 'verify')
        self.script([('preflight', self.preflight(True)), ('filemetas', self.detail())])
        def crash(point):
            if point == 'after_cleanup_unlink':
                raise OSError('synthetic process crash')
        with self.assertRaises(OSError):
            self.retention.execute_synthetic(plan, plan['digest'], 'synthetic-delete', self.session(verify), crash)
        self.assertEqual(self.retention.reconcile_interrupted(self.aid), 'cleaned')
        self.assertEqual(self.registry.receipt(self.entry)['source_staging_status'], 'accepted')

    def test_V_W_cache_sha_lease_ttl_capacity_do_not_apply_to_staging(self):
        self.setup_archive()
        self.upload()
        verify = self.archive.plan(self.aid, 'verify')
        self.script([('preflight', self.preflight(True)), ('filemetas', self.detail()),
                     ('download', dict(errno=0, stream=io.BytesIO(self.content)))])
        cache = AssetCache(self.archive)
        future = datetime.now(timezone.utc) + timedelta(days=2)
        with cache.acquire(self.aid, self.session(verify), 'synthetic-read') as stream:
            self.assertEqual(stream.read(), self.content)
            self.assertEqual(cache.prune(at=future), [])
        self.assertEqual(cache.prune(at=future), [self.aid])
        self.assertTrue((self.runtime.root / self.registry.entry(self.entry)['relative_path']).exists())
        cache = AssetCache(self.archive, capacity_bytes=1)
        with self.assertRaisesRegex(ContractError, 'capacity'):
            with cache.acquire(self.aid, self.session(verify), 'synthetic-read'):
                pass

    def test_derived_text_registry_reuses_actual_extraction_identity(self):
        self.setup_archive()
        _, mapping = self.convert(self.entry)
        assets = self.archive.register_extraction(mapping['extraction_id'])
        self.assertIn(mapping['output_asset_id'], assets)
        with self.archive.binding(assets[0], 'synthetic-derived-read') as stream:
            self.assertEqual(stream.read(), self.content)
        self.assertFalse(self.registry.rows('SELECT * FROM derived_leases WHERE released_at IS NULL'))

    def test_busy_adapter_does_not_rebind_another_window_or_claim_operation(self):
        self.setup_archive()
        oid = self.archive.plan(self.aid)
        first = self.session(oid)
        second = self.session(oid)
        with self.baidu.bound_request(first.request):
            with self.assertRaisesRegex(ContractError, 'already serving'):
                second.execute(oid)
            self.assertEqual(self.baidu.request, first.request)
        self.assertEqual(self.archive.operation(oid)['state'], 'planned')
        self.transport.request.assert_not_called()

    def test_concurrent_cache_assets_share_one_capacity_budget(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.setup_archive()
        self.upload()
        first_aid, first_item = self.aid, dict(self.item)
        second_entry = self.receive(entry_key='second-cache-asset')
        self.aid = self.archive.register_raw(second_entry)
        self.item = {**self.item, 'fs_id': '18446744073709551613', 'path': self.baidu.root + '/' + self.aid + '-synthetic.md'}
        self.upload()
        items = {first_item['fs_id']: first_item, self.item['fs_id']: dict(self.item)}
        def respond(method, payload):
            if method == 'preflight':
                return self.preflight(True)
            if method == 'filemetas':
                return self.detail(items[str(json.loads(payload['fsids'])[0])])
            if method == 'download':
                return dict(errno=0, stream=io.BytesIO(self.content))
            raise AssertionError(method)
        self.transport.request.side_effect = respond
        barrier = Barrier(2)
        cache = AssetCache(self.archive, capacity_bytes=len(self.content))
        def read(aid):
            oid = self.archive.plan(aid, 'verify')
            session = self.session(oid)
            barrier.wait(timeout=5)
            try:
                with cache.acquire(aid, session, 'synthetic-concurrent-read') as stream:
                    self.assertEqual(stream.read(), self.content)
                return 'read'
            except ContractError as exc:
                self.assertIn('capacity exhausted', str(exc))
                return 'capacity'
        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(read, [first_aid, self.aid]))
        self.assertCountEqual(results, ['read', 'capacity'])
        self.assertEqual(sum(r['size_bytes'] for r in self.registry.rows('SELECT size_bytes FROM asset_cache')), len(self.content))

    def test_s14_published_bytes_survive_cloud_rename_cleanup_and_cached_retrieval(self):
        import test_publication_v21
        fixture = test_publication_v21.PublicationTests()
        fixture.setUp()
        self.runtime, self.registry, self.staging = fixture.runtime, fixture.registry, fixture.fixture.staging
        # Reuse the existing accepted source and actual conversion/guide/preview.
        self.entry, self.content = fixture.entry, fixture.body
        self.transport = Mock(is_mock=True)
        evidence = dict(endpoint=DETAIL_ENDPOINT, kind='content_md5', validation_mode='offline_mock', evidence_ref='synthetic-contract')
        self.baidu = BaiduAdapter(self.transport, 'synthetic-account', '/apps/synthetic/test', evidence)
        self.archive = Archive(self.registry, self.baidu)
        self.aid = self.archive.register_raw(self.entry)
        self.path = self.baidu.root + '/' + self.aid + '-synthetic.md'
        self.item = dict(fs_id='18446744073709551614', path=self.path, size=len(self.content), md5=hashlib.md5(self.content).hexdigest(), isdir=0)
        preview, root = fixture.publish()
        original_files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
        self.upload()
        old = dict(self.item)
        self.item = {**self.item, 'path': self.baidu.root + '/' + self.aid + '-renamed.md'}
        rename = self.archive.plan(self.aid, 'rename', self.item['path'])
        self.script([('preflight', self.preflight()), ('filemetas', self.detail(old)),
            ('filemanager', {'errno': 0, 'info': [{'errno': 0}]}), ('filemetas', self.detail())])
        self.session(rename).execute(rename)
        self.assertEqual(self.archive.location(self.aid)['path_snapshot'], old['path'])
        self.assertEqual(self.archive.location(self.aid)['current_path'], self.item['path'])
        retention = Retention(self.archive)
        retention.hold(self.aid, False, 'synthetic-confirmation')
        with self.registry.transaction() as db:
            db.execute('UPDATE asset_locations SET first_content_verified_at=? WHERE asset_id=?',
                ((datetime.now(timezone.utc)-timedelta(days=8)).isoformat(),self.aid))
        plan = retention.plan([self.aid])
        verify = self.archive.plan(self.aid, 'verify')
        self.script([('preflight', self.preflight(True)), ('filemetas', self.detail())])
        retention.execute_synthetic(plan, plan['digest'], 'synthetic-cleanup', self.session(verify))
        self.assertEqual(self.registry.receipt(self.entry)['local_presence'], 'cleaned')
        verify = self.archive.plan(self.aid, 'verify')
        self.script([('preflight', self.preflight(True)), ('filemetas', self.detail()),
            ('download', {'errno': 0, 'stream': io.BytesIO(self.content)})])
        with AssetCache(self.archive).acquire(self.aid, self.session(verify), 'synthetic-retrieve') as stream:
            self.assertEqual(stream.read(), self.content)
        self.assertEqual(original_files, {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()})
        self.assertEqual(fixture.publisher.publish(preview['publication_id']), root)

    def test_window_budget_survives_object_recreation(self):
        self.setup_archive()
        oid = self.archive.plan(self.aid)
        expiry = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        session = self.archive.session('synthetic-fixed-window', [oid], expiry, 1)
        self.script([('preflight', self.preflight())])
        with self.assertRaises(ContractError):
            session.execute(oid)
        with self.assertRaisesRegex(ContractError, 'stopped'):
            self.archive.session('synthetic-fixed-window', [oid], expiry, 1)
        self.assertEqual(self.registry.rows('SELECT used_calls FROM archive_windows')[0]['used_calls'], 1)

    def test_cleanup_late_hold_refuses_unlink(self):
        self.eligible()
        plan = self.retention.plan([self.aid])
        verify = self.archive.plan(self.aid, 'verify')
        self.script([('preflight', self.preflight(True)), ('filemetas', self.detail())])
        def hold(point):
            if point == 'after_cleanup_intent':
                self.retention.hold(self.aid, True, 'synthetic-new-hold')
        with self.assertRaisesRegex(ContractError, 'conditions changed'):
            self.retention.execute_synthetic(plan, plan['digest'], 'synthetic-cleanup', self.session(verify), hold)
        self.assertTrue((self.runtime.root / self.registry.entry(self.entry)['relative_path']).exists())

    def test_python_child_network_guard(self):
        import os, subprocess, sys
        package = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-c', 'import socket; socket.create_connection(("127.0.0.1",9))'],
            env={**os.environ,'KB_V21_OFFLINE_TEST':'1','PYTHONPATH':str(package/'tests/offline_site')},capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('network disabled in V2.1 offline child process',result.stderr)
