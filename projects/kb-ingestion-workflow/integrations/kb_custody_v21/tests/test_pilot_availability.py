import copy
from datetime import timedelta
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.notifications import PersistentMockSender
from kb_custody_v21.pilot_availability import PilotServiceNotices, collection_report
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
import test_custody as custody
import test_pilot_feishu as platform


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pilot-availability-', dir=custody.BASE/'test-output')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(custody.WORKTREE, DEFAULTS, Path(self.temp.name))
        self.registry = Registry(self.runtime)
        self.scope = PilotScope('cli_synthetic', 'oc_synthetic', 'ou_synthetic')
        self.intake = PilotIntake(self.registry, self.scope, max_attachments=2, max_pending=8)
        self.notices = PilotServiceNotices(self.intake)
        self.ready = Mock(return_value=True)
        self.access = Mock()
        self.sender = PersistentMockSender(self.runtime)

    def plan(self, phase='ready'):
        return self.notices.prepare('synthetic-run', phase, checked_state=self.ready)

    def send(self, plan):
        return self.notices.dispatch(plan['notification_id'], self.sender,
                                    checked_access=self.access, checked_state=self.ready)

    def test_readiness_required_before_any_ready_notice_is_prepared(self):
        self.ready.return_value = False
        with self.assertRaises(ContractError): self.plan()
        self.assertEqual(self.notices._events(), [])
        self.assertEqual(self.sender.records('calls'), [])

    def test_repeat_plan_and_restart_do_not_send_twice_or_touch_asset_outbox(self):
        plan = self.plan()
        self.assertEqual(self.plan(), plan)
        self.assertLessEqual(len(plan['provider_request_key']), 50)
        result = self.send(plan)
        self.notices = PilotServiceNotices(self.intake)
        self.assertEqual(self.send(self.plan()), result)
        self.assertEqual(result['delivery_state'], 'delivered')
        self.assertEqual(len(self.sender.records('calls')), 1)
        self.assertEqual(self.sender.records('deliveries')[0]['target_chat_id'], self.scope.chat_id)
        self.assertFalse(self.registry.rows('SELECT * FROM notification_outbox'))
        self.assertFalse(self.registry.rows('SELECT * FROM intakes'))

    def test_stopping_supersedes_unsent_ready_without_claiming_stopped(self):
        old = self.plan()
        stop = self.plan('pausing')
        with self.assertRaises(ContractError): self.send(old)
        self.assertIn('即将暂停', stop['payload'])
        self.assertEqual(self.send(stop)['delivery_state'], 'delivered')
        self.assertEqual(len(self.sender.records('calls')), 1)

    def test_phase_change_and_expiry_during_readiness_check_send_nothing(self):
        plan = self.plan()
        self.ready.return_value = False
        with self.assertRaises(ContractError): self.send(plan)
        self.ready.return_value = True
        self.access.side_effect = [None, ContractError('synthetic expired window')]
        with self.assertRaises(ContractError): self.send(plan)
        self.assertEqual(self.sender.records('calls'), [])

    def test_lost_response_stays_unknown_without_lookup_or_new_key(self):
        self.sender = PersistentMockSender(self.runtime, mode='success_lost_response')
        plan = self.plan('pausing')
        self.assertEqual(self.send(plan)['delivery_state'], 'delivery_unknown')
        self.notices = PilotServiceNotices(self.intake)
        self.send(self.plan('pausing'))
        self.assertEqual(len(self.sender.records('calls')), 1)
        self.assertEqual(len(self.sender.records('deliveries')), 1)

    def test_process_exit_after_remote_acceptance_preserves_intent_and_never_resends(self):
        plan = self.plan()
        child = subprocess.run([sys.executable, str(Path(__file__).with_name('pilot_availability_crash_worker.py')),
                                str(self.runtime.root), plan['notification_id']],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 77, child.stderr)
        self.notices = PilotServiceNotices(self.intake)
        self.assertEqual(self.send(plan)['delivery_state'], 'delivery_unknown')
        self.assertEqual(len(self.sender.records('calls')), 1)

    def test_retryable_failure_is_recorded_but_not_automatically_retried(self):
        self.sender = PersistentMockSender(self.runtime, mode='retryable_failure')
        plan = self.plan('pausing')
        self.assertEqual(self.send(plan)['delivery_state'], 'failed_no_retry')
        self.send(plan)
        self.assertEqual(len(self.sender.records('calls')), 1)

    def test_final_access_failure_preserves_remote_success_as_unknown(self):
        plan = self.plan()
        self.access.side_effect = [None, None, ContractError('source changed')]
        self.assertEqual(self.send(plan)['delivery_state'], 'delivery_unknown')
        self.assertEqual(len(self.sender.records('deliveries')), 1)
        self.send(plan)
        self.assertEqual(len(self.sender.records('calls')), 1)

    def test_stopped_run_cannot_create_new_ready_notice(self):
        self.plan('pausing')
        with self.assertRaises(ContractError): self.plan()


class CollectionReportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = platform.PilotFeishuTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = Runtime(custody.WORKTREE, DEFAULTS, self.fixture.root.parent/'local-custody')
        self.registry = Registry(self.runtime)
        self.scope = PilotScope('cli_synthetic', 'oc_synthetic', 'ou_synthetic')
        self.intake = PilotIntake(self.registry, self.scope, max_attachments=2, max_pending=8)
        self.body = '# Synthetic original\n\nLocal-only text.\n'.encode()
        self.message = dict(appId=self.scope.app_id, chatId=self.scope.chat_id, senderId=self.scope.user_id,
            chatType='group', messageId='om_synthetic', resources=[dict(type='file', fileKey='file_synthetic',
                fileName='synthetic.md', sizeBytes=len(self.body))])

    def scan(self):
        s = self.fixture.make()
        s.verify_isolation()
        self.fixture.scan(s)
        return s

    def report(self, session):
        before = self.fixture.runner.call_count
        result = collection_report(session, self.intake, 'scan_synthetic')
        self.assertEqual(self.fixture.runner.call_count, before)
        return result

    def test_complete_query_is_not_acceptance_and_accepted_queue_needs_no_resend(self):
        session = self.scan()
        result = self.report(session)
        self.assertTrue(result['query_complete'])
        self.assertFalse(result['all_observed_files_accepted'])
        self.assertEqual(result['materials'][0]['status'], 'NOT_CONFIRMED')
        attachment = MockAttachment(self.body)
        self.intake.receive(self.message, 0, attachment.download)
        result = self.report(session)
        self.assertEqual(result['materials'][0]['status'], 'ACCEPTED')
        self.assertEqual(result['materials'][0]['processing_state'], 'waiting_conversion')
        self.assertIn('无需重发', result['materials'][0]['action'])
        self.intake.receive(self.message, 0, attachment.download)
        self.assertEqual(attachment.download_calls, 1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_conversion_jobs')), 1)

    def test_empty_truncated_query_still_reports_entire_unchecked_interval(self):
        self.fixture.pages = [dict(items=[], has_more=True, page_token='synthetic-more')]
        result = self.report(self.scan())
        self.assertEqual(result['materials'], [])
        self.assertFalse(result['query_complete'])
        self.assertEqual(result['unchecked_interval'], result['interval'])
        self.assertIn('无法列出完全未观察到', result['notice'])

    def test_same_filename_keeps_distinct_observed_messages_and_does_not_invent_missing_names(self):
        other = copy.deepcopy(self.fixture.message)
        other['message_id'] = 'om_synthetic_other'
        self.fixture.pages = [dict(items=[self.fixture.message, other], has_more=True, page_token='more')]
        result = self.report(self.scan())
        self.assertEqual(len(result['materials']), 2)
        self.assertEqual(len({r['message_id'] for r in result['materials']}), 2)
        self.assertFalse(result['query_complete'])
        self.assertTrue(all(r['filename']=='synthetic.md' for r in result['materials']))

    def test_failed_original_requests_resubmission_but_integrity_hold_does_not_revoke_acceptance(self):
        session = self.scan()
        with self.assertRaises(OSError):
            self.intake.receive(self.message, 0, MockAttachment(self.body, available=False).download)
        self.assertEqual(self.report(session)['materials'][0]['status'], 'NEEDS_RESUBMISSION')
        eid = self.intake.receive(self.message, 0, MockAttachment(self.body).download)['entry_id']
        self.registry.health(eid, 'suspect', 'synthetic integrity test')
        result = self.report(session)
        self.assertEqual(result['materials'][0]['status'], 'INTEGRITY_HOLD')
        self.assertEqual(self.registry.receipt(eid)['source_staging_status'], 'accepted')

    def test_receipt_tampering_and_scope_change_are_rejected_without_network(self):
        session = self.scan()
        self.intake.scope = PilotScope('cli_other', self.scope.chat_id, self.scope.user_id)
        with self.assertRaises(ContractError): self.report(session)
        self.intake.scope = self.scope
        scan = session.value('scan_synthetic:result')
        Path(next(iter(scan['receipts']))).write_text('{}')
        with self.assertRaises(ContractError): self.report(session)

    def test_deleted_known_message_can_request_resubmission_without_assuming_unknown_messages(self):
        self.fixture.pages[0]['items'][0]['deleted'] = True
        result = self.report(self.scan())
        self.assertEqual(result['materials'][0]['status'], 'NEEDS_RESUBMISSION')
        self.assertEqual(len(result['materials']), 1)

    def test_unfinished_scan_does_not_become_empty_success(self):
        session = self.fixture.make()
        with self.assertRaises(ContractError): self.report(session)

    def test_receipt_changes_during_custody_lookup_cannot_finish_report(self):
        session = self.scan()
        self.intake.receive(self.message, 0, MockAttachment(self.body).download)
        status = self.intake.status
        receipt = Path(next(iter(session.value('scan_synthetic:result')['receipts'])))
        def altered(eid):
            result = status(eid)
            receipt.write_text('{}')
            return result
        self.intake.status = altered
        with self.assertRaises(ContractError): self.report(session)
