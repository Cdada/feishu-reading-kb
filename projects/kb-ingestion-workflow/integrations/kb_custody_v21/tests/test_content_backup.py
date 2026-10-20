"""P44 content-only backups with synthetic files; no network or model calls."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
import zipfile

from kb_custody_v21 import content_backup as backup
from kb_custody_v21.content_backup_cli import main
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.locking import open_lock
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.storage_layout import CONTENT_DIRS
from kb_custody_v21.storage_migration import digest_file, state_evidence
from test_custody import BASE, WORKTREE
from test_daily_store import policy
import test_reading_flow as flow_fixture
import test_relations as relation_fixture
from test_reading_content import sample, reviewed, assets
from kb_custody_v21.reading_bundle import build_bundle
from kb_custody_v21.content_snapshot import readonly
from test_storage_layout import StorageFixture


class ContentBackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='p44-', dir=BASE / 'test-output')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.runtime = Runtime(WORKTREE, DEFAULTS, self.home / 'runtime')
        self.runtime.initialize()
        self.bundle = self.home / 'content.zip'
        self.target = self.home / 'restored'
        self.put('source-staging/held/article.pdf', b'%PDF synthetic original')
        self.put('inbound/not-yet-archived.md', '尚未入库'.encode())
        self.put('conversion-work/extractions/image.png', b'synthetic image bytes')
        self.put('conversion-work/publications/draft/guide.md', '未确认导读'.encode())
        self.put('conversion-work/pdf-chunks/part.pdf', b'synthetic PDF part')

    def put(self, relative, raw):
        path = self.runtime.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def plan(self):
        return backup.plan_backup(self.runtime, self.bundle)

    def create(self, plan=None, **kwargs):
        plan = plan or self.plan()
        return backup.create_backup(self.runtime, plan['plan'], plan['plan_sha256'], **kwargs)

    def restore(self, result=None, **kwargs):
        result = result or self.create()
        return backup.restore_backup(self.bundle, result['bundle_sha256'], self.target, **kwargs)

    def repack(self, modify):
        self.create()
        with zipfile.ZipFile(self.bundle) as archive:
            files = {v.filename: archive.read(v) for v in archive.infolist()}
        modify(files)
        with zipfile.ZipFile(self.bundle, 'w') as archive:
            for name, raw in files.items():
                archive.writestr(name, raw)
        return digest_file(self.bundle)

    def test_roundtrip_includes_unpublished_content_and_all_content_roots(self):
        for root in CONTENT_DIRS:
            self.put(root + '/synthetic.bin', root.encode())
        before = {str(p.relative_to(self.runtime.data_root)): p.read_bytes() for p in self.runtime.data_root.rglob('*') if p.is_file()}
        plan = self.plan(); result = self.create(plan); restored = self.restore(result)
        self.assertEqual(result['restore_test'], 'not_run')
        self.assertEqual(restored['state'], 'restore_verified')
        self.assertEqual(restored['files'], len(before))
        for name, raw in before.items():
            self.assertEqual((self.target / name).read_bytes(), raw)
            self.assertEqual((self.runtime.data_root / name).read_bytes(), raw)
        self.assertFalse(restored['runtime_restore'])
        self.assertEqual(restored['model_calls'], 0)
        self.assertGreater(plan['required_free_bytes'], plan['total_bytes'])
        self.assertEqual(json.loads(Path(result['receipt']).read_text())['bundle_sha256'], digest_file(self.bundle))

    def test_secrets_sessions_full_queue_and_original_databases_are_excluded(self):
        Registry(self.runtime)
        daily = DailyStore(self.runtime.root / 'conversion-work/daily-service/queue.sqlite3', policy(), clock=lambda: 1000000)
        daily.put('credentials', dict(access_token='synthetic-token'))
        daily.reserve_model('a' * 64)
        (self.runtime.root / 'session.json').write_text('secret-synthetic-session')
        (self.runtime.root / 'cloud-assets/credential.json').write_text('secret-synthetic-credential')
        before = state_evidence(self.runtime)
        result = self.create(); self.restore(result)
        self.assertEqual(state_evidence(self.runtime), before)
        self.assertEqual(daily.summary()['model_turns'], 1)
        with zipfile.ZipFile(self.bundle) as archive:
            self.assertFalse(any(name.endswith('.sqlite3') or 'credential' in name or 'session' in name for name in archive.namelist()))
            catalog = archive.read(backup.META + 'catalog.jsonl')
            self.assertNotIn(b'secret-synthetic', catalog)
            self.assertNotIn(b'synthetic-token', catalog)
            self.assertIn(b'cumulative_turns', catalog)

    def test_zip64_and_large_file_streaming_memory(self):
        path = self.put('source-staging/large.bin', b'')
        block = b'x' * backup.BLOCK
        with path.open('wb') as stream:
            for _ in range(65):
                stream.write(block)
        tracemalloc.start()
        try:
            with patch.object(zipfile, 'ZIP64_LIMIT', 1024):
                result = self.create(); self.restore(result)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 16 * 1024**2)
        self.assertEqual(digest_file(path), digest_file(self.target / 'source-staging/large.bin'))

    def test_existing_archive_or_receipt_never_overwritten(self):
        self.bundle.write_bytes(b'keep')
        with self.assertRaises(ContractError): self.plan()
        self.assertEqual(self.bundle.read_bytes(), b'keep')
        self.bundle.unlink()
        Path(str(self.bundle) + '.receipt.json').write_bytes(b'keep')
        with self.assertRaises(ContractError): self.plan()

    def test_backup_inside_source_or_runtime_rejected(self):
        for path in (self.runtime.root / 'backup.zip', self.runtime.path('reading-library') / 'backup.zip'):
            with self.subTest(path=path), self.assertRaises(ContractError):
                backup.plan_backup(self.runtime, path)

    def test_space_failure_before_archive_or_restore_writes(self):
        with patch.object(backup, 'enough_space', side_effect=ContractError('insufficient space')):
            with self.assertRaises(ContractError): self.plan()
        self.assertFalse(self.bundle.exists())
        result = self.create()
        with patch.object(backup, 'enough_space', side_effect=ContractError('insufficient space')):
            with self.assertRaises(ContractError): self.restore(result)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.home.glob('.p44-restore-*')))

    def test_live_service_lock_blocks_backup(self):
        path = self.runtime.root / 'conversion-work/daily-service/service.lock'
        path.parent.mkdir(parents=True)
        fd = open_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ContractError): self.plan()
        finally:
            os.close(fd)

    def test_changed_plan_catalog_and_original_require_new_preview(self):
        for change in ('plan', 'catalog', 'source'):
            with self.subTest(change=change):
                plan = self.plan()
                if change == 'plan': Path(plan['plan']).write_bytes(b'changed')
                elif change == 'catalog': Path(plan['plan']).with_name('catalog.jsonl').write_bytes(b'changed')
                else: self.put('inbound/not-yet-archived.md', b'changed original')
                with self.assertRaises(ContractError): self.create(plan)
                self.assertFalse(self.bundle.exists())

    def test_state_change_after_plan_is_not_ignored(self):
        Registry(self.runtime)
        plan = self.plan()
        with sqlite3.connect(self.runtime.db) as db:
            db.execute("INSERT INTO custody_events VALUES ('synthetic-event','synthetic','observation','synthetic','now','1','{}')")
        with self.assertRaises(ContractError): self.create(plan)

    def test_wal_committed_history_is_included_without_source_side_effects(self):
        Registry(self.runtime)
        writer = sqlite3.connect(self.runtime.db)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO custody_events VALUES ('wal-event','wal-dedupe','observation','synthetic','now','1','{}')")
        writer.commit()
        self.assertGreater(Path(str(self.runtime.db) + '-wal').stat().st_size, 0)
        before = state_evidence(self.runtime)
        result = self.create(); self.restore(result)
        self.assertEqual(state_evidence(self.runtime), before)
        text = (self.target / backup.META / 'catalog.jsonl').read_text()
        self.assertIn('wal-event', text)

    def test_empty_database_observation_creates_no_source_sidecars(self):
        path = self.home / 'history.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE t(value TEXT)')
        before = {p.name: p.read_bytes() for p in self.home.glob('history.sqlite3*')}
        with readonly(path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM t').fetchone()[0], 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.home.glob('history.sqlite3*')}, before)

    def test_mutation_during_backup_keeps_partial_without_success_receipt(self):
        def mutate(point):
            if point == 'packed': self.put('inbound/not-yet-archived.md', b'changed mid-copy')
        with self.assertRaises(ContractError): self.create(hook=mutate)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(Path(str(self.bundle) + '.receipt.json').exists())
        self.assertTrue(list(self.home.glob('.p44-*.partial')))

    def test_interruption_then_new_backup_preserves_original_and_partial(self):
        def stop(_): raise OSError('synthetic interruption')
        with self.assertRaises(OSError): self.create(hook=stop)
        partials = list(self.home.glob('.p44-*.partial'))
        self.assertTrue(partials)
        self.restore()
        self.assertTrue(all(p.exists() for p in partials))
        self.assertEqual((self.target / 'inbound/not-yet-archived.md').read_text(), '尚未入库')

    def test_wrong_digest_rejected_before_creating_target(self):
        self.create()
        with self.assertRaises(ContractError): backup.restore_backup(self.bundle, '0' * 64, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.home.glob('.p44-restore-*')))

    def test_restore_refuses_existing_empty_or_populated_target(self):
        result = self.create()
        self.target.mkdir()
        with self.assertRaises(ContractError): self.restore(result)
        (self.target / 'keep').write_bytes(b'keep')
        with self.assertRaises(ContractError): self.restore(result)
        self.assertEqual((self.target / 'keep').read_bytes(), b'keep')

    def test_restore_interruption_leaves_only_identified_partial(self):
        result = self.create()
        def stop(_): raise OSError('synthetic restore interruption')
        with self.assertRaises(OSError): self.restore(result, hook=stop)
        self.assertFalse(self.target.exists())
        self.assertTrue(list(self.home.glob('.p44-restore-*.partial')))
        self.restore(result)

    def test_links_and_case_collisions_in_source_rejected(self):
        target = self.put('inbound/a.txt', b'a')
        self.runtime.path('inbound/linked.txt').symlink_to(target)
        with self.assertRaises((ContractError, OSError)): self.plan()
        (self.runtime.data_root / 'inbound/linked.txt').unlink()
        # Unicode/case uniqueness is enforced in the disk-backed inventory.
        with tempfile.TemporaryDirectory() as tmp:
            db = backup.new_index(Path(tmp) / 'plan.sqlite3')
            try:
                backup.insert_item(db, 'inbound/A.txt', 'file', 1, 'a' * 64)
                with self.assertRaises(sqlite3.IntegrityError): backup.insert_item(db, 'inbound/a.txt', 'file', 1, 'a' * 64)
            finally: db.close()

    def test_hostile_member_and_inventory_traversal_rejected_even_with_matching_hash(self):
        digest = self.repack(lambda files: files.update({'../escape': b'escape'}))
        with self.assertRaises(ContractError): backup.restore_backup(self.bundle, digest, self.target)
        self.assertFalse((self.home / 'escape').exists())
        self.assertFalse(self.target.exists())

    def test_corrupt_member_cannot_be_hidden_by_new_outer_digest(self):
        digest = self.repack(lambda files: files.update({'inbound/not-yet-archived.md': b'x' * len('尚未入库'.encode())}))
        with self.assertRaises(ContractError): backup.restore_backup(self.bundle, digest, self.target)
        self.assertFalse(self.target.exists())

    def test_missing_file_and_compressed_metadata_are_rejected_before_restore(self):
        self.create()
        with zipfile.ZipFile(self.bundle) as archive:
            files = {v.filename: archive.read(v) for v in archive.infolist()}
        with zipfile.ZipFile(self.bundle, 'w') as archive:
            for name, raw in files.items():
                if name != 'inbound/not-yet-archived.md': archive.writestr(name, raw)
        with self.assertRaises(ContractError): backup.restore_backup(self.bundle, digest_file(self.bundle), self.target)
        with zipfile.ZipFile(self.bundle, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in files.items(): archive.writestr(name, raw)
        with self.assertRaises(ContractError): backup.restore_backup(self.bundle, digest_file(self.bundle), self.target)
        self.assertFalse(self.target.exists())

    def test_restore_parent_symlink_rejected(self):
        result = self.create()
        link = self.home / 'parent-link'; link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ContractError): backup.restore_backup(self.bundle, result['bundle_sha256'], link / 'escaped')
        self.assertFalse((self.home / 'escaped').exists())

    def test_broken_card_topic_or_brief_link_blocks_verified_snapshot(self):
        source, draft = sample()
        bundle = build_bundle(source, draft, reviewed(source, draft), assets=assets(source), date='2026-10-18', existing_files={})
        for path, raw in bundle['files'].items(): self.put('reading-library/' + path, raw)
        result = self.restore()
        for prefix in ('20_Cards_知识卡片/', '30_Topics_主题索引/', '50_Daily_Briefs_知识简报/'):
            paths = [path for path in bundle['files'] if path.startswith(prefix)]
            self.assertTrue(paths)
            self.assertTrue(all((self.target / 'reading-library' / p).read_bytes() == bundle['files'][p] for p in paths))
        self.bundle = self.home / 'broken-links.zip'
        path = next(p for p in bundle['files'] if p.startswith('30_Topics_主题索引/'))
        with self.runtime.path('reading-library/' + path).open('a') as output:
            output.write('\n[损坏链接](missing-source.md)\n')
        with self.assertRaises(ContractError): self.plan()

    def test_declined_preview_is_retained_without_marking_it_published(self):
        fixture = flow_fixture.ReadingFlowTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        document = fixture.f.preview()
        self.runtime = fixture.a.runtime
        pid = document['publication_id']
        with sqlite3.connect(self.runtime.db) as db:
            db.execute('INSERT INTO pilot_preview_decisions VALUES (?,?,?,?,?,?,?,?)',
                (pid, 'synthetic-app', 'synthetic-chat', 'synthetic-message', 'synthetic-user', 'synthetic-preview', '暂不入库', '2026-10-18'))
        self.restore()
        self.assertTrue((self.target / 'conversion-work/publications' / (pid + '.candidate')).is_dir())
        catalog = [json.loads(v) for v in (self.target / backup.META / 'catalog.jsonl').read_text().splitlines()]
        self.assertEqual(next(r['value']['state'] for r in catalog if r['kind'] == 'publication'), 'prepared')
        self.assertEqual(next(r['value']['command'] for r in catalog if r['kind'] == 'decision'), '暂不入库')

    def test_cli_returns_structured_error_without_live_binding_change(self):
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            code = main(['restore', '--bundle', str(self.bundle), '--sha256', 'bad', '--target', str(self.target)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out.getvalue())['live_binding_changed'])

    def test_cli_plan_create_restore_are_bound_to_explicit_runtime(self):
        config = self.home / 'runtime.local.json'
        config.write_text(json.dumps(dict(DEFAULTS, runtime_relative=str(self.runtime.root.relative_to(WORKTREE)))))
        common = ['--worktree', str(WORKTREE), '--runtime-config', str(config)]
        def run(args):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                code = main(args)
            self.assertEqual(code, 0, out.getvalue())
            return json.loads(out.getvalue())['result']
        plan = run(['plan', *common, '--bundle', str(self.bundle)])
        result = run(['create', *common, '--plan', plan['plan'], '--plan-sha256', plan['plan_sha256']])
        restored = run(['restore', '--bundle', str(self.bundle), '--sha256', result['bundle_sha256'], '--target', str(self.target)])
        self.assertEqual(restored['state'], 'restore_verified')

    def test_source_card_without_frontmatter_blocks_complete_backup(self):
        source, draft = sample()
        bundle = build_bundle(source, draft, reviewed(source, draft), assets=assets(source), date='2026-10-18', existing_files={})
        for path, raw in bundle['files'].items(): self.put('reading-library/' + path, raw)
        self.put('reading-library/' + bundle['group'] + '/来源卡片.md', b'broken card')
        with self.assertRaises(ContractError): self.plan()

    def test_registered_original_missing_or_changed_blocks_complete_backup(self):
        from test_custody import CustodyTests
        fixture = CustodyTests(); fixture.setUp()
        self.runtime = fixture.runtime
        result = fixture.receive()
        original = fixture.original(result['entry_id'])
        original.write_bytes(b'wrong original')
        with self.assertRaisesRegex(ContractError, 'content reference'): self.plan()
        self.assertFalse(self.bundle.exists())

    def test_approved_complete_reading_flow_roundtrip(self):
        fixture = flow_fixture.ReadingFlowTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.approve(); fixture.finish()
        self.runtime = fixture.a.runtime
        before = state_evidence(self.runtime)
        self.restore()
        group = fixture.document()['data']['reading_group']
        self.assertTrue((self.target / 'reading-library' / group / '综合读书笔记.md').is_file())
        self.assertEqual(state_evidence(self.runtime), before)
        self.assertEqual(fixture.daily.summary()['model_turns'], 3)

    def test_relation_history_bidirectional_links_and_revoked_edges_roundtrip(self):
        fixture = relation_fixture.RelationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        first, second, third = [fixture.material(n) for n in (1, 2, 3)]
        fixture.publish(fixture.plan(first, second))
        fixture.publish(fixture.plan(first, third, kind='topic', operation='second'))
        fixture.publish(fixture.plan(first, third, kind='topic', remove=True, operation='revoke'))
        shutil.copytree(fixture.profile.root, self.runtime.path('reading-library'))
        result = self.restore()
        self.assertEqual(result['validation']['active_relations'], 1)
        path = 'reading-library/40_Projects_专项引用/资料关联/关系记录.json'
        graph = json.loads((self.target / path).read_bytes())
        self.assertEqual(len(graph['history']), 3)
        self.assertEqual((self.target / path).read_bytes(), (self.runtime.data_root / path).read_bytes())

    def test_broken_reciprocal_link_does_not_become_a_verified_backup(self):
        fixture = relation_fixture.RelationTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        first, second = fixture.material(1), fixture.material(2)
        fixture.publish(fixture.plan(first, second))
        shutil.copytree(fixture.profile.root, self.runtime.path('reading-library'))
        path = self.runtime.path('reading-library') / first['group'] / '来源卡片.md'
        path.write_text(path.read_text().replace('## 已确认的资料关联', '## 已删除'))
        with self.assertRaises(ContractError): self.plan()

    def test_external_content_store_roundtrip_ignores_stale_local_copy(self):
        (self.home / 'volume-fixture').mkdir()
        fixture = StorageFixture(self.home / 'volume-fixture')
        self.runtime = Runtime(WORKTREE, dict(DEFAULTS, storage=fixture.config), self.home / 'external-runtime', storage_probe=fixture.probe)
        self.runtime.initialize()
        self.put('source-staging/original.pdf', b'current external original')
        (self.runtime.root / 'source-staging').mkdir()
        (self.runtime.root / 'source-staging/stale.pdf').write_bytes(b'stale local copy')
        self.restore()
        self.assertEqual((self.target / 'source-staging/original.pdf').read_bytes(), b'current external original')
        self.assertFalse((self.target / 'source-staging/stale.pdf').exists())


if __name__ == '__main__':
    unittest.main()
