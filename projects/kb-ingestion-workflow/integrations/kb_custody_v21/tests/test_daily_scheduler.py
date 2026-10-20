"""Slow heavy work must not block receiving files or publishing confirmed ones."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from kb_custody_v21.daily_scheduler import DailyScheduler, MaterialProblem
from kb_custody_v21.daily_store import DailyStore, Deferred
from test_daily_store import policy, message


class Adapter:
    def __init__(self, store):
        self.store = store
        self.collected = 0
        self.heavy_started = threading.Event()
        self.release_heavy = threading.Event()
        self.light = []
        self.maximum = self.active = 0
        self.fail = None

    def check_authorization(self):
        pass

    def collect(self):
        self.collected += 1
        scan = self.store.scan('chat', initial_start=self.store.now()-60)
        self.store.save_page(scan, [message(self.collected)], has_more=False)

    def report_status(self):
        pass

    def recover(self):
        pass

    def advance_heavy(self, document):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.heavy_started.set()
        try:
            self.release_heavy.wait(5)
            if self.fail:
                raise self.fail
            self.store.advance(document['message_id'], 'registered', 'original_saved')
        finally:
            self.active -= 1

    def advance_light(self, document):
        self.light.append(document['message_id'])
        if document['phase']=='confirmed':
            self.store.advance(document['message_id'], 'confirmed', 'published')

    def request_stop(self):
        self.release_heavy.set()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DailyStore(Path(self.tmp.name).resolve()/'queue.sqlite3', policy())
        self.adapter = Adapter(self.store)
        self.mono = 0
        self.scheduler = DailyScheduler(self.store, self.adapter, monotonic=lambda:self.mono)
        self.addCleanup(self.scheduler.close)

    def confirmed(self):
        scan = self.store.scan('chat', initial_start=self.store.now()-60)
        self.store.save_page(scan, [message('confirmed')], has_more=False)
        path = ['registered','original_saved','converted','generating','guide_ready',
                'preview_ready','awaiting_confirmation','confirmed']
        for a,b in zip(path, path[1:]):
            self.store.advance('om_confirmed', a,b)

    def test_collect_and_publish_while_generation_is_blocked(self):
        self.scheduler.tick()
        self.assertTrue(self.adapter.heavy_started.wait(1))
        self.confirmed()
        self.mono += 20
        self.scheduler.tick()
        self.assertEqual(self.adapter.collected, 2)
        self.assertEqual(self.store.document('om_2')['phase'], 'registered')
        self.assertEqual(self.store.document('om_confirmed')['phase'], 'published')
        self.assertEqual(self.adapter.maximum, 1)

    def test_file_problem_isolated_and_next_document_runs(self):
        self.adapter.fail = MaterialProblem('synthetic corrupt document')
        self.adapter.release_heavy.set()
        self.scheduler.tick()
        self.scheduler.future.result(timeout=2)
        self.mono += 20
        self.scheduler.tick()
        self.scheduler.future.result(timeout=2)
        self.assertEqual(self.store.document('om_1')['phase'], 'held')
        self.assertEqual(self.store.document('om_2')['phase'], 'held')
        self.assertEqual(self.adapter.maximum, 1)

    def test_model_budget_pause_still_checks_owner_reply(self):
        self.adapter.fail = Deferred('model_limit')
        self.adapter.release_heavy.set()
        self.scheduler.tick()
        self.scheduler.future.result(timeout=2)
        self.confirmed()
        self.scheduler.tick()
        self.assertEqual(self.store.document('om_confirmed')['phase'], 'published')
        self.assertEqual(self.store.get('defer:om_1')['reason'], 'model_limit')

    def test_explicit_final_report_does_not_collect_or_start_work(self):
        calls=[]
        self.adapter.report_status=lambda:calls.append('report')
        self.assertTrue(self.scheduler.report_status())
        self.assertEqual(calls,['report'])
        self.assertEqual(self.adapter.collected,0)
        self.assertIsNone(self.scheduler.future)

    def test_final_report_failure_is_recorded_once_without_retrying(self):
        calls=[]
        def deferred():
            calls.append('report')
            raise Deferred('synthetic unavailable',12345)
        self.adapter.report_status=deferred
        self.assertFalse(self.scheduler.report_status())
        self.assertEqual(calls,['report'])
        self.assertEqual(self.store.get('report_wait')['reason'],'synthetic unavailable')
        self.adapter.report_status=lambda:None
        self.assertTrue(self.scheduler.report_status())
        self.assertIsNone(self.store.get('report_wait'))

    def test_stop_does_not_start_another_worker(self):
        self.scheduler.tick()
        self.assertTrue(self.adapter.heavy_started.wait(1))
        self.store.put('stop_requested', True)
        self.assertFalse(self.scheduler.tick())
        self.scheduler.close()
        self.assertEqual(self.adapter.collected, 1)
        self.assertEqual(self.store.get('heartbeat')['state'], 'stopped')
        self.assertEqual(self.store.document('om_1')['phase'], 'original_saved')


if __name__ == '__main__':
    unittest.main()
