"""Synthetic content export/restore; no cloud operation or user approval claim."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from pypdf import PdfReader
from kb_custody_v21.contracts import ContractError, new_id
from kb_custody_v21.conversion import sha
from kb_custody_v21.manual_backup import ManualBackups, portable_json
from kb_custody_v21.registry import encoded
import test_publication_v21 as publication_fixture


class ManualBackupTests(unittest.TestCase):
    def setUp(self):
        self.f = publication_fixture.PublicationTests()
        self.f.setUp()
        self.preview, self.publication = self.f.publish()
        self.registry, self.runtime = self.f.registry, self.f.runtime
        self.backups = ManualBackups(self.registry)
        self.pid = self.preview['publication_id']

    def create(self):
        return self.backups.create([self.pid])

    def copy(self, batch):
        path = self.runtime.root / (new_id('download') + '.zip')
        shutil.copyfile(self.runtime.root / batch['bundle_ref'], path)
        return path

    def snapshot(self):
        tables = ('assets', 'staging_files', 'staging_receipts', 'asset_locations', 'archive_operations',
                  'staging_cleanup', 'publications_v21', 'publication_confirmations', 'active_publications_v21')
        return {table: self.registry.rows('SELECT * FROM ' + table) for table in tables}

    def test_roundtrip_exact_original_publication_pdf_and_stable_references(self):
        before = self.snapshot()
        original = self.runtime.root / self.registry.entry(self.f.entry)['relative_path']
        original_bytes = original.read_bytes()
        batch = self.create()
        self.assertEqual(batch['manual_upload_status'], 'pending_manual_upload')
        self.assertEqual(batch['verification_status'], 'not_run')
        copied = self.copy(batch)
        verified = self.backups.verify_download(batch['backup_batch_id'], copied)
        restored = self.backups.restore(batch['backup_batch_id'], verified['verification_id'])
        target = self.runtime.root / restored['target_ref']
        _, trusted = self.backups._trusted(batch['backup_batch_id'])
        self.assertNotIn('bundle_sha256', trusted['frozen'])
        scope = trusted['frozen']['scope'][0]
        self.assertEqual(json.loads((target / 'MANIFEST.json').read_bytes()), trusted['frozen'])
        self.assertEqual((target / scope['original_path']).read_bytes(), original_bytes)
        root = target / 'publications' / self.pid
        for path in self.publication.rglob('*'):
            if path.is_file():
                self.assertEqual((root / path.relative_to(self.publication)).read_bytes(), path.read_bytes())
        reader = PdfReader(root / '发布记录/已确认入库预览.pdf')
        self.assertIn('街道树荫', ''.join(p.extract_text() for p in reader.pages))
        metadata = json.loads((root / 'metadata.json').read_bytes())
        self.assertEqual(metadata['raw_asset_id'], scope['asset_id'])
        self.assertEqual(metadata['extraction']['extraction_id'], scope['extraction_id'])
        self.assertIn('街道树荫', (root / '提取/source.md').read_text())
        self.assertTrue((root / '导读.md').read_text())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertFalse(list(target.rglob('*.sqlite3')))
        self.assertFalse(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))

    def test_user_upload_report_does_not_verify_or_modify_immutable_zip(self):
        batch = self.create(); bid = batch['backup_batch_id']
        bundle = self.runtime.root / batch['bundle_ref']; before = bundle.read_bytes()
        status = self.backups.report_upload(bid, provider='baidu', confirmation_ref='synthetic-owner-report')
        self.assertEqual(status['manual_upload_status'], 'user_reported_uploaded')
        self.assertEqual(status['verification_status'], 'not_run')
        self.assertEqual(status['restore_test_status'], 'not_run')
        self.assertEqual(bundle.read_bytes(), before)
        with self.assertRaises(ContractError): self.backups.report_upload(bid, provider='baidu', confirmation_ref='')

    def test_missing_external_trusted_manifest_rejects_self_consistent_zip(self):
        batch = self.create(); copied = self.copy(batch)
        (self.runtime.root / batch['local_trusted_manifest_ref']).unlink()
        with self.assertRaises(ContractError): self.backups.verify_download(batch['backup_batch_id'], copied)
        status = self.backups.status(batch['backup_batch_id'])
        self.assertEqual(status['verification_status'], 'failed')
        self.assertNotIn('verified_at', status['latest_verification'])

    def test_tampered_trusted_record_cannot_replace_ledger_digest(self):
        batch = self.create(); copied = self.copy(batch)
        path = self.runtime.root / batch['local_trusted_manifest_ref']
        value = json.loads(path.read_bytes()); value['bundle_sha256'] = 'a' * 64
        path.write_text(encoded(value))
        with self.assertRaises(ContractError): self.backups.verify_download(batch['backup_batch_id'], copied)

    def test_original_export_is_not_a_download(self):
        batch = self.create()
        with self.assertRaises(ContractError):
            self.backups.verify_download(batch['backup_batch_id'], self.runtime.root / batch['bundle_ref'])

    def test_failed_later_copy_keeps_prior_success_and_exact_restore_binding(self):
        batch = self.create(); bid = batch['backup_batch_id']; good = self.copy(batch)
        passed = self.backups.verify_download(bid, good)
        damaged = self.copy(batch); damaged.write_bytes(b'broken')
        with self.assertRaises(ContractError): self.backups.verify_download(bid, damaged)
        status = self.backups.status(bid)
        self.assertEqual(status['verification_status'], 'failed')
        self.assertEqual(status['attempts'][0]['status'], 'download_verification_passed')
        self.assertEqual(self.backups.restore(bid, passed['verification_id'])['status'], 'passed')
        with self.assertRaises(ContractError): self.backups.restore(bid, status['latest_verification']['verification_id'])

    def test_copy_changed_after_verification_is_rejected_before_restore(self):
        batch = self.create(); copied = self.copy(batch); bid = batch['backup_batch_id']
        verified = self.backups.verify_download(bid, copied)
        copied.write_bytes(b'changed')
        with self.assertRaises(ContractError): self.backups.restore(bid, verified['verification_id'])
        status = self.backups.status(bid)
        self.assertEqual(status['restore_test_status'], 'failed')
        self.assertNotIn('restored_at', status['latest_restore'])
        self.assertFalse((self.runtime.root / 'test-output/manual-content-restores').exists())

    def test_verification_cannot_be_borrowed_by_new_batch(self):
        first = self.create(); second = self.create()
        self.assertNotEqual(first['backup_batch_id'], second['backup_batch_id'])
        verified = self.backups.verify_download(first['backup_batch_id'], self.copy(first))
        with self.assertRaises(ContractError): self.backups.restore(second['backup_batch_id'], verified['verification_id'])

    def test_candidate_or_withdrawn_publication_is_not_exported(self):
        self.f.publisher.withdraw(self.pid, 'synthetic-withdraw')
        with self.assertRaises(ContractError): self.create()
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='manual_backup_created'"))

    def test_original_damaged_during_packaging_preserves_acceptance_and_blocks_batch(self):
        collect = self.backups._collect
        original = self.runtime.root / self.registry.entry(self.f.entry)['relative_path']
        calls = []
        def mutate(ids):
            result = collect(ids)
            if not calls: original.write_bytes(b'broken')
            calls.append(True)
            return result
        with patch.object(self.backups, '_collect', side_effect=mutate):
            with self.assertRaises(ValueError): self.create()
        self.assertEqual(self.registry.receipt(self.f.entry)['source_staging_status'], 'accepted')
        self.assertEqual(self.registry.entry(self.f.entry)['custody_health'], 'suspect')
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='manual_backup_created'"))

    def test_original_damaged_during_first_read_marks_suspect(self):
        from kb_custody_v21 import manual_backup
        read = manual_backup.read_bounded
        original = self.runtime.root / self.registry.entry(self.f.entry)['relative_path']
        calls = []
        def damage(stream, size):
            if not calls:
                original.write_bytes(b'X' * size)
                calls.append(True)
            return read(stream, size)
        with patch.object(manual_backup, 'read_bounded', side_effect=damage):
            with self.assertRaises(ContractError): self.create()
        self.assertEqual(self.registry.receipt(self.f.entry)['source_staging_status'], 'accepted')
        self.assertEqual(self.registry.entry(self.f.entry)['custody_health'], 'suspect')
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='manual_backup_created'"))

    def test_private_metadata_stops_export_without_rewriting_publication(self):
        for value in ({'app_secret':'synthetic-secret'}, {'chat_id':'synthetic-chat'},
                      {'local_path':'/' + 'home/' + 'synthetic/private/file'},
                      {'path':'/var/synthetic/material'}, {'x':'ou_' + '1' * 32}):
            with self.subTest(value=next(iter(value))):
                with self.assertRaises(ContractError): portable_json(value)
        old = self.publication.joinpath('metadata.json').read_bytes()
        with patch('kb_custody_v21.manual_backup.portable_json', side_effect=ContractError('synthetic private field')):
            with self.assertRaises(ContractError): self.create()
        self.assertEqual(self.publication.joinpath('metadata.json').read_bytes(), old)

    def test_download_symlink_is_rejected(self):
        batch = self.create(); copied = self.copy(batch)
        link = self.runtime.root / 'linked.zip'; link.symlink_to(copied)
        with self.assertRaises(ContractError): self.backups.verify_download(batch['backup_batch_id'], link)

    def test_restore_never_overwrites_existing_target(self):
        batch = self.create(); verified = self.backups.verify_download(batch['backup_batch_id'], self.copy(batch))
        fixed = new_id('manual_restore')
        target = self.runtime.root / 'test-output/manual-content-restores' / fixed
        target.mkdir(parents=True); (target / 'keep.txt').write_bytes(b'keep')
        def choose(kind): return fixed if kind == 'manual_restore' else new_id(kind)
        with patch('kb_custody_v21.manual_backup.new_id', side_effect=choose):
            with self.assertRaises(ContractError): self.backups.restore(batch['backup_batch_id'], verified['verification_id'])
        self.assertEqual(list(target.iterdir()), [target / 'keep.txt'])
        self.assertEqual((target / 'keep.txt').read_bytes(), b'keep')

    def hostile(self, name='payload.txt', data=b'content', mode=stat.S_IFREG | 0o600, duplicate=False, compression=zipfile.ZIP_STORED):
        """A valid outer hash never makes unsafe archive structure acceptable."""
        manifest = dict(schema_version='manual-backup/v0.1', file_manifest={name:dict(size_bytes=len(data),sha256=sha(data))})
        path = self.runtime.root / (new_id('hostile') + '.zip')
        with zipfile.ZipFile(path, 'w', compression) as archive:
            info = zipfile.ZipInfo(name); info.external_attr = mode << 16; info.compress_type = compression
            archive.writestr(info, data)
            if duplicate:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning); archive.writestr(info, data)
            archive.writestr('MANIFEST.json', encoded(manifest).encode())
        return path, dict(frozen=manifest,bundle_sha256=sha(path.read_bytes()),bundle_size_bytes=path.stat().st_size)

    def test_archive_paths_symlinks_duplicates_and_expansion_are_rejected(self):
        for options in (dict(name='../escape.txt'), dict(name='/absolute.txt'), dict(name='a\\b'),
                        dict(name='a/../b'), dict(name='C:drive'), dict(mode=stat.S_IFLNK | 0o777),
                        dict(mode=stat.S_IFIFO | 0o600), dict(duplicate=True),
                        dict(data=b'0' * (2 * 1024**2), compression=zipfile.ZIP_DEFLATED)):
            with self.subTest(options={k:v for k,v in options.items() if k != 'data'}):
                path, trusted = self.hostile(**options)
                with self.assertRaises(ContractError): self.backups._read_copy(path, trusted)

    def test_oversized_scope_rejected_before_packaging(self):
        with patch('kb_custody_v21.manual_backup.MAX_BYTES', 8):
            with self.assertRaises(ContractError): self.create()
        with self.assertRaises(ContractError): self.backups.create([self.pid, self.pid])

    def test_restart_reads_same_trusted_batch_without_runtime_restore(self):
        batch = self.create(); copied = self.copy(batch)
        verifier = ManualBackups(self.registry)
        verified = verifier.verify_download(batch['backup_batch_id'], copied)
        restarted = ManualBackups(self.registry)
        result = restarted.restore(batch['backup_batch_id'], verified['verification_id'])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(restarted.status(batch['backup_batch_id'])['restore_test_status'], 'passed')

    def test_text_only_publication_exports_parent_original_image_without_changing_publication(self):
        self.f.test_actual_embedded_image_extract_text_only_reexpression_and_publish()
        rows = self.registry.rows('SELECT * FROM publications_v21 WHERE publication_id!=?', (self.pid,))
        self.assertEqual(len(rows), 1)
        pid = rows[0]['publication_id']
        batch = self.backups.create([pid])
        verified = self.backups.verify_download(batch['backup_batch_id'], self.copy(batch))
        restored = self.backups.restore(batch['backup_batch_id'], verified['verification_id'])
        target = self.runtime.root / restored['target_ref']
        images = list((target / 'images').rglob('*.png'))
        self.assertEqual(len(images), 1)
        metadata = json.loads((target / 'publications' / pid / 'metadata.json').read_bytes())
        original = next(a for a in metadata['assets'] if a['kind'] == 'extracted_image')
        self.assertEqual(sha(images[0].read_bytes()), original['sha256'])
        self.assertFalse(list((self.f.publisher.root / pid).rglob('*.png')))
        from PIL import Image
        with Image.open(images[0]) as image:
            self.assertEqual(image.size, (50, 40))

    def test_partial_restore_failure_keeps_files_and_prior_success(self):
        batch = self.create(); bid = batch['backup_batch_id']
        verified = self.backups.verify_download(bid, self.copy(batch))
        first = self.backups.restore(bid, verified['verification_id'])
        from kb_custody_v21 import safe_publish as fs
        write = fs.write_tree
        def fail(fd, files):
            name = next(iter(files)); write(fd, {name: files[name]})
            raise OSError('synthetic disk full')
        with patch.object(fs, 'write_tree', side_effect=fail):
            with self.assertRaises(ContractError): self.backups.restore(bid, verified['verification_id'])
        status = self.backups.status(bid)
        self.assertEqual(status['restore_test_status'], 'failed')
        self.assertTrue((self.runtime.root / first['target_ref']).is_dir())
        self.assertTrue(list((self.runtime.root / status['latest_restore']['target_ref']).rglob('*')))
        self.assertIn('passed', [a['status'] for a in status['attempts'] if a['kind'] == 'manual_backup_restore'])

    def test_offline_cli_create_verify_restore_without_dispatch(self):
        package = Path(__file__).resolve().parents[1]
        # Explicitly inherited resolved import paths work both locally and CI;
        # dev.py itself installs the network audit guard before any operation.
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', KB_V21_OFFLINE_TEST='1',
                   PYTHONPATH=os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p))
        def run(command, *args):
            reply = subprocess.run([sys.executable, '-B', str(package / 'dev.py'), command,
                '--runtime', str(self.runtime.root), *args], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(reply.returncode, 0, reply.stderr)
            return json.loads(reply.stdout)
        before = self.snapshot()
        batch = run('backup-create', '--publication-id', self.pid)
        copied = self.copy(batch)
        verified = run('backup-verify', '--batch-id', batch['backup_batch_id'], '--downloaded-copy', str(copied))
        restored = run('backup-restore', '--batch-id', batch['backup_batch_id'], '--verification-id', verified['verification_id'])
        self.assertEqual(restored['status'], 'passed')
        self.assertEqual(run('backup-status', '--batch-id', batch['backup_batch_id'])['restore_test_status'], 'passed')
        self.assertEqual(self.snapshot(), before)
