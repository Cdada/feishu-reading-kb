"""Admission routing without parsing files or invoking an AI."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from kb_custody_v21.daily_backend import DailyBackend

class RoutingTests(TestCase):
    def test_parse_is_strict_except_bounded_plain_text(self):
        backend=SimpleNamespace(registry=SimpleNamespace(entry=lambda _:dict(size_bytes=1000)))
        for name,wanted in [('paper.pdf','heavy'),('archive.zip','heavy'),('text.md','light'),('text.txt','light')]:
            doc=dict(phase='original_saved',filename=name,entry_id='synthetic')
            self.assertEqual(DailyBackend.resource_workload(backend,doc),wanted)
        backend.registry.entry=lambda _:dict(size_bytes=1024**2+1)
        self.assertEqual(DailyBackend.resource_workload(backend,dict(phase='original_saved',filename='large.md',entry_id='synthetic')),'heavy')
        for phase in ('registered','converted','reading','preview_ready','guide_ready'):
            self.assertEqual(DailyBackend.resource_workload(backend,dict(phase=phase)),'light')

    def test_light_guide_waits_at_strict_renderer_without_repeating_model(self):
        import test_daily_backend as fixture
        from kb_custody_v21.daily_store import Deferred
        from kb_custody_v21.pilot_resources import ResourceSample
        f=fixture.DailyBackendTests();f.setUp();self.addCleanup(f.doCleanups)
        f.backend.advance_heavy(f.store.document(f.mid))
        f.backend.pipeline.resources.probe=lambda _:ResourceSample(10,'warning',10*1024**3)
        with self.assertRaisesRegex(Deferred,'paused_resources'):
            f.backend.advance_heavy(f.store.document(f.mid))
        self.assertEqual(f.g.wire.calls,1)
        self.assertEqual(len(f.registry.rows('SELECT * FROM publications_v21')),0)
        f.backend.pipeline.resources.probe=lambda _:ResourceSample(40,'normal',10*1024**3)
        f.backend.advance_heavy(f.store.document(f.mid))
        self.assertEqual(f.g.wire.calls,1)
        self.assertEqual(len(f.registry.rows('SELECT * FROM publications_v21')),1)
