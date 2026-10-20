"""Copy, resume, identity and data integrity faults against synthetic folders."""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.locking import open_lock
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.storage_layout import StorageUnavailable
from kb_custody_v21.storage_migration import create_plan,copy_plan,switch_preview,BLOCK
from test_custody import BASE,WORKTREE
from test_storage_layout import StorageFixture


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='p43-migration-',dir=BASE/'test-output')
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve()
        self.runtime=Runtime(WORKTREE,DEFAULTS,self.base/'state');self.runtime.initialize()
        self.f=StorageFixture(self.base)
        self.files={'source-staging/pending.md':b'An unapproved synthetic original.',
            'conversion-work/extractions/synthetic/source.md':'正文与脚注保留。'.encode(),
            'reading-library/source-a.md':b'[other](source-b.md)',
            'reading-library/source-b.md':b'[first](source-a.md)',
            'reading-library/relations.json':b'{"synthetic":"two-way"}'}
        for name,raw in self.files.items():
            path=self.runtime.path(name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        (self.runtime.root/'logs/secret-operational.txt').write_text('synthetic operational exclusion')

    def plan(self):
        result=create_plan(self.runtime,self.f.config,probe=self.f.probe)
        self.path,self.sha=result['plan'],result['plan_sha256'];return result

    def copy(self,**kwargs):
        return copy_plan(self.runtime,self.path,self.sha,probe=self.f.probe,**kwargs)

    def test_copy_all_content_keep_originals_and_only_propose_switch(self):
        planned=self.plan();self.assertEqual(planned['files'],len(self.files))
        result=self.copy();self.assertEqual(result['verified_files'],len(self.files))
        self.assertFalse(result['live_binding_changed']);self.assertTrue(result['source_retained'])
        for name,raw in self.files.items():
            self.assertEqual((self.f.store.root/name).read_bytes(),raw)
            self.assertEqual((self.runtime.root/name).read_bytes(),raw)
        self.assertFalse((self.f.store.root/'logs').exists())
        proposal=switch_preview(self.runtime,self.path,self.sha,probe=self.f.probe)
        self.assertFalse(proposal['applied']);self.assertIn('storage',proposal['runtime_config'])
        self.assertIsNone(self.runtime.storage)
        again=self.copy();self.assertEqual(again['copied'],0);self.assertEqual(again['reused'],len(self.files))

    def test_interruption_after_file_resumes_without_overwrite(self):
        self.plan()
        def interrupt(point,path):
            if point=='after_file':raise InterruptedError('synthetic stop')
        with self.assertRaises(InterruptedError):self.copy(hook=interrupt)
        self.assertFalse(Path(self.path).with_name('copy-verified.local.json').exists())
        result=self.copy();self.assertEqual(result['reused'],1)
        self.assertEqual(result['verified_files'],len(self.files))

    def test_mid_file_interruption_restarts_only_own_partial(self):
        path=self.runtime.path('source-staging/large.bin');path.write_bytes(b'x'*(BLOCK+17))
        self.plan()
        def interrupt(point,name):
            if point=='after_chunk' and name.endswith('large.bin'):raise InterruptedError('synthetic stop')
        with self.assertRaises(InterruptedError):self.copy(hook=interrupt)
        self.assertTrue(list(self.f.store.root.rglob('.p43-*.partial')))
        result=self.copy();self.assertEqual(result['verified_files'],len(self.files)+1)
        self.assertFalse(list(self.f.store.root.rglob('.p43-*.partial')))
        self.assertEqual((self.f.store.root/'source-staging/large.bin').read_bytes(),path.read_bytes())

    def test_source_change_and_addition_require_new_preview(self):
        self.plan();self.runtime.path('source-staging/pending.md').write_text('changed synthetic')
        with self.assertRaisesRegex(ContractError,'source content changed'):self.copy()
        self.assertFalse(Path(self.path).with_name('copy-verified.local.json').exists())

    def test_new_source_file_is_not_silently_omitted(self):
        self.plan();self.runtime.path('source-staging/new.md').write_text('new synthetic')
        with self.assertRaises(ContractError):self.copy()

    def test_existing_different_target_is_not_overwritten(self):
        self.plan();target=self.f.store.ensure_directory('source-staging')/'pending.md';target.write_text('keep user edit')
        with self.assertRaisesRegex(ContractError,'never overwrite'):self.copy()
        self.assertEqual(target.read_text(),'keep user edit')

    def test_unexpected_target_file_prevents_verified_marker(self):
        self.plan();(self.f.store.root/'foreign.txt').write_text('preserve')
        with self.assertRaisesRegex(ContractError,'unexpected file'):self.copy()
        self.assertEqual((self.f.store.root/'foreign.txt').read_text(),'preserve')
        self.assertFalse(Path(self.path).with_name('copy-verified.local.json').exists())

    def test_wrong_volume_read_only_and_low_space_stop_before_copy(self):
        self.plan()
        for change in (dict(volume_uuid='00000000-0000-0000-0000-000000000000'),dict(writable=False),dict(free_bytes=0)):
            with self.subTest(change=change),self.assertRaises(StorageUnavailable):
                copy_plan(self.runtime,self.path,self.sha,probe=lambda _:dict(self.f.sample,**change))

    def test_disconnect_does_not_create_fallback_mount_or_publish_receipt(self):
        self.plan();saved=self.f.mount/'detached'
        def interrupt(point,name):
            if point=='after_chunk':self.f.store.root.rename(saved)
        with self.assertRaises(StorageUnavailable):self.copy(hook=interrupt)
        self.assertFalse(self.f.store.root.exists())
        self.assertFalse(Path(self.path).with_name('copy-verified.local.json').exists())
        self.assertEqual(self.runtime.path('source-staging/pending.md').read_bytes(),self.files['source-staging/pending.md'])

    def test_running_service_fence_blocks_inventory(self):
        root=self.runtime.ensure_directory('conversion-work/daily-service')
        lock=open_lock(root/'service.lock')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ContractError,'停止归档'):self.plan()
        finally:os.close(lock)

    def test_plan_tamper_symlink_and_case_collision_are_rejected(self):
        self.plan();Path(self.path).write_bytes(Path(self.path).read_bytes()+b'changed')
        with self.assertRaisesRegex(ContractError,'digest changed'):self.copy()
        link=self.runtime.path('source-staging/link');link.symlink_to(self.base,target_is_directory=True)
        with self.assertRaisesRegex(ContractError,'links and special'):self.plan()
        link.unlink();self.runtime.path('source-staging/A.md').write_text('A');self.runtime.path('source-staging/a.md').write_text('a')
        # Case-sensitive CI mounts can represent both names; case-insensitive hosts cannot.
        if self.runtime.path('source-staging/A.md').stat().st_ino!=self.runtime.path('source-staging/a.md').stat().st_ino:
            with self.assertRaises(sqlite3.IntegrityError):self.plan()

    def test_switch_preview_requires_complete_copy(self):
        self.plan()
        with self.assertRaisesRegex(ContractError,'copy and digest'):switch_preview(self.runtime,self.path,self.sha,probe=self.f.probe)


if __name__=='__main__':unittest.main()
