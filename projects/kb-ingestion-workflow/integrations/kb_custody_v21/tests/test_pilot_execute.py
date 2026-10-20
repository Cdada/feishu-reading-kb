"""Finite operator startup and composition; existing synthetic transports only."""
from datetime import timedelta
import json
from unittest.mock import Mock,patch
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded
from kb_custody_v21.f05 import file_sha
import pilot_execute
import test_pilot_guide_preparation as guide_fixture
from kb_custody_v21.pilot_resources import ResourceSample


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.f=guide_fixture.GuidePreparationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.f.f.root;self.now=self.f.f.f.now
        self.approval=dict(self.f.session.approval)
        self.f.run.close();self.f.session.close()
        self.source=Mock()
        self.manifest_path=self.root/'source-manifest.synthetic.json'
        self.manifest=dict(worktree=str(self.f.intake.runtime.worktree),source_commit='synthetic-commit',
            source_snapshot=dict(files={},sha256='synthetic-snapshot'),approved_files=dict(self.f.f.f.frozen))
        self.executable=self.root/'synthetic-executable';self.executable.write_bytes(b'fixture only');self.executable.chmod(0o700)
        self.manifest['approved_files'][str(self.executable)]=file_sha(self.executable)
        self.approval.update(source_manifest=str(self.manifest_path),run_plan=str(self.f.run_path),
            guide_plan=str(self.f.path),runtime_config=dict(self.f.intake.runtime.config,
                runtime_relative=str(self.f.intake.runtime.root.relative_to(self.f.intake.runtime.worktree))))
        for name in ('cli_guard','text_node','text_script','codex_binary'):self.approval[name]=str(self.executable)
        self.save()

    def save(self):
        self.manifest_path.write_text(encoded(self.manifest))
        self.approval['source_manifest_sha256']=file_sha(self.manifest_path)
        path=self.root/'approval.local.json';path.write_text(encoded(self.approval));self.digest=file_sha(path)

    def check(self):
        with patch.object(pilot_execute,'checked_source',self.source):
            return pilot_execute.execution_inputs(self.root,self.digest,clock=lambda:self.now)

    def test_complete_source_and_dependencies_checked_before_external_factories(self):
        worktree,approval,frozen=self.check()
        self.source.assert_called_once_with(worktree,self.manifest)
        self.assertEqual(approval,self.approval)
        self.assertIn(str(self.f.path),frozen)
        self.assertIn(str(self.executable),frozen)

    def test_unapproved_window_does_not_open_credentials_or_write_markers(self):
        self.approval['approved']=False;self.save()
        with self.assertRaises(ContractError):self.check()
        self.source.assert_not_called()
        self.assertFalse(list(self.root.glob('entry-*.local.json')))

    def test_expired_window_is_rejected_before_source_work(self):
        self.now+=timedelta(days=1)
        with self.assertRaises(ContractError):self.check()
        self.source.assert_not_called()

    def test_source_check_crossing_deadline_rejects_before_access(self):
        def expire(*_):self.now+=timedelta(days=1)
        self.source.side_effect=expire
        with self.assertRaisesRegex(ContractError,'outside'):self.check()
        self.assertFalse(list(self.root.glob('entry-*.local.json')))

    def test_manifest_changed_during_source_check_is_rejected(self):
        self.source.side_effect=lambda *_:self.manifest_path.write_text('{}')
        with self.assertRaisesRegex(ContractError,'manifest changed'):self.check()

    def test_unbound_executable_is_rejected(self):
        self.manifest['approved_files'].pop(str(self.executable));self.save()
        with self.assertRaisesRegex(ContractError,'dependencies'):self.check()

    def test_binary_byte_change_is_rejected_before_instantiation(self):
        self.executable.write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError,'input changed'):self.check()

    def test_missing_explicit_owner_reply_does_not_even_load_execution_inputs(self):
        with patch.object(pilot_execute,'execution_inputs') as load:
            with self.assertRaises(ContractError):pilot_execute.execute(self.root,self.digest,'owner-reply')
            load.assert_not_called()

    def test_owner_followup_freezes_only_its_actual_executables(self):
        self.approval['operation']='owner-followup'
        for k in ('guide_plan','text_node','text_script','codex_binary'):self.approval.pop(k)
        self.save()
        self.assertEqual(self.check()[1]['operation'],'owner-followup')

    def test_owner_followup_cannot_borrow_intake_approval(self):
        with patch.object(pilot_execute,'execution_inputs',return_value=(None,self.approval,{})):
            with self.assertRaisesRegex(ContractError,'own explicit approval'):
                pilot_execute.execute(self.root,self.digest,'owner-followup')
        self.assertFalse(list(self.root.glob('entry-*.local.json')))

    def test_operator_entry_runs_existing_connected_fixtures_and_cannot_restart(self):
        # No request has occurred. This header belongs exclusively to this
        # synthetic setup, so replace it with the final synthetic approval.
        journal=self.root/'journal.local.jsonl'
        self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=self.approval))+'\n')
        process=Mock(side_effect=self.f.f.f.runner)
        with patch.object(pilot_execute,'checked_source',self.source), patch.object(
                pilot_execute,'CliProcess',return_value=process), patch.object(
                pilot_execute,'CodexTextWire',return_value=self.f.wire), patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(self.root,self.digest,'intake')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
            self.assertEqual(result['result']['state'],'awaiting_owner_decision')
            calls=process.call_count
            with self.assertRaises(FileExistsError):pilot_execute.execute(self.root,self.digest,'intake')
            self.assertEqual(process.call_count,calls)
        self.assertEqual(self.f.wire.calls,1)
        self.assertFalse(self.f.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertTrue(list(self.root.glob('entry-*-result.local.json')))

    def test_conversion_only_excludes_model_factories_and_stops_after_conversion(self):
        self.approval['operation']='intake-convert'
        for key in ('guide_plan','text_node','text_script','codex_binary'):
            self.approval.pop(key)
        self.save()
        journal=self.root/'journal.local.jsonl'
        self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=self.approval))+'\n')
        process=Mock(side_effect=self.f.f.f.runner)
        with patch.object(pilot_execute,'checked_source',self.source), patch.object(
                pilot_execute,'CliProcess',return_value=process), patch.object(
                pilot_execute,'CodexTextWire',side_effect=AssertionError('no model factory')), patch.object(
                pilot_execute,'PilotGuidePreparation',side_effect=AssertionError('no guide factory')), patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(self.root,self.digest,'intake-convert')
        self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
        self.assertEqual(result['result']['state'],'converted_awaiting_preparation')
        self.assertFalse(result['result']['service_admission_open'])
        self.assertFalse(self.f.registry.rows('SELECT * FROM publications_v21'))
        self.assertEqual(self.f.wire.calls,0)

    def test_staged_operations_cannot_borrow_other_operation_approval(self):
        for action,operation in (('prepare',None),('intake-convert',None),('intake','intake-convert'),('owner-reply','prepare')):
            approval=dict(self.approval,operation=operation)
            with self.subTest(action=action,operation=operation), patch.object(pilot_execute,'execution_inputs',return_value=(None,approval,{})):
                with self.assertRaisesRegex(ContractError,'own explicit approval'):
                    pilot_execute.execute(self.root,self.digest,action,message_id='om_synthetic' if action=='owner-reply' else None)
        self.assertFalse(list(self.root.glob('entry-*.local.json')))

    def test_prepare_requires_a_frozen_existing_material_plan(self):
        self.approval['operation']='prepare';self.save()
        with patch.object(pilot_execute,'checked_source',self.source):
            with self.assertRaisesRegex(ContractError,'explicitly approved material plan'):
                pilot_execute.execute(self.root,self.digest,'prepare')
        self.assertFalse(list(self.root.glob('entry-*.local.json')))

    def test_notice_only_has_no_model_dependencies(self):
        self.approval['operation']='publication-notice'
        for key in ('guide_plan','text_node','text_script','codex_binary'):self.approval.pop(key)
        self.save()
        self.assertEqual(self.check()[1]['operation'],'publication-notice')

    def test_notice_only_cannot_borrow_other_operation_approval(self):
        with patch.object(pilot_execute,'execution_inputs',return_value=(None,self.approval,{})):
            with self.assertRaisesRegex(ContractError,'own explicit approval'):
                pilot_execute.execute(self.root,self.digest,'publication-notice')
        self.assertFalse(list(self.root.glob('entry-*.local.json')))

    def test_moving_candidate_input_is_rejected_at_operator_start(self):
        candidate=self.f.intake.runtime.root/'conversion-work/publications/publication_synthetic.candidate'
        candidate.mkdir(parents=True);file=candidate/'synthetic';file.write_text('synthetic')
        self.manifest['approved_files'][str(file)]=file_sha(file);self.save()
        with self.assertRaisesRegex(ContractError,'live candidate'):self.check()
        self.assertFalse(list(self.root.glob('entry-*.local.json')))
