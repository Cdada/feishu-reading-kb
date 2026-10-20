"""Synthetic mounts only: no disk creation, real data, network or model calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from kb_custody_v21.contracts import ContractError, ENTRY_SCHEMA
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.storage_layout import ContentStore, StorageUnavailable, initialize_store, mac_volume, GIB
from kb_custody_v21.registry import Registry
from kb_custody_v21.staging import Staging
from kb_custody_v21.intake import FeishuMockIntake, MockAttachment
from kb_custody_v21.notifications import PersistentMockSender, OutboxWorker
from kb_custody_v21.conversion import CustodyConversion
from kb_custody_v21.publication import Publications
from test_custody import BASE, WORKTREE


class StorageFixture:
    def __init__(self, root):
        self.mount=root/'synthetic-volume';self.mount.mkdir()
        self.config=dict(reference='P43-storage-v1',mount_path=str(self.mount),volume_uuid=str(uuid4()),
            root=str(self.mount/'content'),store_id=str(uuid4()),minimum_free_bytes=GIB)
        self.sample=dict(volume_uuid=self.config['volume_uuid'],mount_path=str(self.mount),filesystem='apfs',
            external=True,writable=True,roles=[],free_bytes=40*GIB)
        self.probe=lambda _:deepcopy(self.sample)
        initialize_store(self.config,approved_store_id=self.config['store_id'],probe=self.probe)
        self.store=ContentStore(self.config,probe=self.probe)


class StorageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='p43-layout-',dir=BASE/'test-output')
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve()
        self.f=StorageFixture(self.base)
        self.runtime=Runtime(WORKTREE,dict(DEFAULTS,storage=self.f.config),self.base/'state',storage_probe=self.f.probe)

    def test_state_and_content_are_separate_and_old_copy_is_inactive(self):
        self.runtime.initialize()
        data=self.runtime.path('source-staging/original.md');data.write_text('synthetic')
        self.assertEqual(data.parent,self.f.store.root/'source-staging')
        self.assertTrue(self.runtime.db.is_relative_to(self.runtime.root))
        self.assertFalse((self.runtime.root/'source-staging').exists())
        self.assertEqual(self.runtime.relative(data),'source-staging/original.md')
        fd=self.runtime.read_fd(data)
        try:self.assertEqual(os.read(fd,20),b'synthetic')
        finally:os.close(fd)
        with self.assertRaisesRegex(ContractError,'inactive'):
            self.runtime.checked(self.runtime.root/'source-staging/original.md')

    def test_registry_original_conversion_and_publication_locations(self):
        registry=Registry(self.runtime);staging=Staging(registry)
        sender=PersistentMockSender(self.runtime)
        adapter=FeishuMockIntake(registry,staging,OutboxWorker(registry,sender))
        result=adapter.receive(dict(schema_version=ENTRY_SCHEMA,intake_key='synthetic',entry_key='one',
            received_filename='synthetic.md',lineage=dict(chat_id='synthetic-chat')),MockAttachment(b'# Synthetic\n\nEvidence.'))
        entry=registry.entry(result['entry_id'])
        with staging.input_binding(entry['entry_id']) as binding:
            self.assertTrue(Path(binding['path']).is_relative_to(self.f.store.root))
            self.assertEqual(binding['stream'].read(),b'# Synthetic\n\nEvidence.')
        conversion=CustodyConversion(registry)
        self.assertTrue(conversion.settings.ledger_path.is_relative_to(self.runtime.root))
        self.assertTrue(conversion.settings.output_root.is_relative_to(self.f.store.root))
        self.assertTrue(conversion.settings.delivery_root.is_relative_to(self.f.store.root))
        self.assertEqual(Publications(registry).root,self.f.store.root/'conversion-work/publications')
        inspected=conversion.inspect(entry['entry_id'])
        task=conversion.submit(inspected['inspection_id'],'synthetic-external-0001')
        conversion.worker.run_once()
        completed=conversion.service.ledger.get_task(task['task_id'])
        self.assertEqual(completed['status'],'ready_candidate')
        extraction=json.loads(completed['quality_json'])['extraction']
        row,mapping,root=conversion.extraction(extraction['extraction_id'])
        self.assertTrue(root.is_relative_to(self.f.store.root))
        self.assertEqual((root/'source.md').read_bytes(),b'# Synthetic\n\nEvidence.')
        self.assertEqual(row['relative_root'],'conversion-work/extractions/'+mapping['extraction_id'])

    def test_wrong_uuid_read_only_backup_role_and_space_are_distinct_pauses(self):
        for change,reason in ((dict(volume_uuid=str(uuid4())),'wrong_volume'),(dict(writable=False),'volume_read_only'),
                (dict(roles=['Backup']),'ordinary_external_apfs_required'),(dict(free_bytes=GIB-1),'insufficient_space')):
            with self.subTest(reason=reason):
                store=ContentStore(self.f.config,probe=lambda _,change=change:dict(self.f.sample,**change))
                with self.assertRaisesRegex(StorageUnavailable,reason):store.check(fresh=True)

    def test_missing_store_does_not_recreate_it_or_use_local_copy(self):
        self.runtime.initialize();self.runtime.path('source-staging/a.md').write_text('synthetic')
        moved=self.f.mount/'disconnected';self.f.store.root.rename(moved)
        with self.assertRaises(StorageUnavailable):self.runtime.path('source-staging/a.md')
        self.assertFalse(self.f.store.root.exists())
        self.assertFalse((self.runtime.root/'source-staging').exists())
        moved.rename(self.f.store.root)
        self.assertEqual(self.runtime.path('source-staging/a.md').read_text(),'synthetic')

    def test_marker_and_directory_identity_cannot_be_substituted(self):
        self.f.store.check();marker=self.f.store.root/'.kb-content-store.json'
        original=marker.read_text();marker.write_text('{}')
        with self.assertRaisesRegex(StorageUnavailable,'wrong_content_store'):self.f.store.check()
        marker.write_text(original)
        self.f.store.root.rename(self.f.mount/'retained')
        self.f.store.root.mkdir();(self.f.store.root/marker.name).write_text(original)
        with self.assertRaisesRegex(StorageUnavailable,'store_replaced'):self.f.store.check()

    def test_symlink_escape_and_traversal_rejected(self):
        self.runtime.initialize();outside=self.base/'outside';outside.mkdir()
        (self.f.store.root/'source-staging/link').symlink_to(outside,target_is_directory=True)
        for path in ('source-staging/link/file','source-staging/../elsewhere','/absolute','source-staging//file'):
            with self.subTest(path=path),self.assertRaises(ContractError):self.runtime.path(path)

    def test_native_probe_refuses_a_regular_directory_before_diskutil(self):
        with patch('kb_custody_v21.storage_layout.subprocess.run') as run:
            with self.assertRaises(StorageUnavailable):mac_volume(self.f.config)
            run.assert_not_called()

    def test_initialization_never_adopts_an_existing_directory(self):
        with self.assertRaises(FileExistsError):
            initialize_store(self.f.config,approved_store_id=self.f.config['store_id'],probe=self.f.probe)
        self.assertTrue(self.f.store.check())


if __name__=='__main__':unittest.main()
