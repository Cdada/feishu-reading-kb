"""Bounded natural sampling using synthetic queue items and controlled clocks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kb_custody_v21.archive_timing import ArchiveTiming, PROFILE, CURRENT, measure, observed, stage, report, markdown
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import DailyStore, Deferred
from test_daily_store import policy, message


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name).resolve()/'queue.sqlite3'
        self.wall=1000000.;self.mono=0.
        self.store=DailyStore(self.path,policy(),clock=lambda:self.wall)

    def enable(self,version='a'):
        self.timing=ArchiveTiming(self.store,dict(source_commit=version*40),monotonic=lambda:self.mono)
        self.store.timing=self.timing
        return self.timing

    def receive(self,*items):
        scan=self.store.scan('chat',initial_start=0)
        self.store.save_page(scan,list(items),has_more=False)

    def tick(self,seconds):
        self.mono+=seconds;self.wall+=seconds

    def sample(self,n=0):
        return report(self.path)['samples'][n]

    def test_only_four_new_tasks_restart_failure_and_duplicates_do_not_replenish(self):
        self.receive(message('old'))
        self.assertEqual(report(self.path)['state'],'not_enabled')
        self.enable();self.receive(message('old'),message(1),message(2))
        self.store.advance('om_1','registered','held',reason='synthetic failure')
        self.store=DailyStore(self.path,policy(),clock=lambda:self.wall)
        self.enable('b');self.timing.recover()
        self.receive(message(2),message(3),message(4),message(5))
        value=report(self.path)
        self.assertEqual([s['ordinal'] for s in value['samples']],[1,2,3,4])
        self.assertEqual(value['samples'][0]['phase'],'held')
        self.assertEqual(value['state'],'sample_limit_reached')
        self.assertEqual(self.store.rows('SELECT * FROM daily_requests'),[])
        self.assertEqual(self.store.rows('SELECT * FROM daily_models'),[])
        # A legacy reader can still use the same queue without enabling timing.
        legacy=DailyStore(self.path,policy(),clock=lambda:self.wall)
        self.assertIsNone(legacy.timing)
        self.assertEqual(legacy.document('om_5')['phase'],'registered')

    def test_invalid_page_rolls_back_sample_selection(self):
        self.enable()
        bad=message(2);bad['chat_id']='oc_wrong'
        with self.assertRaises(ContractError):self.receive(message(1),bad)
        self.assertEqual(report(self.path)['samples'],[])
        self.receive(message(1))
        self.assertEqual(self.sample()['ordinal'],1)

    def test_monotonic_nested_checks_errors_and_versions_do_not_copy_sensitive_text(self):
        self.enable();self.receive(message(1))
        with self.assertRaisesRegex(Deferred,'private content'):
            with self.timing.step('om_1','original_saved'):
                stage('conversion')
                with measure('resources','check'):
                    self.tick(2)
                with measure('mineru','parser'):
                    self.tick(5)
                    observed('pdf',dict(total_pages=10,completed_pages=1))
                    raise Deferred('private content')
        self.assertIsNone(CURRENT.get())
        value=self.sample()
        self.assertEqual(value['phase_totals']['conversion']['measured_seconds'],7)
        self.assertEqual(value['check_and_step_totals']['check:resources']['seconds'],2)
        self.assertEqual(value['segments'][0]['outcome'],'Deferred')
        page=next(m for m in value['timeline'] if m['kind']=='pdf')
        self.assertEqual(page['details']['segment_id'],value['segments'][0]['segment_id'])
        self.assertNotIn('private content',json.dumps(value))
        self.enable('b')
        with self.timing.step('om_1','original_saved'):self.tick(3)
        self.assertEqual([s['version']['source_commit'][0] for s in self.sample()['segments']],['a','b'])
        rendered=json.dumps(report(self.path))
        for forbidden in ('om_1','file_1','1.md',str(self.tmp.name)):
            self.assertNotIn(forbidden,rendered)

    def test_interrupted_step_is_unknown_never_elapsed_until_restart(self):
        self.enable();self.receive(message(1))
        with self.store.transaction() as db:
            db.execute("INSERT INTO archive_timing_segments VALUES ('unfinished','om_1','old','conversion',?,NULL,NULL,'started','{}','{}',1)",(self.wall,))
        self.tick(300)
        self.enable();self.timing.recover()
        value=self.sample()
        self.assertIsNone(value['segments'][0]['elapsed_seconds'])
        self.assertEqual(value['segments'][0]['outcome'],'interrupted')
        self.assertEqual(value['phase_totals']['conversion']['incomplete'],1)

    def test_waits_keep_feedback_and_measured_polls_separate(self):
        self.enable();self.receive(message(1))
        self.tick(2);self.timing.feedback('om_1');self.tick(3);self.timing.feedback('om_1')
        for before,after in [('registered','original_saved'),('original_saved','converted'),
            ('converted','generating'),('generating','guide_ready'),('guide_ready','preview_ready'),
            ('preview_ready','awaiting_confirmation')]:
            self.store.advance('om_1',before,after)
        for _ in range(4):
            self.tick(100)
            with self.timing.step('om_1','awaiting_confirmation'):
                with measure('identity','check'):self.tick(1)
        self.store.advance('om_1','awaiting_confirmation','confirmed')
        value=self.sample()
        self.assertEqual(len(value['segments']),1)
        self.assertEqual(value['segments'][0]['attempts'],4)
        self.assertEqual(value['segments'][0]['elapsed_seconds'],4)
        self.assertEqual(value['waiting_intervals'][0]['wall_seconds'],404)
        self.assertEqual(value['first_feedback_from_registration_seconds'],2)
        self.assertIsNone(value['total_elapsed_seconds'])
        self.assertIn('不得相加',markdown(report(self.path)))

    def test_resource_pauses_and_service_stops_are_explicit_and_bounded(self):
        self.enable();self.receive(message(1))
        self.store.put('defer:om_1',dict(reason='paused_resources'))
        self.tick(10)
        self.store.put('defer:om_1',dict(reason='paused_resources'))
        self.tick(10)
        with self.timing.step('om_1','registered'):self.tick(1)
        self.store.put('heartbeat',dict(state='stopped',detail='secret'))
        self.tick(100);self.store.put('heartbeat',dict(state='running'))
        value=self.sample()
        self.assertEqual([w['kind'] for w in value['waiting_intervals']],['resources','service_stopped'])
        self.assertEqual([w['wall_seconds'] for w in value['waiting_intervals']],[20,100])
        self.assertEqual(sum(m['kind']=='deferred' for m in value['timeline']),1)
        self.store.put('defer:om_1',dict(reason='paused_resources'))
        self.assertEqual(sum(m['kind']=='deferred' for m in self.sample()['timeline']),2)
        self.assertNotIn('secret',json.dumps(self.sample()))

    def test_bounds_are_observable_and_do_not_reset_the_queue(self):
        self.enable();self.receive(message(1))
        with patch('kb_custody_v21.archive_timing.MAX_MARKS',2):
            for _ in range(3):self.timing.mark('om_1','pdf',dict(total_pages=10))
        with patch('kb_custody_v21.archive_timing.MAX_SEGMENTS',1):
            for _ in range(3):
                with self.timing.step('om_1','registered'):self.tick(1)
        self.assertTrue(self.sample()['truncated'])
        self.assertEqual(len(self.sample()['segments']),1)
        self.assertEqual(self.store.document('om_1')['phase'],'registered')

    def test_report_missing_path_does_not_create_database(self):
        missing=self.path.parent/'missing'/'queue.sqlite3'
        self.assertEqual(report(missing),dict(state='not_enabled',samples=[]))
        self.assertFalse(missing.parent.exists())

    def test_characteristics_validate_without_exposing_filename(self):
        self.enable();item=message(1);item['create_time']='invalid';self.receive(item)
        self.timing.characteristics('om_1',size=123,pages=12)
        value=self.sample()
        self.assertEqual((value['media_kind'],value['size_bytes'],value['total_pages']),('.md',123,12))
        self.assertIsNone(value['source_time'])
        with self.assertRaises(ContractError):self.timing.characteristics('om_1',pages=400)


if __name__=='__main__':unittest.main()
