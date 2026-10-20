"""Connected publication notice and duplicate selection, with existing fixtures."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.manual_backup import ManualBackups
from kb_custody_v21.pilot_duplicate import validate_selection
from kb_custody_v21.pilot_run import PilotBoundedRun
from kb_custody_v21.f01 import encoded
from kb_custody_v21.f05 import file_sha
from pilot_pipeline_fixture import ready
from test_pilot_decision import owner_reply
import test_pilot_intake as intake_fixture
import test_pilot_run as run_fixture


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.f = intake_fixture.PilotIntakeTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.flow, self.registry = self.f.flow, self.f.registry
        self.old = self.f.receive()
        self.flow.work_once(entry_id=self.old)
        self.old_task = self.flow._job(self.old)['task_id']
        self.eid = self.f.receive(intake_fixture.message('second'))
        self.choice = dict(action='convert_as_new_version', sha256=self.registry.entry(self.eid)['sha256'],
                           prior_task_ids=[self.old_task])

    def test_explicit_new_conversion_preserves_original_task_and_binds_new_extraction(self):
        original = self.registry.entry(self.old)
        task = self.flow.conversion.service.ledger.get_task(self.old_task)
        data = (self.f.runtime.root/original['relative_path']).read_bytes()
        state = self.flow.work_once(entry_id=self.eid, duplicate_selection=self.choice)
        self.assertEqual(state['processing_state'], 'ready_candidate')
        self.assertNotEqual(state['task_id'], self.old_task)
        new = self.flow.conversion.service.ledger.get_task(state['task_id'])
        self.assertEqual(new['duplicate_action'], 'convert_as_new_version')
        mapping = json.loads(new['quality_json'])['extraction']
        self.assertEqual(mapping['source_asset_id'], self.registry.entry(self.eid)['asset_id'])
        self.assertEqual(self.registry.entry(self.old), original)
        self.assertEqual(self.flow.conversion.service.ledger.get_task(self.old_task), task)
        self.assertEqual((self.f.runtime.root/original['relative_path']).read_bytes(), data)
        self.flow.work_once(entry_id=self.eid, duplicate_selection=self.choice)
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')), 2)

    def test_changed_sha_or_prior_set_does_not_create_conversion(self):
        for change in [dict(sha256='0'*64), dict(prior_task_ids=['task_'+'b'*32]),
                       dict(prior_task_ids=[self.old_task,'task_'+'b'*32])]:
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.flow.work_once(entry_id=self.eid, duplicate_selection=dict(self.choice, **change))
        self.assertIsNone(self.flow._job(self.eid)['task_id'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')), 1)

    def test_no_queue_wide_choice_and_no_automatic_reopen_of_stopped_job(self):
        with self.assertRaises(ContractError): self.flow.work_once(duplicate_selection=self.choice)
        self.flow.work_once(entry_id=self.eid)
        self.flow.work_once(entry_id=self.eid, duplicate_selection=self.choice)
        self.assertEqual(self.flow.status(self.eid)['processing_state'], 'duplicate_needs_review')
        self.assertIsNone(self.flow._job(self.eid)['task_id'])

    def test_unhealthy_prior_source_cannot_authorize_new_conversion(self):
        self.registry.health(self.old, 'suspect', 'synthetic prior source damage')
        with self.assertRaises(ContractError):
            self.flow.work_once(entry_id=self.eid, duplicate_selection=self.choice)
        self.assertIsNone(self.flow._job(self.eid)['task_id'])

    def test_new_duplicate_since_frozen_choice_is_detected_by_fresh_inspection(self):
        inspection = self.flow.conversion.inspect(self.eid)
        with self.registry.transaction() as db:
            db.execute('UPDATE pilot_conversion_jobs SET inspection_id=? WHERE entry_id=?',
                       (inspection['inspection_id'],self.eid))
        third = self.f.receive(intake_fixture.message('third'))
        self.flow.work_once(entry_id=third, duplicate_selection=self.choice)
        with self.assertRaises(ContractError):
            self.flow.work_once(entry_id=self.eid, duplicate_selection=self.choice)
        self.assertIsNone(self.flow._job(self.eid)['task_id'])

    def test_selection_rejects_invalid_structures(self):
        for change in [dict(action='reuse_existing'),dict(prior_task_ids=[[]]),dict(prior_task_ids=[]),
                       dict(prior_task_ids=[self.old_task,self.old_task]),dict(sha256=None)]:
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_selection(dict(self.choice,**change))


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.f=run_fixture.RunTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def publish(self):
        r=self.f.run.run_to_preview()
        self.f.f.f.message=owner_reply()
        self.f.f.f.pages=[dict(items=[copy.deepcopy(self.f.f.f.message)],has_more=False)]
        self.f.session.scan('scan_final',start=self.f.f.f.now-timedelta(minutes=30),end=self.f.f.f.now)
        return r, self.f.run.owner_reply('scan_final','om_owner_reply')

    def test_exact_publication_notice_and_local_manual_package_preserve_separate_states(self):
        _, result=self.publish()
        self.assertEqual(result['notification']['delivery_state'],'delivered')
        pid=result['publication_id']
        n=self.f.f.f.runner.call_count
        self.assertEqual(self.f.run.notices.publication(pid),result['notification'])
        self.assertEqual(n,self.f.f.f.runner.call_count)
        plan=self.f.run.notices._publication_plan(pid)[1]
        self.assertIn('本机知识库',plan['payload'])
        self.assertIn('尚未完成',plan['payload'])
        backups=ManualBackups(self.f.registry)
        package=backups.create([pid])
        self.assertEqual(package['manual_upload_status'],'pending_manual_upload')
        self.assertEqual(package['verification_status'],'not_run')
        trusted, manifest=backups._trusted(package['backup_batch_id'])
        self.assertEqual(manifest['frozen']['scope'][0]['publication_id'],pid)
        self.assertEqual(n,self.f.f.f.runner.call_count)

    def test_final_notice_unknown_preserves_published_and_will_not_resend(self):
        def unknown(c,v):
            if c.get('notice'):
                plan=json.loads(Path(c['notice']['plan_path']).read_bytes())
                if plan['purpose']=='publication':v['data']['updated']=True
        self.f.f.changed=unknown
        _,result=self.publish()
        self.assertEqual(result['notification']['delivery_state'],'delivery_unknown')
        pid=result['publication_id']
        self.assertEqual(self.f.run.pipeline.publisher._row(pid)['state'],'published')
        n=self.f.f.f.runner.call_count
        self.assertEqual(self.f.run.notices.publication(pid)['delivery_state'],'delivery_unknown')
        self.assertEqual(n,self.f.f.f.runner.call_count)

    def test_without_actual_owner_confirmation_there_is_no_final_notice(self):
        self.f.run.run_to_preview()
        pid=self.f.session.value(self.f.run.prefix+'candidate')['publication_id']
        n=self.f.f.f.runner.call_count
        with self.assertRaises(ContractError):self.f.run.notices.publication(pid)
        self.assertEqual(n,self.f.f.f.runner.call_count)

    def test_frozen_duplicate_choice_connects_to_normal_guide_and_preview(self):
        old=dict(appId='cli_synthetic',chatId='oc_synthetic',senderId='ou_synthetic',chatType='group',
                 messageId='om_prior',resources=[dict(type='file',fileKey='file_prior',fileName='prior.md')])
        eid=self.f.intake.receive(old,0,MockAttachment(self.f.sample.read_bytes()).download)['entry_id']
        self.f.intake.work_once(entry_id=eid)
        tid=self.f.intake._job(eid)['task_id']
        self.f.run.close()
        plan=json.loads(self.f.plan.read_bytes())
        plan['duplicate_selection']=dict(action='convert_as_new_version',sha256=file_sha(self.f.sample),prior_task_ids=[tid])
        self.f.plan.write_text(encoded(plan));self.f.f.f.frozen[str(self.f.plan)]=file_sha(self.f.plan)
        self.f.run=PilotBoundedRun(self.f.session,self.f.intake,self.f.plan,
            prepare_candidate=self.f.preparer,checked_ready=lambda:True,resources=ready(self.f.registry))
        self.addCleanup(self.f.run.close)
        result=self.f.run.run_to_preview()
        self.assertEqual(result['state'],'awaiting_owner_decision')
        self.assertNotEqual(self.f.intake._job(result['entry_id'])['task_id'],tid)

    def test_mutated_in_memory_choice_is_rejected_before_calls(self):
        self.f.run.plan['duplicate_selection']={}
        with self.assertRaises(ContractError):self.f.run.run_to_preview()
        self.f.f.f.runner.assert_not_called()
