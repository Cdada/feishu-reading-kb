"""Restart, pagination and budget tests; all materials and identities synthetic."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_store import DailyStore, Deferred


def policy(**kw):
    return DailyPolicy('cli_synthetic', 'oc_synthetic', 'ou_owner', 'ou_bot', 8, **kw)


def message(n, *, kind='file', text='确认入库'):
    return dict(message_id='om_' + str(n), chat_id='oc_synthetic', msg_type=kind,
                sender=dict(id='ou_owner', id_type='open_id', sender_type='user'),
                deleted=False, updated=False, create_time='1000000',
                body=dict(content=json.dumps(dict(file_key='file_' + str(n), file_name=str(n)+'.md')
                                             if kind=='file' else dict(text=text))))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'daily.sqlite3'
        self.time = 1_000_000.0
        self.policy = policy()
        self.db = self.reopen()

    def reopen(self):
        return DailyStore(self.path, self.policy, clock=lambda: self.time)

    def receive(self, *items):
        scan = self.db.scan('chat', initial_start=self.time-60)
        self.db.save_page(scan, list(items), has_more=False)

    def test_queue_more_than_three_survives_process_restart(self):
        self.receive(*(message(n) for n in range(8)))
        self.db = self.reopen()
        self.assertEqual(self.db.summary()['continuing_count'], 8)
        self.assertEqual(self.db.document('om_7')['phase'], 'registered')

    def test_pagination_restart_preserves_fixed_interval_until_complete(self):
        first = self.db.scan('chat', initial_start=self.time-60)
        self.db.save_page(first, [message(1)], has_more=True, next_token='page2')
        self.time += 120
        self.db = self.reopen()
        second = self.db.scan('chat', initial_start=self.time-1)
        self.assertEqual(second['end'], first['end'])
        self.assertEqual(second['completed'], first['completed'])
        self.assertEqual(second['token'], 'page2')
        self.db.save_page(second, [message(1), message(2)], has_more=False)
        next_scan = self.db.scan('chat', initial_start=0)
        self.assertEqual(next_scan['completed'], first['end'])
        self.assertEqual(next_scan['start'], first['end']-2)
        self.assertEqual(self.db.summary()['continuing_count'], 2)

    def test_bad_page_rolls_back_entire_page_and_cursor(self):
        scan = self.db.scan('chat', initial_start=self.time-60)
        wrong = message(2)
        wrong['chat_id'] = 'oc_other'
        with self.assertRaises(ContractError):
            self.db.save_page(scan, [message(1), wrong], has_more=False)
        self.assertEqual(self.db.scan('chat', initial_start=0), scan)
        self.assertEqual(self.db.summary()['continuing_count'], 0)

    def test_queue_full_does_not_advance_past_unregistered_files(self):
        self.policy = replace(self.policy, max_pending=1)
        self.db = self.reopen()
        scan = self.db.scan('chat', initial_start=self.time-60)
        with self.assertRaises(Deferred):
            self.db.save_page(scan, [message(1), message(2)], has_more=False)
        self.assertEqual(self.db.scan('chat', initial_start=0), scan)
        self.assertEqual(self.db.summary()['continuing_count'], 1)

    def test_old_history_gap_is_visible_and_not_claimed_as_covered(self):
        scan = self.db.scan('chat', initial_start=1)
        self.assertEqual(scan['gap_start'], 1)
        self.assertEqual(scan['gap_end'], self.time-7*86400)
        self.db.save_page(scan, [], has_more=False)
        self.assertEqual(len(self.db.summary()['history_gaps']), 1)

    def test_stale_result_cannot_overwrite_new_cursor(self):
        scan = self.db.scan('chat', initial_start=self.time-60)
        self.db.save_page(scan, [], has_more=True, next_token='next')
        with self.assertRaises(ContractError):
            self.db.save_page(scan, [message(1)], has_more=False)

    def test_wrong_sender_is_not_registered_and_edited_replies_are_retained(self):
        other = message(1)
        other['sender']['id'] = 'ou_other'
        self.receive(other, message(2, kind='text', text='@确认入库'))
        corrected = message(2, kind='text')
        corrected['updated'] = True
        self.receive(corrected)
        self.assertEqual(self.db.summary()['continuing_count'], 0)
        self.assertEqual(len(self.db.rows('SELECT * FROM daily_messages')), 2)

    def test_file_identity_change_rejected_without_losing_original(self):
        self.receive(message(1))
        changed = message(1)
        changed['body']['content'] = json.dumps(dict(file_key='file_replaced', file_name='1.md'))
        with self.assertRaises(ContractError):
            self.receive(changed)
        self.assertEqual(self.db.document('om_1')['data']['resource']['file_key'], 'file_1')

    def test_resubmission_of_same_bytes_links_existing_document(self):
        self.receive(message(1), message(2))
        for mid in ('om_1', 'om_2'):
            self.db.advance(mid, 'registered', 'original_saved')
        self.assertIsNone(self.db.bind_source('om_1', 'a'*64))
        self.assertEqual(self.db.bind_source('om_2', 'a'*64), 'om_1')
        self.db.advance('om_2', 'original_saved', 'duplicate')
        self.assertEqual(self.db.summary()['continuing_count'], 1)

    def test_unfinished_call_cannot_retry_after_restart_without_evidence(self):
        self.db.operation('download', 'read', dict(file='synthetic'))
        self.db.begin('download', physical_requests=2)
        self.db = self.reopen()
        with self.assertRaises(Deferred):
            self.db.begin('download', physical_requests=2)
        with self.assertRaises(ContractError):
            self.db.resolve_retry('download', dict(safe_to_retry=True))
        self.db.resolve_retry('download', dict(safe_to_retry=True, receipts=['checked-incomplete-read']))
        self.assertEqual(self.db.begin('download', physical_requests=2), 2)

    def test_fifteen_attempt_limit_and_delay_survive_restart(self):
        self.db.operation('list', 'read', {})
        for attempt in range(1, 16):
            self.assertEqual(self.db.begin('list', physical_requests=2), attempt)
            self.db.fail('list', attempt, 'offline', temporary=True)
            self.db = self.reopen()
            with self.assertRaises(Deferred):
                self.db.begin('list', physical_requests=2)
            self.time += self.policy.retry_delay(attempt)
        with self.assertRaises(Deferred):
            self.db.begin('list', physical_requests=2)
        self.assertEqual(self.db.rows('SELECT sum(count) AS n FROM daily_requests')[0]['n'], 30)

    def test_platform_backoff_and_shared_network_pause(self):
        for key in ('one', 'two'):
            self.db.operation(key, 'read', {})
        self.db.begin('one', physical_requests=2)
        self.db.fail('one', 1, 'rate_limited', temporary=True, retry_after=600, global_pause=True)
        self.time += 599
        with self.assertRaises(Deferred):
            self.db.begin('two', physical_requests=2)
        self.time += 1
        self.assertEqual(self.db.begin('two', physical_requests=2), 1)

    def test_message_unknown_does_not_become_automatic_retry(self):
        self.db.operation('pdf', 'send', {'uuid': 'same-id'})
        self.db.begin('pdf', physical_requests=2, kind='send')
        self.db.fail('pdf', 1, 'timeout', uncertain=True)
        self.time += 3601
        with self.assertRaises(Deferred):
            self.db.begin('pdf', physical_requests=2)
        self.db.complete('pdf', 1, dict(message_id='om_recovered'))
        self.assertEqual(self.db.operation('pdf', 'send', {'uuid': 'same-id'})['state'], 'complete')

    def test_concurrent_workers_cannot_double_reserve_same_operation(self):
        self.db.operation('same', 'read', {})
        def attempt():
            try:
                return self.db.begin('same', physical_requests=2)
            except Deferred:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(x for x in values if x is not None), [1])

    def test_minute_cap_survives_reopening_database(self):
        self.db.operation('first', 'read', {})
        self.db.begin('first', physical_requests=60)
        self.db = self.reopen()
        self.db.operation('next', 'read', {})
        with self.assertRaises(Deferred):
            self.db.begin('next', physical_requests=1)
        self.time += 60
        self.assertEqual(self.db.begin('next', physical_requests=1), 1)

    def test_model_quota_is_cumulative_and_policy_topup_preserves_usage(self):
        self.policy = replace(self.policy, model_turn_limit=1)
        self.db = self.reopen()
        self.db.reserve_model('a'*64)
        self.time += 86400
        self.db = self.reopen()
        with self.assertRaises(Deferred):
            self.db.reserve_model('b'*64)
        self.policy = replace(self.policy, model_turn_limit=2)
        self.db = self.reopen()
        self.assertEqual(self.db.reserve_model('b'*64), 1)
        self.assertEqual(self.db.summary()['model_turns'], 2)

    def test_only_one_extra_model_and_old_late_result_cannot_win(self):
        self.db.reserve_model('a'*64)
        self.db.model_result('a'*64, 1, 'unknown', dict(receipt='timeout'))
        evidence = dict(checked_saved_results=True, old_attempt_fenced=True, receipts=['reconciled'])
        self.db.allow_model_recovery('a'*64, evidence)
        self.assertFalse(self.db.model_result('a'*64, 1, 'validated', dict(receipt='late')))
        self.assertEqual(self.db.reserve_model('a'*64), 2)
        self.db.model_result('a'*64, 2, 'unknown', {})
        with self.assertRaises(ContractError):
            self.db.allow_model_recovery('a'*64, evidence)
        with self.assertRaises(Deferred):
            self.db.reserve_model('a'*64)

    def test_summary_preserves_publication_if_notification_not_finished(self):
        self.receive(message(1))
        path = ['registered', 'original_saved', 'converted', 'generating', 'guide_ready',
                'preview_ready', 'awaiting_confirmation', 'confirmed', 'published']
        for a,b in zip(path, path[1:]):
            self.db.advance('om_1', a, b)
        summary = self.reopen().summary()
        self.assertEqual(summary['last_published'], '1.md')
        self.assertEqual(summary['pending_notifications'], 1)
        self.assertEqual(summary['continuing_count'], 0)

    def test_changing_scope_or_rolling_clock_back_rejected(self):
        with self.assertRaises(ContractError):
            DailyStore(self.path, replace(self.policy, account_id='ou_another'), clock=lambda:self.time)
        self.time -= 1
        with self.assertRaises(ContractError):
            self.reopen()

    def test_missing_ledger_beside_request_evidence_does_not_reset_quota(self):
        (self.path.parent/'requests').mkdir()
        with self.assertRaisesRegex(ContractError,'refuse to reset'):
            DailyStore(self.path.parent/'missing.sqlite3',self.policy,clock=lambda:self.time)

    def test_policy_rejects_paid_calls_and_boolean_budgets(self):
        for change in (dict(paid_model_cny=1), dict(model_turn_limit=True), dict(retry_attempts=16)):
            with self.subTest(change=change), self.assertRaises(ContractError):
                replace(self.policy, **change)


if __name__ == '__main__':
    unittest.main()
