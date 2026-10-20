"""Reuse the real local publication fixture; no simulated chat service."""
from pathlib import Path
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.pilot_preview import PilotPreviews,PilotScope
import test_publication_v21 as publication_fixture


class PilotPreviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publication_fixture.PublicationTests()
        self.fixture.setUp()
        self.registry = self.fixture.registry
        self.runtime = self.fixture.runtime
        self.scope = PilotScope('synthetic-app','synthetic-chat','synthetic-user')
        self.pilot = PilotPreviews(self.registry,self.scope)

    def candidate(self):
        preview = self.fixture.prepare()
        self.pilot.stage(preview['publication_id'])
        return preview

    def bind(self,preview,message='synthetic-preview',**overrides):
        args = dict(app_id=self.scope.app_id,chat_id=self.scope.chat_id,message_id=message,
                    pdf_sha256=preview['preview_sha256'],receipt_ref='synthetic-receipt-'+message,outcome='sent')
        self.pilot.bind_delivery(preview['publication_id'],**(args|overrides))

    def decide(self,**overrides):
        args = dict(app_id=self.scope.app_id,chat_id=self.scope.chat_id,user_id=self.scope.user_id,
                    message_id='synthetic-decision',reply_to='synthetic-preview',text='确认入库')
        return self.pilot.decide(**(args|overrides))

    def test_exact_reply_publishes_same_pdf_once_across_restart(self):
        preview = self.candidate()
        original = Path(preview['preview_path']).read_bytes()
        self.bind(preview)
        first = self.decide()
        self.pilot = PilotPreviews(self.registry,self.scope)
        self.assertEqual(self.decide(),first)
        self.assertEqual((Path(first['path'])/'发布记录/已确认入库预览.pdf').read_bytes(),original)
        for table in ('pilot_preview_decisions','publication_confirmations','active_publications_v21'):
            self.assertEqual(len(self.registry.rows('SELECT * FROM '+table)),1)

    def test_scope_and_explicit_reply_required(self):
        preview = self.candidate()
        self.bind(preview)
        for change in ({'app_id':'another-app'},{'chat_id':'another-chat'},
                       {'user_id':'another-user'},{'reply_to':''},{'reply_to':'unknown-preview'},
                       {'text':'好的'},{'text':'确认'},{'text':'确认当前版本入库'}):
            with self.subTest(change=change),self.assertRaises(ContractError):
                self.decide(**change)
        self.assertEqual(self.registry.rows('SELECT * FROM publication_confirmations'),[])

    def test_no_binding_for_unstaged_unknown_failed_or_wrong_pdf(self):
        preview = self.fixture.prepare()
        with self.assertRaises(ContractError): self.bind(preview)
        self.pilot.stage(preview['publication_id'])
        for change in ({'outcome':'unknown'},{'outcome':'failed'},{'message_id':''},
                       {'pdf_sha256':'0'*64},{'chat_id':'another-chat'},{'app_id':'another-app'}):
            with self.subTest(change=change),self.assertRaises(ContractError): self.bind(preview,**change)
        self.assertEqual(self.registry.rows('SELECT * FROM pilot_preview_deliveries'),[])
        with self.assertRaises(ContractError): self.decide()

    def test_new_candidate_invalidates_old_even_if_new_send_unknown(self):
        old = self.candidate()
        self.bind(old)
        new = self.candidate()
        with self.assertRaises(ContractError): self.bind(new,'synthetic-new-preview',outcome='unknown')
        with self.assertRaisesRegex(ContractError,'old preview'): self.decide()
        with self.assertRaisesRegex(ContractError,'old preview'): self.pilot.stage(old['publication_id'])
        self.bind(new,'synthetic-new-preview')
        result = self.decide(reply_to='synthetic-new-preview')
        self.assertEqual(result['publication_id'],new['publication_id'])
        self.assertEqual(self.fixture.publisher._row(old['publication_id'])['state'],'prepared')

    def test_delivery_and_decision_identity_cannot_be_rebound(self):
        preview = self.candidate()
        self.bind(preview)
        self.bind(preview)  # Same successful receipt is safe to ingest twice.
        with self.assertRaises(ContractError): self.bind(preview,'synthetic-different-preview')
        self.decide()
        with self.assertRaises(ContractError): self.decide(text='暂不入库')
        with self.assertRaises(ContractError): self.decide(message_id='synthetic-new-decision')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)

    def test_changed_candidate_rejected_without_recording_approval(self):
        preview = self.candidate()
        self.bind(preview)
        Path(preview['preview_path']).write_bytes(b'changed after send')
        with self.assertRaises(ContractError): self.decide()
        self.assertEqual(self.registry.rows('SELECT * FROM pilot_preview_decisions'),[])
        self.assertEqual(self.registry.rows('SELECT * FROM publication_confirmations'),[])

    def test_decision_record_failure_rolls_back_confirmation(self):
        preview = self.candidate()
        self.bind(preview)
        with self.registry.transaction() as db:
            db.execute("CREATE TRIGGER synthetic_decision_failure BEFORE INSERT ON pilot_preview_decisions BEGIN SELECT RAISE(ABORT,'synthetic transaction failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): self.decide()
        self.assertEqual(self.fixture.publisher._row(preview['publication_id'])['state'],'prepared')
        self.assertEqual(self.registry.rows('SELECT * FROM publication_confirmations'),[])

    def test_crash_after_approval_resumes_same_candidate_and_blocks_replacement(self):
        preview = self.candidate()
        self.bind(preview)
        with patch.object(self.pilot.publisher,'publish',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError): self.decide()
        self.assertEqual(self.fixture.publisher._row(preview['publication_id'])['state'],'confirmed')
        replacement = self.fixture.prepare()
        with self.assertRaises(ContractError): self.pilot.stage(replacement['publication_id'])
        self.pilot = PilotPreviews(self.registry,self.scope)
        self.assertEqual(self.decide()['status'],'published')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)

    def test_crash_after_rename_resumes_without_new_publication(self):
        preview = self.candidate()
        self.bind(preview)
        pid = preview['publication_id']
        original_tree = self.pilot.publisher._tree
        def crash_after_rename(row,name):
            if name==pid: raise RuntimeError('synthetic crash after rename')
            return original_tree(row,name)
        with patch.object(self.pilot.publisher,'_tree',side_effect=crash_after_rename):
            with self.assertRaises(RuntimeError): self.decide()
        self.assertEqual(self.fixture.publisher._row(pid)['state'],'publishing')
        self.pilot = PilotPreviews(self.registry,self.scope)
        self.assertEqual(self.decide()['status'],'published')
        self.assertEqual(len(self.registry.rows("SELECT * FROM custody_events WHERE event_type='publication_published'")),1)

    def test_supplement_and_defer_preserve_draft_and_require_new_preview(self):
        for command,status in (('补充材料后再入库','supplement'),('暂不入库','defer')):
            with self.subTest(command=command):
                preview = self.candidate()
                message = 'synthetic-preview-'+status
                self.bind(preview,message)
                args = dict(message_id='synthetic-decision-'+status,reply_to=message,text=command)
                self.assertEqual(self.decide(**args)['status'],status)
                self.assertEqual(self.decide(**args)['status'],status)
                self.assertTrue(Path(preview['preview_path']).exists())
                self.assertEqual(self.fixture.publisher._row(preview['publication_id'])['state'],'prepared')
                with self.assertRaises(ContractError): self.decide(reply_to=message,message_id='synthetic-confirm-'+status)
        self.assertEqual(self.registry.rows('SELECT * FROM publication_confirmations'),[])

    def test_source_damage_after_preview_still_blocks_publish(self):
        preview = self.candidate()
        self.bind(preview)
        path = self.runtime.root/self.registry.entry(self.fixture.entry)['relative_path']
        path.write_bytes(b'synthetic damaged original')
        from kb_custody_v21.staging import IntegrityError
        with self.assertRaises(IntegrityError): self.decide()
        self.assertFalse(self.registry.rows('SELECT * FROM active_publications_v21'))
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_preview_decisions')),1)

    def test_restart_with_changed_scope_cannot_accept_previous_candidate(self):
        preview = self.candidate()
        self.bind(preview)
        self.pilot = PilotPreviews(self.registry,PilotScope('another-app','another-chat','another-user'))
        with self.assertRaises(ContractError): self.pilot.stage(preview['publication_id'])
        with self.assertRaises(ContractError): self.decide()

    def test_old_approval_redelivery_does_not_restore_old_active_revision(self):
        old = self.candidate()
        self.bind(old)
        self.decide()
        new = self.fixture.prepare(revision_of=old['publication_id'])
        self.pilot.stage(new['publication_id'])
        self.bind(new,'synthetic-revision-preview')
        self.decide(reply_to='synthetic-revision-preview',message_id='synthetic-revision-decision')
        self.assertEqual(self.decide()['publication_id'],old['publication_id'])
        active = self.registry.rows('SELECT * FROM active_publications_v21')
        self.assertEqual(active[0]['publication_id'],new['publication_id'])

    def test_same_preview_message_cannot_be_assigned_to_new_version(self):
        first = self.candidate()
        self.bind(first)
        second = self.candidate()
        with self.assertRaisesRegex(ContractError,'another candidate'):
            self.bind(second)
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_preview_deliveries')),1)

    def test_status_follows_explicit_defer_supplement_and_superseded_candidate(self):
        for command,status in (('暂不入库','defer'),('补充材料后再入库','supplement')):
            preview=self.candidate();pid=preview['publication_id'];message='synthetic-preview-'+status
            self.bind(preview,message)
            self.decide(reply_to=message,message_id='synthetic-decision-'+status,text=command)
            observed=self.pilot.status(pid)
            self.assertEqual(observed['state'],status)
            self.assertEqual(observed['owner_decision'],status)
            self.assertEqual(observed['publication_state'],'prepared')
            replacement=self.candidate()
            observed=self.pilot.status(pid)
            self.assertEqual(observed['state'],'preview_superseded')
            self.assertFalse(observed['current_candidate'])
            self.assertEqual(observed['owner_decision'],status)
            self.assertEqual(self.pilot.status(replacement['publication_id'])['delivery_state'],'NO_VERIFIED_RECEIPT')
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_status_reports_unstaged_and_rejects_candidate_from_another_scope(self):
        preview=self.fixture.prepare();pid=preview['publication_id']
        self.assertEqual(self.pilot.status(pid)['state'],'preview_prepared')
        self.pilot.stage(pid)
        changed=PilotPreviews(self.registry,PilotScope('another-app','another-chat','another-user'))
        with self.assertRaises(ContractError):changed.status(pid)

    def test_status_preserves_historical_publication_without_calling_it_active(self):
        old=self.candidate();self.bind(old);self.decide()
        new=self.fixture.prepare(revision_of=old['publication_id'])
        self.pilot.stage(new['publication_id']);self.bind(new,'synthetic-new')
        self.decide(reply_to='synthetic-new',message_id='synthetic-new-choice')
        observed=self.pilot.status(old['publication_id'])
        self.assertEqual(observed['state'],'published')
        self.assertFalse(observed['active_publication'])
        self.assertFalse(observed['current_candidate'])
        self.assertTrue(self.pilot.status(new['publication_id'])['active_publication'])
