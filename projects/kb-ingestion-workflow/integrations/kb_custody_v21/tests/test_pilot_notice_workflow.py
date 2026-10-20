"""Business composition reuses the existing custody and physical CLI fixtures."""
import copy
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.notifications import OutboxWorker, PersistentMockSender
from kb_custody_v21.pilot_lifecycle import PilotLifecycle
from kb_custody_v21.pilot_notice_workflow import PilotNoticeWorkflow
from kb_custody_v21.pilot_resources import GIB, PilotResources, ResourceSample
from kb_custody_v21.staging import IntegrityError
import test_pilot_availability as availability
import test_pilot_notices as notices


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f = notices.NoticeTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.a = availability.AvailabilityTests(); self.a.setUp(); self.addCleanup(self.a.doCleanups)
        self.intake, self.registry = self.a.intake, self.a.registry
        self.body = b'# Synthetic original\n\nLocal-only text.\n'
        self.message = dict(appId='cli_synthetic', chatId='oc_synthetic', senderId='ou_synthetic',
            chatType='group', messageId='om_synthetic', resources=[dict(type='file', fileKey='file_synthetic',
                fileName='synthetic.md', sizeBytes=len(self.body))])
        self.changed = lambda c, v: None
        def hook(c, v):
            if c['kind'] == 'send':
                v.clear(); v.update(code=0, data=dict(message_id='om_notice', chat_id='oc_synthetic',
                    msg_type='text', deleted=False, updated=False,
                    sender=dict(id='ou_synthetic_bot', id_type='open_id', sender_type='app'),
                    body=dict(content=json.loads(c['body'])['content'])))
                self.changed(c, v)
        self.f.f.hook = hook
        self.session = self.f.make()
        self.flow = PilotNoticeWorkflow(self.session, self.intake)

    def receive(self, message=None):
        receipt = self.intake.receive(message or self.message, 0, MockAttachment(self.body).download)
        row = self.registry.entry(receipt['entry_id'])
        path = self.intake.runtime.root/row['relative_path']
        self.f.f.frozen[str(path)] = row['sha256']
        return row['entry_id'], path

    def sends(self):
        return sum(e['type']=='intent' and e['kind']=='send' for e in self.session.http_events())

    def test_exact_outbox_selection_leaves_old_expired_and_pending_notices_untouched(self):
        eid, _ = self.receive()
        old = self.registry.rows('SELECT * FROM notification_outbox')[0]
        other = copy.deepcopy(self.message); other['messageId'] = 'om_other'
        self.receive(other)
        # Leave an expired old worker and a second pending notice in the same queue.
        sender = PersistentMockSender(self.intake.runtime)
        worker = OutboxWorker(self.registry, sender)
        worker.claim(set(), notification_id=old['notification_id'])
        with self.registry.transaction() as db:
            db.execute('UPDATE notification_outbox SET lease_until=0 WHERE notification_id=?', (old['notification_id'],))
        before = self.registry.rows('SELECT * FROM notification_outbox WHERE notification_id=?', (old['notification_id'],))
        attempts = self.registry.rows('SELECT * FROM notification_attempts')
        other_notice = self.registry.rows('SELECT notification_id FROM notification_outbox WHERE notification_id!=?', (old['notification_id'],))[0]
        self.assertEqual(worker.drain(1, notification_id=other_notice['notification_id']), 1)
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox WHERE notification_id=?', (old['notification_id'],)), before)
        self.assertEqual(self.registry.rows('SELECT * FROM notification_attempts WHERE notification_id=?', (old['notification_id'],)), attempts)
        self.assertEqual(len(sender.records('calls')), 1)
        with self.assertRaises(ContractError): worker.drain(notification_id="invalid' OR 1=1")

    def test_custody_send_holds_original_and_keeps_existing_identity_on_restart(self):
        eid, path = self.receive()
        before = self.registry.rows('SELECT * FROM notification_outbox')[0]
        def observe(c, v):
            leases = self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL')
            self.assertEqual(len(leases), 1)
            self.assertEqual(leases[0]['purpose'], 'pilot_custody_notice')
            row = self.registry.entry(eid)
            with (path.parent/'.custody.lock').open('a+') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(json.loads(c['body'])['uuid'], before['provider_request_key'])
        self.changed = observe
        result = self.flow.custody(eid)
        self.assertEqual(result['delivery_state'], 'delivered')
        self.assertEqual(result['notification_id'], before['notification_id'])
        self.assertFalse(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))
        self.assertEqual(path.read_bytes(), self.body)
        n = self.f.f.runner.call_count
        self.assertEqual(PilotNoticeWorkflow(self.session, self.intake).custody(eid)['delivery_state'], 'delivered')
        self.assertEqual(n, self.f.f.runner.call_count)

    def test_corrupted_original_blocks_pending_notice_and_preserves_acceptance(self):
        eid, path = self.receive(); path.write_bytes(b'corrupt')
        n = self.f.f.runner.call_count
        with self.assertRaises(IntegrityError): self.flow.custody(eid)
        row = self.registry.entry(eid)
        self.assertEqual((row['status'], row['custody_health']), ('accepted', 'suspect'))
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state'], 'pending')
        self.assertEqual(n, self.f.f.runner.call_count)

    def test_damage_during_remote_response_preserves_unknown_hold_without_restart_send(self):
        eid, path = self.receive()
        self.changed = lambda c, v: path.write_bytes(b'corrupt')
        with self.assertRaises(IntegrityError): self.flow.custody(eid)
        self.assertEqual(self.registry.entry(eid)['custody_health'], 'suspect')
        self.assertEqual(self.registry.entry(eid)['status'], 'accepted')
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state'], 'delivery_unknown')
        self.assertEqual(len(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL')), 1)
        n = self.f.f.runner.call_count
        self.assertEqual(self.flow.custody(eid)['delivery_state'], 'delivery_unknown')
        self.assertEqual(n, self.f.f.runner.call_count)

    def test_unapproved_original_and_group_notice_are_not_replaced_or_sent(self):
        eid, path = self.receive(); del self.f.f.frozen[str(path)]
        n = self.f.f.runner.call_count
        with self.assertRaises(ContractError): self.flow.custody(eid)
        self.assertEqual(n, self.f.f.runner.call_count)
        group = self.registry.create_group()
        message = copy.deepcopy(self.message); message['messageId'] = 'om_group'
        message['sourceGroup'] = dict(**group, required_indices=[0])
        grouped, _ = self.receive(message)
        before = self.registry.rows('SELECT * FROM notification_outbox')
        with self.assertRaises(ContractError): self.flow.custody(grouped)
        self.assertEqual(before, self.registry.rows('SELECT * FROM notification_outbox'))
        self.assertEqual(n, self.f.f.runner.call_count)

    def crash(self, mode, entry_id='unused'):
        self.session.close()
        cfg = self.f.f.root/'worker.synthetic.json'
        cfg.write_text(encoded(dict(session_root=str(self.f.f.root), approval_sha=self.f.f.digest,
            frozen=self.f.f.frozen, runtime=str(self.intake.runtime.root), entry_id=entry_id, mode=mode)))
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('pilot_notice_workflow_crash_worker.py')), str(cfg)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 77, result.stderr)
        if mode in {'custody', 'collection'}:
            # A physical receipt alone cannot finish the interrupted logical
            # request. Read local status through the old object; never relax
            # the session guard merely to let a test reopen the window.
            with self.assertRaises(ContractError): self.f.make(False)
        else:
            self.session = self.f.make(False)
        self.flow = PilotNoticeWorkflow(self.session, self.intake)

    def test_process_dies_after_notice_receipt_keeps_asset_lease_and_never_sends_again(self):
        eid, _ = self.receive()
        self.crash('custody', eid)
        before = (self.f.f.root/'http.local.jsonl').read_bytes()
        row = self.registry.rows('SELECT * FROM notification_outbox')[0]
        self.assertEqual(row['delivery_state'], 'sending')
        self.assertEqual(self.flow.custody(eid)['delivery_state'], 'delivery_unknown')
        self.assertEqual(before, (self.f.f.root/'http.local.jsonl').read_bytes())
        self.assertEqual(len(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL')), 1)
        self.assertEqual(self.registry.entry(eid)['status'], 'accepted')

    def scan(self):
        self.f.f.scan(self.session)

    def test_checklist_sends_saved_observed_status_once_and_keeps_unknown_when_state_changes(self):
        self.scan()
        self.changed = lambda c, v: self.receive()
        result = self.flow.collection('scan_synthetic')
        self.assertEqual(result['delivery_state'], 'delivery_unknown')
        n = self.f.f.runner.call_count
        self.assertEqual(self.flow.collection('scan_synthetic')['delivery_state'], 'delivery_unknown')
        self.assertEqual(self.f.f.runner.call_count, n)
        plans = self.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='pilot_collection_notice'")
        first = json.loads(plans[0]['payload_json'])['plan']
        self.assertIn('尚未确认接管', first['payload'])
        self.assertNotIn('om_synthetic', first['payload'])

    def test_checklist_success_does_not_drain_custody_or_claim_no_missing_files(self):
        self.f.f.pages[0].update(has_more=True, page_token='synthetic-next')
        self.scan(); self.receive()
        before = self.registry.rows('SELECT * FROM notification_outbox')
        r = self.flow.collection('scan_synthetic')
        self.assertEqual(r['delivery_state'], 'delivered')
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox'), before)
        body = json.loads(self.f.f.runner.call_args.args[0]['body'])
        text = json.loads(body['content'])['text']
        self.assertIn('原件已保存', text); self.assertIn('无法列出完全未观察到', text)
        n = self.f.f.runner.call_count
        self.assertEqual(self.flow.collection('scan_synthetic'), r)
        self.assertEqual(self.f.f.runner.call_count, n)

    def test_unsent_saved_report_cannot_silently_change_after_acceptance(self):
        self.scan()
        real = self.flow._report_event
        def interruption(aggregate, key, payload):
            if 'attempt' in payload: raise OSError('synthetic intent persistence failure')
            real(aggregate, key, payload)
        with patch.object(self.flow, '_report_event', side_effect=interruption):
            with self.assertRaises(OSError): self.flow.collection('scan_synthetic')
        self.receive(); n = self.f.f.runner.call_count
        with self.assertRaises(ContractError): self.flow.collection('scan_synthetic')
        self.assertEqual(self.f.f.runner.call_count, n)

    def test_report_process_death_preserves_intent_and_does_not_recreate_snapshot_or_key(self):
        self.scan(); self.crash('collection')
        before = (self.f.f.root/'http.local.jsonl').read_bytes()
        events = self.registry.rows("SELECT * FROM custody_events WHERE event_type='pilot_collection_notice'")
        self.assertEqual(len(events), 2)  # One plan, one unfinished send.
        self.assertEqual(self.flow.collection('scan_synthetic')['delivery_state'], 'delivery_unknown')
        self.assertEqual(events, self.registry.rows("SELECT * FROM custody_events WHERE event_type='pilot_collection_notice'"))
        self.assertEqual(before, (self.f.f.root/'http.local.jsonl').read_bytes())


    def lifecycle(self):
        self.probe = Mock(return_value=ResourceSample(60, 'normal', 10*GIB))
        return PilotLifecycle(self.session, self.intake,
            resources=PilotResources(self.registry, probe=self.probe))

    def test_lifecycle_announces_ready_then_closes_admission_before_pause(self):
        flow = self.lifecycle()
        observed = []
        def observe(c, v):
            observed.append(flow.status()['admission_open'])
            competing = self.lifecycle()
            with self.assertRaises(BlockingIOError):
                with competing.running(): pass
        self.changed = observe
        with flow.running():
            self.assertEqual(flow.start(checked_ready=lambda: True)['delivery_state'], 'delivered')
            with self.assertRaises(ContractError):
                with flow.running(): pass
            self.assertTrue(flow.admit())
            self.receive()  # Lifecycle ownership must not deadlock intake's own serial lock.
            self.assertEqual(flow.pause()['delivery_state'], 'delivered')
            with self.assertRaises(ContractError): flow.admit()
        self.assertEqual(observed, [True, False])
        self.assertEqual(flow.status()['state'], 'stopped')
        self.assertFalse(flow.status()['admission_open'])
        before = self.f.f.runner.call_count
        with self.assertRaises(ContractError):
            with self.lifecycle().running(): pass
        self.assertEqual(self.f.f.runner.call_count, before)

    def test_readiness_failure_never_opens_admission_or_sends_notice(self):
        flow = self.lifecycle(); n = self.f.f.runner.call_count
        with flow.running():
            with self.assertRaises(ContractError): flow.start(checked_ready=lambda: False)
            self.assertFalse(flow.status()['admission_open'])
        self.assertEqual(flow.status()['state'], 'interrupted')
        self.assertEqual(n, self.f.f.runner.call_count)

    def test_resource_pressure_closes_admission_and_allows_bounded_pause_notice(self):
        flow = self.lifecycle()
        with flow.running():
            flow.start(checked_ready=lambda: True)
            self.probe.return_value = ResourceSample(10, 'warning', 10*GIB)
            with self.assertRaises(ContractError): flow.admit()
            self.assertFalse(flow.status()['admission_open'])
            self.assertEqual(flow.pause()['delivery_state'], 'delivered')
        self.assertEqual(flow.status()['state'], 'stopped')

    def test_unknown_pause_keeps_closed_state_without_restart_or_resend(self):
        flow = self.lifecycle()
        with flow.running():
            flow.start(checked_ready=lambda: True)
            self.changed = lambda c, v: v['data'].update(updated=True)
            self.assertEqual(flow.pause()['delivery_state'], 'delivery_unknown')
        self.assertFalse(flow.status()['admission_open'])
        self.assertEqual(flow.status()['pause_notice'], 'delivery_unknown')
        n = self.f.f.runner.call_count
        with self.assertRaises(ContractError):
            with self.lifecycle().running(): pass
        self.assertEqual(n, self.f.f.runner.call_count)

    def test_exception_exit_does_not_send_or_claim_pause_success(self):
        flow = self.lifecycle()
        with self.assertRaises(OSError):
            with flow.running():
                flow.start(checked_ready=lambda: True)
                n = self.f.f.runner.call_count
                raise OSError('synthetic controller failure')
        self.assertEqual(n, self.f.f.runner.call_count)
        self.assertEqual(flow.status()['state'], 'interrupted')
        self.assertFalse(flow.status()['admission_open'])

    def test_process_death_after_ready_does_not_prove_service_alive_or_restart_it(self):
        self.crash('lifecycle')
        flow = self.lifecycle()
        self.assertEqual(flow.status()['state'], 'ready')
        self.assertFalse(flow.status()['admission_open'])
        before = (self.f.f.root/'http.local.jsonl').read_bytes()
        with self.assertRaises(ContractError):
            with flow.running(): pass
        self.assertEqual(flow.status()['state'], 'interrupted')
        self.assertEqual(before, (self.f.f.root/'http.local.jsonl').read_bytes())
