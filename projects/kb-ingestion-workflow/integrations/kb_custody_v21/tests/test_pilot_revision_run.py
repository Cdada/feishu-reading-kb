"""Explicit revision uses the same existing transport and preparation fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_guide_preparation import PilotGuidePreparation
from kb_custody_v21.pilot_run import PilotBoundedRun
from kb_custody_v21.pilot_revision import digest, rejected_receipt
from kb_custody_v21.pilot_resources import ResourceSample
from kb_custody_v21.registry import encoded
from pilot_pipeline_fixture import ready
import pilot_execute
import test_pilot_guide_preparation as fixture


class RevisionRunTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.GuidePreparationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.entry=self.f.accept()
        _,derived,consent,args=self.f.guide._inputs(self.f.run.pipeline,self.entry)
        self.f.wire.mutate=lambda _:dict(status='received',text='{}')
        with self.assertRaisesRegex(ContractError,'received_rejected'):self.f.guide(self.f.run.pipeline,self.entry)
        self.old_root=self.f.children()[0]
        _,mapping,extraction=self.f.intake.conversion.publication_extraction(derived['extraction_id'])
        self.receipt=rejected_receipt(self.old_root,file_sha(self.old_root/'approval.local.json'),
            self.f.plan['record'],(extraction/'source.md').read_bytes(),mapping,consent,**args)
        previous=self.f.run.pipeline._event(self.entry,'plan')
        self.binding=dict(previous_plan_sha256=digest(previous),previous_text_root=str(self.old_root),
            previous_text_approval_sha256=file_sha(self.old_root/'approval.local.json'),
            previous_consent=consent,previous_receipt_sha256=digest(self.receipt))
        self.f.run.close();self.f.session.close()
        platform=self.f.f.f
        self.old_platform=platform.root
        self.old_bytes={str(path):path.read_bytes() for path in self.old_platform.iterdir() if path.is_file()}
        platform.root=platform.root.parent/'revision'/'p11-requests';platform.root.mkdir(parents=True)
        platform.session=platform.root/'approval.local.json'
        self.run_path=platform.root/'run.synthetic.json'
        plan=json.loads(self.f.run_path.read_bytes());plan['revision']=dict(entry_id=self.entry,binding=self.binding)
        self.run_path.write_text(encoded(plan))
        self.guide_path=platform.root/'guide.synthetic.json'
        guide_plan=dict(self.f.plan,run_plan_path=str(self.run_path),run_plan_sha256=file_sha(self.run_path))
        self.guide_path.write_text(encoded(guide_plan))
        platform.frozen.update({str(self.run_path):file_sha(self.run_path),str(self.guide_path):file_sha(self.guide_path),
            **self.receipt['files']})
        platform.approval.update(root=str(platform.root),guide_plan_sha256=file_sha(self.guide_path))
        self.f.f.save_approval()
        self.session=self.f.f.make(False)
        self.f.wire.mutate=lambda result:result
        self.guide=PilotGuidePreparation(self.session,self.guide_path,wire=self.f.wire)
        self.run=PilotBoundedRun(self.session,self.f.intake,self.run_path,prepare_candidate=self.guide,
            checked_ready=lambda:True,resources=ready(self.f.registry))
        self.addCleanup(self.run.close)
        self.platform=platform

    def test_existing_material_reaches_preview_without_scan_download_conversion_or_reopening(self):
        with patch.object(self.run,'_scan',side_effect=AssertionError('must not scan')),patch.object(
                self.run.pipeline,'convert_once',side_effect=AssertionError('must not reconvert')),patch.object(
                self.session,'receive_file',side_effect=AssertionError('must not receive')),patch.object(
                self.run.lifecycle,'start',side_effect=AssertionError('must not open intake')):
            first=self.run.revise_to_preview()
            self.assertEqual(self.run.revise_to_preview(),first)
        self.assertEqual(first['state'],'awaiting_owner_decision')
        self.assertFalse(first['service_admission_open'])
        self.assertEqual(first['entry_id'],self.entry)
        self.assertEqual(self.f.wire.calls,2) # the retained rejection plus one new turn
        self.assertFalse(self.f.registry.rows('SELECT * FROM publication_confirmations'))
        counts={k:sum(e.get('type')=='intent' and e.get('kind')==k for e in self.session.http_events())
                for k in self.session.limits}
        self.assertEqual(counts,dict(auth=6,identity=3,list=0,message=0,download=1,upload=1,send=1))
        for name,raw in self.old_bytes.items():self.assertEqual(Path(name).read_bytes(),raw)

    def test_revision_plan_cannot_reopen_intake(self):
        before=self.platform.runner.call_count
        with self.assertRaisesRegex(ContractError,'cannot reopen intake'):self.run.run_to_preview()
        self.assertEqual(self.platform.runner.call_count,before)

    def test_parent_must_freeze_all_previous_failure_files(self):
        name=next(iter(self.receipt['files']));self.platform.frozen.pop(name)
        with self.assertRaisesRegex(ContractError,'complete previous'):self.run.revise_to_preview()
        self.assertEqual(self.f.wire.calls,1)

    def test_new_unknown_is_not_reissued(self):
        self.f.wire.mutate=lambda _: (_ for _ in ()).throw(OSError('synthetic timeout'))
        with self.assertRaisesRegex(ContractError,'unknown'):self.run.revise_to_preview()
        with self.assertRaises(ContractError):self.run.revise_to_preview()
        self.assertEqual(self.f.wire.calls,2)
        self.assertFalse(self.f.registry.rows('SELECT * FROM publications_v21'))

    def test_changed_previous_evidence_during_model_stops_before_delivery(self):
        def changed(value):
            path=self.old_root/(self.receipt['request_sha256']+'.response.local.json')
            path.write_bytes(b'changed')
            return value
        self.f.wire.mutate=changed
        with self.assertRaises(ContractError):self.run.revise_to_preview()
        self.assertFalse(self.f.registry.rows('SELECT * FROM publications_v21'))
        self.assertFalse(any(e.get('kind') in {'upload','send'} for e in self.session.http_events()))

    def test_operator_action_mismatch_stops_before_new_marker_or_factory(self):
        with patch.object(pilot_execute,'execution_inputs',return_value=(self.f.intake.runtime.worktree,
                dict(run_plan=str(self.run_path)),{})),patch.object(pilot_execute,'CliProcess') as factory:
            with self.assertRaisesRegex(ContractError,'action must match'):
                pilot_execute.execute(self.platform.root,self.platform.digest,'intake')
            factory.assert_not_called()
        self.assertFalse(list(self.platform.root.glob('entry-*.local.json')))

    def test_finite_operator_revision_uses_new_grant_and_cannot_restart(self):
        self.run.close();self.session.close()
        platform=self.platform;root=platform.root
        executable=root/'synthetic-executable';executable.write_bytes(b'fixture only');executable.chmod(0o700)
        manifest=dict(worktree=str(self.f.intake.runtime.worktree),source_commit='synthetic-commit',
            source_snapshot=dict(files={},sha256='synthetic-source'),approved_files=dict(platform.frozen))
        manifest['approved_files'][str(executable)]=file_sha(executable)
        manifest_path=root/'manifest.synthetic.json';manifest_path.write_text(encoded(manifest))
        platform.approval.update(source_manifest=str(manifest_path),source_manifest_sha256=file_sha(manifest_path),
            run_plan=str(self.run_path),guide_plan=str(self.guide_path),runtime_config=dict(self.f.intake.runtime.config,
                runtime_relative=str(self.f.intake.runtime.root.relative_to(self.f.intake.runtime.worktree))))
        for name in ('cli_guard','text_node','text_script','codex_binary'):platform.approval[name]=str(executable)
        self.f.f.save_approval()
        # New synthetic session has no requests yet; finalize only its header.
        journal=root/'journal.local.jsonl';self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=platform.approval))+'\n')
        process=Mock(side_effect=platform.runner)
        with patch.object(pilot_execute,'checked_source'),patch.object(pilot_execute,'CliProcess',return_value=process),patch.object(
                pilot_execute,'CodexTextWire',return_value=self.f.wire),patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(root,platform.digest,'revise')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
            self.assertEqual(result['result']['state'],'awaiting_owner_decision')
            count=process.call_count
            with self.assertRaises(FileExistsError):pilot_execute.execute(root,platform.digest,'revise')
            self.assertEqual(process.call_count,count)
        self.assertEqual(self.f.wire.calls,2)
        self.assertFalse(self.f.registry.rows('SELECT * FROM publication_confirmations'))
