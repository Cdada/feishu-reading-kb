"""QA and archive generation share one lane while confirmations stay live."""
from types import SimpleNamespace
import threading
import unittest

from kb_custody_v21.daily_store import Deferred
import test_daily_scheduler as fixture


class QASchedulingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchedulerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_qa_work_does_not_start_a_second_heavy_thread_or_block_confirmations(self):
        f=self.f;started=threading.Event();release=threading.Event()
        def step():
            started.set();release.wait(3)
        f.adapter.qa=SimpleNamespace(pending=lambda:True,step=step)
        self.addCleanup(release.set)
        f.scheduler.tick();self.assertTrue(started.wait(1))
        self.assertFalse(f.adapter.heavy_started.is_set())
        f.confirmed();f.mono+=20;f.scheduler.tick()
        self.assertEqual(f.store.document('om_confirmed')['phase'],'published')
        self.assertFalse(f.adapter.heavy_started.is_set())
        release.set();f.scheduler.future.result(timeout=2)
        f.adapter.release_heavy.set();f.scheduler.tick();f.scheduler.future.result(timeout=2)
        self.assertTrue(f.adapter.heavy_started.is_set())
        self.assertEqual(f.adapter.maximum,1)

    def test_resource_pause_delays_qa_without_stopping_collection(self):
        f=self.f
        def step():raise Deferred('paused_resources')
        f.adapter.qa=SimpleNamespace(pending=lambda:True,step=step)
        f.scheduler.tick();f.scheduler.future.result(timeout=2)
        self.assertEqual(f.store.get('qa-wait')['reason'],'paused_resources')
        f.adapter.release_heavy.set();f.mono+=20;f.scheduler.tick();f.scheduler.future.result(timeout=2)
        self.assertEqual(f.adapter.collected,2)
