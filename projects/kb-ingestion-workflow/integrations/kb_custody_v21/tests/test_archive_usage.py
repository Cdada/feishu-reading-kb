"""Synthetic local accounting: no live requests or model execution."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from unittest import TestCase
import test_daily_store as fixture
from kb_custody_v21.archive_usage import document_usage, snapshot, settle, OWNER
from kb_custody_v21.daily_store import Deferred

class UsageTests(TestCase):
    def setUp(self):
        self.f=fixture.StoreTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.receive(fixture.message(1),fixture.message(2));self.store=self.f.db

    def request(self,key,kind='message',owner=None,fail=False):
        with document_usage(owner):
            self.store.operation(key,'read',{})
            attempt=self.store.begin(key,physical_requests=2,kind=kind)
            if fail:self.store.fail(key,attempt,'synthetic',uncertain=True)
            else:self.store.complete(key,attempt,{})

    def view(self):
        with self.store.connection() as db:
            return snapshot(db,self.store.now(),asdict(self.store.policy))

    def test_failed_attempts_count_and_shared_requests_are_not_assigned(self):
        self.request('old');self.request('scan','list','om_1')
        self.request('identity','identity','om_1')
        self.request('download','download','om_1',True)
        self.request('card','send','om_1')
        v=self.view()
        self.assertEqual((v['reserved_24h'],v['shared_or_historical_24h']),(10,6))
        self.assertEqual(v['documents'][0]['reserved_requests'],4)
        self.assertEqual(v['documents'][0]['attempts'],2)
        self.assertFalse(v['near_limit']);self.assertIsNone(OWNER.get())

    def test_concurrent_owners_do_not_leak_and_restart_retains_limits(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n:self.request(str(n),owner='om_'+str(n)),[1,2]))
        before=self.store.rows('SELECT * FROM daily_requests')
        self.store=self.f.reopen()
        self.assertEqual(self.store.rows('SELECT * FROM daily_requests'),before)
        self.assertEqual([d['reserved_requests'] for d in self.view()['documents']],[2,2])
        with document_usage('om_1'):
            with self.assertRaises(ValueError):raise ValueError('synthetic')
        self.assertIsNone(OWNER.get())

    def test_pause_and_completion_replace_one_summary_without_events(self):
        self.request('one',owner='om_1')
        self.store.put('defer:om_1',dict(reason='paused_resources'))
        settle(self.store)
        self.assertEqual(self.store.get('archive-usage:om_1')['paused_reason'],'paused_resources')
        count=len(self.store.rows('SELECT * FROM daily_events'))
        settle(self.store);self.assertEqual(len(self.store.rows('SELECT * FROM daily_events')),count)
        self.store.put('defer:om_1',None)
        self.store.advance('om_1','registered','held',reason='synthetic')
        settle(self.store)
        self.assertEqual(self.store.get('archive-usage:om_1')['phase'],'held')
        self.assertIsNone(self.store.get('archive-usage:om_1')['paused_reason'])

    def test_old_schema_read_only_view_and_rolling_window(self):
        self.request('legacy')
        with self.store.transaction() as db:db.execute('DROP TABLE archive_request_owners')
        self.assertEqual(self.view()['shared_or_historical_24h'],2)
        self.f.time+=86401
        self.assertEqual(self.view()['reserved_24h'],0)
        self.store=self.f.reopen()
        self.assertEqual(self.store.rows('SELECT sum(count) AS n FROM daily_requests')[0]['n'],2)
