"""Exercise actual ingestion and publication against an injected synthetic volume."""
import json
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.daily_status import local_status, user_summary
from kb_custody_v21.reading_profile import ReadingProfile
from kb_custody_v21.runtime import Runtime
from kb_custody_v21.storage_layout import mac_volume, ContentStore, StorageUnavailable
from test_custody import BASE
from test_storage_layout import StorageFixture
import test_reading_flow as flow_fixture
import test_daily_backend as daily_fixture
import test_relations as relation_fixture


class StorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='p43-integration-',dir=BASE/'test-output')
        self.addCleanup(temp.cleanup);self.f=StorageFixture(Path(temp.name).resolve())

    def make_runtime(self, worktree, config, root):
        return Runtime(worktree,dict(config,storage=self.f.config),root,storage_probe=self.f.probe)

    def profile(self, runtime, config, frozen):
        return ReadingProfile(runtime,dict(config,root=str(runtime.path('reading-library'))),frozen)

    def backend(self):
        fixture=daily_fixture.DailyBackendTests()
        with patch('test_pilot_download.Runtime',side_effect=self.make_runtime):fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_full_approved_reading_flow_uses_selected_store_and_same_model_ledger(self):
        fixture=flow_fixture.ReadingFlowTests()
        with patch('test_pilot_download.Runtime',side_effect=self.make_runtime),patch('test_reading_flow.ReadingProfile',side_effect=self.profile):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.approve();fixture.finish()
        self.assertEqual(fixture.document()['phase'],'notified')
        self.assertEqual(fixture.daily.summary()['model_turns'],3)
        self.assertTrue((self.f.store.root/'reading-library'/fixture.document()['data']['reading_group']/'综合读书笔记.md').is_file())
        self.assertFalse((fixture.a.runtime.root/'reading-library').exists())
        entry=fixture.f.registry.entry(fixture.f.entry)
        self.assertTrue((self.f.store.root/entry['relative_path']).is_file())
        self.assertFalse((fixture.a.runtime.root/entry['relative_path']).exists())

    def test_pause_is_visible_once_and_does_not_advance_or_spend_then_recovers(self):
        fixture=self.backend();backend=fixture.backend
        before=fixture.store.document(fixture.mid);usage=fixture.store.summary()['model_turns']
        backend.card_listener=SimpleNamespace(tick=Mock())
        backend.collector.collect=Mock(return_value='collected')
        moved=self.f.mount/'disconnected';self.f.store.root.rename(moved)
        for _ in range(2):
            with self.assertRaisesRegex(Deferred,'storage_unavailable'):backend.advance_heavy(before)
            with self.assertRaisesRegex(Deferred,'storage_unavailable'):backend.report_status()
            self.assertEqual(backend.collect(),'collected')
            backend.card_listener.tick.assert_called_with(process_events=False)
        self.assertEqual(len(fixture.transport.calls),1)
        self.assertEqual(fixture.store.document(fixture.mid)['phase'],before['phase'])
        self.assertEqual(fixture.store.summary()['model_turns'],usage)
        self.assertIn('资料存储已暂停',user_summary(local_status(fixture.root)))
        moved.rename(self.f.store.root);backend._storage_ready()
        self.assertIsNone(fixture.store.get('storage_wait'))
        backend.advance_heavy(before)
        self.assertEqual(fixture.store.document(fixture.mid)['phase'],'converted')

    def test_disconnect_during_card_tick_keeps_collection_and_confirmation_queue(self):
        fixture=self.backend();backend=fixture.backend
        tick=Mock(side_effect=StorageUnavailable('volume_read_only'))
        backend.card_listener=SimpleNamespace(tick=tick)
        backend.collector.collect=Mock(return_value='collected')
        self.assertEqual(backend.collect(),'collected')
        self.assertEqual(fixture.store.get('storage_wait')['reason'],'volume_read_only')
        self.assertEqual(fixture.store.document(fixture.mid)['phase'],'original_saved')

    def test_relation_publication_and_reciprocal_links_use_external_library(self):
        fixture=relation_fixture.RelationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        # This fixture has no real intake. Its already-reviewed synthetic notes
        # exercise the production reading publisher and relation writer.
        runtime=fixture.profile.runtime
        runtime.check_storage=self.f.store.check
        runtime.path=lambda relative:self.f.store.path(relative)
        runtime.ensure_directory=self.f.store.ensure_directory
        fixture.profile.root=self.f.store.path('reading-library')
        fixture.profile.config['root']=str(fixture.profile.root)
        first=fixture.material(1);second=fixture.material(2)
        fixture.publish(fixture.plan(first,second))
        self.assertEqual(len(fixture.graph()['edges']),1)
        for source,other in ((first,second),(second,first)):
            group=fixture.profile.root/source['group']
            text='\n'.join(p.read_text() for p in group.glob('*.md'))
            self.assertIn(other['group'],text)
        self.assertFalse((runtime.root/'reading-library').exists())

    def test_native_plist_probe_reads_roles_and_rejects_time_machine(self):
        mount='/Volumes/Synthetic-KB'
        config=dict(self.f.config,mount_path=mount,root=mount+'/content')
        info=dict(VolumeUUID=config['volume_uuid'],MountPoint=mount,FilesystemType='apfs',
                  Internal=False,WritableMedia=True,WritableVolume=True)
        apfs=dict(Containers=[dict(Volumes=[dict(APFSVolumeUUID=config['volume_uuid'],Roles=['Backup'])])])
        results=[SimpleNamespace(stdout=plistlib.dumps(v)) for v in (info,apfs)]
        with patch('kb_custody_v21.storage_layout.os.path.ismount',return_value=True),patch('kb_custody_v21.storage_layout.subprocess.run',side_effect=results) as run,patch('kb_custody_v21.storage_layout.shutil.disk_usage',return_value=SimpleNamespace(free=40*1024**3)):
            facts=mac_volume(config)
        self.assertEqual(facts['roles'],['Backup'])
        self.assertEqual(run.call_args_list[1].args[0],['/usr/sbin/diskutil','apfs','list','-plist'])
        with self.assertRaisesRegex(StorageUnavailable,'ordinary_external_apfs_required'):
            ContentStore(config,probe=lambda _:facts).volume()
        results=[SimpleNamespace(stdout=plistlib.dumps(v)) for v in (info,apfs)]
        with patch('kb_custody_v21.storage_layout.os.path.ismount',return_value=True),patch('kb_custody_v21.storage_layout.subprocess.run',side_effect=results),patch('kb_custody_v21.storage_layout.shutil.disk_usage',side_effect=FileNotFoundError('synthetic disconnect')):
            with self.assertRaisesRegex(StorageUnavailable,'volume_unavailable'):mac_volume(config)


if __name__=='__main__':unittest.main()
