"""Synthetic update selection, Chinese-time scheduling and interruption recovery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.qa_quality import digest
from kb_custody_v21.qa_regression import snapshot, changes, periods, plan_regression
from kb_custody_v21.qa_regression_store import RegressionStore
from test_qa_quality import fixture

NOW = datetime.fromisoformat('2030-01-06T20:00:00+08:00')
SYSTEM = 'a'*64


def manifest():
    f = fixture()
    return dict(corpus_id='synthetic-garden', sources=[dict(id='garden', canonical_id='garden',
        body_sha256=f['evidence_bindings']['garden'], metadata_sha256='b'*64, critical=False)] +
        [dict(id=str(i), canonical_id=str(i), body_sha256='c'*64, metadata_sha256='d'*64, critical=False) for i in range(100)])


def watermark(current):
    return dict(periods(NOW), update_snapshot=digest(snapshot(current)), system_fingerprint=SYSTEM)


def plan(*, previous=None, current=None, cases=None, **kwargs):
    current = manifest() if current is None else current
    cases = [fixture()] if cases is None else cases
    defaults = dict(now=NOW, enabled=True, system_fingerprint=SYSTEM, core_ids=['plan'])
    defaults.update(kwargs)
    return plan_regression(previous, current, cases, **defaults)


class RegressionPlanningTests(unittest.TestCase):
    def test_initial_baseline_and_default_disabled(self):
        p = plan(enabled=False)
        self.assertEqual(p['state'], 'disabled')
        self.assertEqual(p['new_model_calls'], 0)
        self.assertFalse(p['automatic_model_execution'])
        self.assertEqual(p['max_semantic_calls'], 2)

    def test_unchanged_snapshot_is_quiet(self):
        m = manifest()
        p = plan(previous=m, current=m, last_completed=watermark(m))
        self.assertEqual(p['state'], 'not_due')
        self.assertEqual(p['offline_case_ids'], [])
        self.assertEqual(p['semantic_case_ids'], [])

    def test_weekly_boundary_uses_shanghai_even_for_utc_input(self):
        before = periods(datetime.fromisoformat('2030-01-06T11:59:59+00:00'))
        at = periods(datetime.fromisoformat('2030-01-06T12:00:00+00:00'))
        self.assertNotEqual(before['weekly'], at['weekly'])
        self.assertEqual(at['weekly'], '2030-01-06T20:00:00+08:00')
        self.assertEqual(at['monthly'], at['weekly'])
        with self.assertRaises(ContractError): periods(datetime(2030, 1, 6))

    def test_long_sleep_catches_only_latest_period(self):
        m = manifest()
        old = dict(watermark(m), weekly='2029-01-07T20:00:00+08:00', monthly='2029-01-07T20:00:00+08:00')
        p = plan(previous=m, current=m, last_completed=old)
        self.assertEqual(p['reasons'], ['weekly', 'monthly'])
        self.assertEqual(len(p['periods']), 2)
        self.assertNotIn('monthly', p['offline_completion_watermarks'])

    def test_minor_update_selects_dependencies_plus_core(self):
        old = manifest(); new = deepcopy(old)
        new['sources'][0]['metadata_sha256'] = 'e'*64
        cases = [fixture(), fixture('unrelated')]
        # Remove source dependency from a genuine empty-evidence boundary fixture.
        from kb_custody_v21.qa_test_fixtures import compile_fixture
        cases[1] = compile_fixture(dict(id='unrelated', question='Unknown?', selectors=[], constraints=['No evidence.'],
            conversation=[], expected=dict(answerability='missing_in_fixture', required_claims=['Say missing.'], forbidden_claims=['Invent.'])), {})
        p = plan(previous=old, current=new, cases=cases, last_completed=watermark(old))
        self.assertFalse(p['changes']['major'])
        self.assertEqual(p['offline_case_ids'], ['plan'])
        self.assertEqual(p['stale_fixture_ids'], ['plan'])

    def test_critical_or_large_update_forces_full(self):
        old = manifest(); new = deepcopy(old)
        new['sources'][0]['critical'] = True
        p = plan(previous=old, current=new, cases=[fixture(), fixture('second')], last_completed=watermark(old))
        self.assertTrue(p['changes']['major'])
        self.assertEqual(p['offline_case_ids'], ['plan', 'second'])
        new = deepcopy(old)
        for s in new['sources'][:6]: s['metadata_sha256'] = 'f'*64
        self.assertTrue(changes(old, new)['major'])

    def test_deleted_changed_body_and_new_unmapped_sources_need_review(self):
        old = manifest(); new = deepcopy(old); new['sources'].pop(0)
        p = plan(previous=old, current=new, last_completed=watermark(old))
        self.assertEqual(p['stale_fixture_ids'], ['plan'])
        new = deepcopy(old); new['sources'][0]['body_sha256'] = 'f'*64
        self.assertEqual(plan(previous=old, current=new, last_completed=watermark(old))['stale_fixture_ids'], ['plan'])
        new = deepcopy(old); new['sources'].append(dict(id='new', canonical_id='new', body_sha256='f'*64, metadata_sha256='e'*64, critical=False))
        self.assertEqual(plan(previous=old, current=new, last_completed=watermark(old))['offline_case_ids'], ['plan'])

    def test_source_copies_do_not_count_as_independent_families(self):
        old = manifest(); new = deepcopy(old)
        for i in range(30):
            new['sources'].append(dict(id='copy-'+str(i), canonical_id='garden', body_sha256='f'*64, metadata_sha256='e'*64, critical=False))
        delta = changes(old, new)
        self.assertEqual(delta['changed_families'], 1)
        self.assertFalse(delta['major'])

    def test_source_order_does_not_create_update(self):
        old = manifest(); new = dict(old, sources=list(reversed(old['sources'])))
        self.assertEqual(digest(snapshot(old)), digest(snapshot(new)))
        self.assertEqual(changes(old, new)['changed_ids'], [])

    def test_heldout_not_exposed_without_explicit_selection(self):
        heldout = dict(fixture('held'), split='heldout')
        p = plan(cases=[fixture(), heldout])
        self.assertNotIn('held', p['offline_case_ids'])
        self.assertNotIn('held', p['semantic_case_ids'])
        self.assertIn('held', plan(cases=[fixture(), heldout], include_heldout=True)['offline_case_ids'])

    def test_other_library_and_future_watermark_rejected(self):
        with self.assertRaises(ContractError): changes(manifest(), dict(manifest(), corpus_id='another'))
        with self.assertRaises(ContractError): plan(last_completed={'weekly': '2031-01-01T00:00:00+08:00'})
        with self.assertRaisesRegex(ContractError, 'different library'): plan(cases=[dict(fixture(), corpus_id='another-library')])


class RegressionStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name).resolve()/'regression.sqlite3'
        self.store = RegressionStore(self.path)
        self.plan = plan(cases=[fixture(), fixture('second')])
        self.key = self.store.enqueue(self.plan)

    @staticmethod
    def ok(key):
        return dict(id=key, status='passed', model_calls=0)

    def test_enqueue_deduplicates_and_completed_does_not_repeat(self):
        self.assertEqual(self.store.enqueue(self.plan), self.key)
        first = self.store.run(self.key, self.ok)
        second = self.store.run(self.key, lambda k: self.fail('must not rerun'))
        self.assertEqual(first, second)
        self.assertEqual(first['state'], 'completed')

    def test_disabled_or_tampered_plan_rejected(self):
        with self.assertRaises(ContractError): self.store.enqueue(plan(enabled=False))
        changed = deepcopy(self.plan); changed['offline_case_ids'] = []
        with self.assertRaises(ContractError): self.store.enqueue(changed)

    def test_missing_disk_defers_and_resumes_only_unfinished_case(self):
        def missing(key):
            if key == 'second': raise FileNotFoundError('synthetic absent disk')
            return self.ok(key)
        first = self.store.run(self.key, missing)
        self.assertEqual(first['state'], 'deferred')
        self.assertEqual(len(first['results']), 1)
        self.store = RegressionStore(self.path)
        seen = []
        def resumed(key): seen.append(key); return self.ok(key)
        self.assertEqual(self.store.run(self.key, resumed)['state'], 'completed')
        self.assertEqual(seen, ['second'])

    def test_programmer_error_is_visible_and_not_automatically_retried(self):
        def broken(key): raise ValueError('synthetic programming error')
        with self.assertRaises(ValueError): self.store.run(self.key, broken)
        self.assertEqual(self.store.get(self.key)['reason'], 'ValueError')
        with self.assertRaises(Deferred): self.store.run(self.key, self.ok)
        self.assertEqual(self.store.run(self.key, self.ok, retry_failed=True)['state'], 'completed')

    def test_stale_fixtures_never_use_old_expected_answers(self):
        old = manifest(); new = deepcopy(old); new['sources'][0]['body_sha256'] = 'f'*64
        key = self.store.enqueue(plan(previous=old, current=new))
        result = self.store.run(key, lambda k: self.fail('stale case must not score'))
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['results'][0]['status'], 'needs_review')

    def test_nonzero_model_call_is_not_an_offline_result(self):
        with self.assertRaises(ContractError): self.store.run(self.key, lambda k: dict(id=k, status='passed', model_calls=1))

    def test_only_one_worker_runs_at_a_time(self):
        started, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        def slow(key): started.set(); release.wait(3); return self.ok(key)
        with ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.store.run, self.key, slow)
            self.assertTrue(started.wait(1))
            with self.assertRaises(Deferred): self.store.run(self.key, self.ok)
            release.set(); self.assertEqual(future.result(3)['state'], 'completed')

    def test_notification_is_local_event_and_unchanged_state_quiet(self):
        self.assertIsNone(self.store.notification(self.key))
        self.store.run(self.key, self.ok)
        self.assertEqual(self.store.notification(self.key)['state'], 'completed')
        self.assertIsNone(self.store.notification(self.key))

    def test_unchanged_outcomes_across_new_tasks_stay_quiet(self):
        self.store.run(self.key, self.ok)
        self.assertIsNotNone(self.store.notification(self.key))
        next_plan = plan(cases=[fixture(), fixture('second')], system_fingerprint='f'*64)
        key = self.store.enqueue(next_plan)
        self.store.run(key, self.ok)
        self.assertIsNone(self.store.notification(key))


if __name__ == '__main__':
    unittest.main()
