import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.staging import IntegrityError
from test_custody import WORKTREE, BASE


SCOPE = PilotScope('synthetic-pilot-app', 'synthetic-pilot-group', 'synthetic-person')
CONTENT = '# 合成材料\n\n第一段城市研究。\n'.encode()


def message(mid='synthetic-message', count=1):
    return dict(appId=SCOPE.app_id, chatId=SCOPE.chat_id, senderId=SCOPE.user_id,
                chatType='group', messageId=mid, resources=[dict(type='file', fileKey=f'key-{i}',
                    fileName=f'synthetic-{i}.md', sizeBytes=len(CONTENT)) for i in range(count)])


class PilotIntakeTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix='pilot-intake-', dir=BASE/'test-output'))
        self.runtime = Runtime(WORKTREE, DEFAULTS, root)
        self.registry = Registry(self.runtime)
        self.flow = self.reopen()

    def reopen(self, **options):
        return PilotIntake(self.registry, SCOPE, max_attachments=5, max_pending=options.get('max_pending', 8))

    def receive(self, m=None, index=0):
        return self.flow.receive(m or message(), index, MockAttachment(CONTENT).download)['entry_id']

    def test_restart_uses_accepted_bytes_when_remote_no_longer_available(self):
        m = message()
        attachment = MockAttachment(CONTENT)
        eid = self.flow.receive(m, 0, attachment.download)['entry_id']
        attachment.available = False
        reopened = self.reopen()
        self.assertEqual(reopened.receive(m, 0, attachment.download)['entry_id'], eid)
        result = reopened.work_once()
        self.assertEqual(result['processing_state'], 'ready_candidate')
        self.assertEqual(attachment.download_calls, 1)
        self.assertEqual(reopened.work_once()['state'], 'idle')
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')), 1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM notification_attempts')), 0)

    def test_scope_rejected_before_any_registration(self):
        for field in ('appId','chatId','senderId','chatType'):
            m = message(); m[field] = 'wrong'
            with self.assertRaises(ContractError): self.flow.prepare(m)
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'))
        with self.assertRaises(ContractError):
            PilotIntake(self.registry, PilotScope('different', SCOPE.chat_id, SCOPE.user_id), max_attachments=5, max_pending=8)

    def test_changed_replay_or_resource_order_is_rejected(self):
        m = message(count=2)
        original = self.flow.prepare(m)
        for change in ('order','drop','key','name'):
            altered = copy.deepcopy(m)
            if change=='order': altered['resources'].reverse()
            elif change=='drop': altered['resources'].pop()
            elif change=='key': altered['resources'][0]['fileKey']='other'
            else: altered['resources'][0]['fileName']='new.md'
            with self.assertRaises(ContractError): self.flow.prepare(altered)
        self.assertEqual(self.flow.prepare(m), original)

    def test_partial_prepare_repairs_same_group_without_duplicate_entries(self):
        m = message(count=2)
        m['sourceGroup'] = dict(source_group_id='group_'+'a'*32, membership_revision=1, required_indices=[0,1])
        register = self.registry.register
        def fail_second(contract):
            if contract['received_filename']=='synthetic-1.md': raise OSError('synthetic storage interruption')
            return register(contract)
        with patch.object(self.registry,'register',side_effect=fail_second), self.assertRaises(OSError):
            self.flow.prepare(m)
        entries = self.reopen().prepare(m)
        self.assertEqual(len(entries),2)
        self.assertEqual(len(self.registry.rows('SELECT * FROM intake_entries')),2)
        self.assertEqual(self.registry.rows('SELECT * FROM source_groups')[0]['membership_sealed'],1)

    def test_failed_required_attachment_does_not_convert_accepted_group_member(self):
        m = message(count=2)
        m['sourceGroup'] = dict(source_group_id='group_'+'b'*32, membership_revision=1, required_indices=[0,1])
        eid = self.receive(m)
        with self.assertRaises(OSError): self.flow.receive(m,1,MockAttachment(CONTENT,available=False).download)
        self.assertEqual(self.flow.work_once()['state'],'idle')
        self.assertEqual(self.flow.status(eid)['original_status'],'accepted')
        self.assertFalse(self.registry.rows('SELECT * FROM conversion_bindings'))

    def test_separate_message_continues_while_other_group_waits(self):
        m=message(count=2)
        m['sourceGroup']=dict(source_group_id='group_'+'c'*32,membership_revision=1,required_indices=[0,1])
        self.receive(m)
        independent=self.receive(message('independent'))
        self.assertEqual(self.flow.work_once()['entry_id'],independent)

    def test_known_oversize_invalid_key_and_queue_capacity_fail_before_download(self):
        m=message(); m['resources'][0]['sizeBytes']=self.runtime.config['max_asset_bytes']+1
        with self.assertRaises(ContractError): self.flow.prepare(m)
        m=message(); m['resources'][0]['fileKey']=None
        with self.assertRaises(ContractError): self.flow.prepare(m)
        bounded=self.reopen(max_pending=1)
        bounded.prepare(message())
        with self.assertRaisesRegex(ContractError,'queue full'): bounded.prepare(message('next'))
        self.assertEqual(len(self.registry.rows('SELECT * FROM intake_entries')),1)

    def test_unsupported_is_saved_but_not_sent_to_new_converter(self):
        m=message(); m['resources'][0]['fileName']='synthetic.pptx'
        eid=self.receive(m)
        self.assertEqual(self.flow.work_once()['state'],'idle')
        self.assertEqual(self.flow.status(eid)['processing_state'],'custody_only')
        self.assertFalse(self.registry.rows('SELECT * FROM conversion_bindings'))

    def test_duplicate_bytes_require_choice_and_no_second_conversion(self):
        self.receive(); self.flow.work_once()
        eid=self.receive(message('second'))
        self.flow.work_once()
        self.assertEqual(self.flow.status(eid)['processing_state'],'duplicate_needs_review')
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')),1)

    def test_damage_marks_suspect_and_preserves_accepted(self):
        eid=self.receive()
        (self.runtime.root/self.registry.entry(eid)['relative_path']).write_bytes(b'damage')
        with self.assertRaises(IntegrityError): self.flow.work_once()
        self.assertEqual(self.flow.status(eid)['original_status'],'accepted')
        self.assertEqual(self.flow.status(eid)['processing_state'],'integrity_hold')

    def test_capacity_pause_and_busy_do_not_start_conversion(self):
        self.receive()
        with patch('kb_custody_v21.pilot_intake.shutil.disk_usage',return_value=shutil_usage(0)):
            self.assertEqual(self.flow.work_once()['state'],'paused_capacity')
        with self.flow._serial():
            self.assertEqual(self.reopen().work_once()['state'],'busy')
        self.assertFalse(self.registry.rows('SELECT * FROM conversion_bindings'))

    def test_hard_exit_after_submit_restarts_same_durable_task(self):
        eid=self.receive()
        worker=Path(__file__).with_name('pilot_intake_crash_worker.py')
        result=subprocess.run([sys.executable,str(worker),str(self.runtime.root)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,86,result.stderr)
        self.assertIsNone(self.flow._job(eid)['task_id'])
        before=self.registry.rows('SELECT * FROM conversion_bindings')
        self.assertEqual(len(before),1)
        after=self.reopen().work_once()
        self.assertEqual(after['task_id'],before[0]['task_id'])
        self.assertEqual(after['processing_state'],'ready_candidate')
        self.assertEqual(len(self.registry.rows('SELECT * FROM staging_attempts')),1)

    def test_live_worker_fence_preserves_queued_task(self):
        eid=self.receive()
        with self.flow.conversion.recovery.lock('execution'):
            self.assertEqual(self.flow.work_once()['state'],'busy')
        task=self.flow.conversion.service.ledger.get_task(self.flow._job(eid)['task_id'])
        self.assertEqual(task['status'],'queued')
        self.assertIsNone(self.registry.rows('SELECT * FROM conversion_bindings')[0]['released_at'])
        self.assertEqual(self.flow.work_once()['processing_state'],'ready_candidate')

    def test_expired_local_inspection_refreshes_only_before_task_creation(self):
        eid=self.receive()
        inspected=self.flow.conversion.inspect(eid)
        with self.registry.transaction() as db:
            db.execute('UPDATE pilot_conversion_jobs SET inspection_id=? WHERE entry_id=?',(inspected['inspection_id'],eid))
        ledger=self.flow.conversion.service.ledger
        original=ledger.get_inspection
        def expired(iid):
            row=original(iid)
            if iid==inspected['inspection_id']: row['expires_at']='2000-01-01T00:00:00+00:00'
            return row
        with patch.object(ledger,'get_inspection',side_effect=expired):
            result=self.flow.work_once()
        self.assertEqual(result['processing_state'],'ready_candidate')
        self.assertNotEqual(self.flow._job(eid)['inspection_id'],inspected['inspection_id'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM staging_attempts')),1)

    def test_pdf_review_and_failed_task_are_not_automatically_retried(self):
        from test_pdf_mapping import synthetic_pdf
        data=synthetic_pdf()
        m=message();m['resources'][0].update(fileName='synthetic.pdf',sizeBytes=len(data))
        eid=self.flow.receive(m,0,MockAttachment(data).download)['entry_id']
        result=self.flow.work_once()
        self.assertEqual(result['processing_state'],'needs_review')
        self.assertEqual(self.reopen().work_once()['state'],'idle')
        next_id=self.receive(message('next'))
        with patch.object(self.flow.conversion.worker,'_run_stage',side_effect=RuntimeError('synthetic failure')):
            failed=self.flow.work_once()
        self.assertEqual(failed['processing_state'],'failed')
        self.assertEqual(self.reopen().work_once()['state'],'idle')
        self.assertEqual(self.flow.status(next_id)['original_status'],'accepted')
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')),2)

    def test_targeted_worker_leaves_unrelated_queued_task_untouched(self):
        first=self.receive(message('first'))
        inspected=self.flow.conversion.inspect(first)
        other=self.flow.conversion.submit(inspected['inspection_id'],'unrelated-key-0001')
        self.flow._stop(first,'manual_other_job')
        second=self.receive(message('second'))
        self.assertEqual(self.flow.work_once()['entry_id'],second)
        self.assertEqual(self.flow.conversion.service.ledger.get_task(other['task_id'])['status'],'queued')


def shutil_usage(free):
    from collections import namedtuple
    return namedtuple('usage','total used free')(100,100-free,free)
