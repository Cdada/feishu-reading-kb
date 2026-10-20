"""The finite operator continues only the frozen pre-turn revision evidence."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from kb_custody_v21.contracts import ContractError,stable_key
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_continuation import blocked_receipt
from kb_custody_v21.pilot_guide_preparation import PilotGuidePreparation
from kb_custody_v21.pilot_run import PilotBoundedRun
from kb_custody_v21.pilot_revision import digest
from kb_custody_v21.pilot_resources import ResourceSample
from kb_custody_v21.registry import encoded
from pilot_pipeline_fixture import ready
from test_pilot_continuation import source_evidence
import test_pilot_revision_run as fixture
import pilot_execute


class ContinuationRunTests(unittest.TestCase):
    def setUp(self):
        self.r=fixture.RevisionRunTests();self.r.setUp();self.addCleanup(self.r.doCleanups)
        r=self.r;f=r.f;platform=r.platform;self.f=f;self.platform=platform;self.entry=r.entry
        r.run.close();r.session.close()
        source_evidence(platform.root,platform.approval);f.f.save_approval()
        path=platform.root/'journal.local.jsonl';self.assertEqual(len(path.read_text().splitlines()),1)
        path.write_text(encoded(dict(approval=platform.approval))+'\n')
        session=f.f.make(False);guide=PilotGuidePreparation(session,r.guide_path,wire=f.wire)
        run=PilotBoundedRun(session,f.intake,r.run_path,prepare_candidate=guide,
            checked_ready=lambda:True,resources=ready(f.registry))
        f.wire.mutate=lambda _:dict(status='blocked_before_turn')
        with self.assertRaisesRegex(ContractError,'blocked_before_turn'):run.revise_to_preview()
        _,derived,consent,args=guide._inputs(run.pipeline,self.entry)
        child=f.intake.runtime.root/'conversion-work'/('pilot-text-'+stable_key(session.approval_sha,guide.digest))
        _,mapping,extraction=f.intake.conversion.publication_extraction(derived['extraction_id'])
        self.binding=dict(previous_plan_sha256=digest(run.pipeline.attempt_event(self.entry,'plan')),
            previous_text_root=str(child),previous_text_approval_sha256=file_sha(child/'approval.local.json'),
            previous_consent=consent,previous_receipt_sha256='0'*64,previous_parent_root=str(platform.root),
            previous_revision_sha256=digest(run.pipeline._event(self.entry,'revision')))
        self.receipt=blocked_receipt(self.binding,f.plan['record'],(extraction/'source.md').read_bytes(),mapping,**args)
        self.binding['previous_receipt_sha256']=digest(self.receipt)
        self.old_bytes={name:Path(name).read_bytes() for name in self.receipt['files']}
        run.close();session.close()
        platform.root=platform.root.parent.parent/'continuation'/'p11-requests';platform.root.mkdir(parents=True)
        platform.session=platform.root/'approval.local.json'
        self.run_path=platform.root/'run.synthetic.json';plan=json.loads(r.run_path.read_bytes())
        plan['continuation']=self.binding;self.run_path.write_text(encoded(plan))
        self.guide_path=platform.root/'guide.synthetic.json'
        guide_plan=dict(f.plan,run_plan_path=str(self.run_path),run_plan_sha256=file_sha(self.run_path))
        self.guide_path.write_text(encoded(guide_plan))
        platform.frozen.update({str(self.run_path):file_sha(self.run_path),str(self.guide_path):file_sha(self.guide_path),
            **self.receipt['files']})
        platform.approval.update(root=str(platform.root),guide_plan_sha256=file_sha(self.guide_path))
        f.f.save_approval();self.session=f.f.make(False);f.wire.mutate=lambda value:value
        self.guide=PilotGuidePreparation(self.session,self.guide_path,wire=f.wire)
        self.run=PilotBoundedRun(self.session,f.intake,self.run_path,prepare_candidate=self.guide,
            checked_ready=lambda:True,resources=ready(f.registry))
        self.addCleanup(self.run.close)

    def test_same_material_reaches_preview_without_intake_or_automatic_publication(self):
        with patch.object(self.run,'_scan',side_effect=AssertionError('scan')),patch.object(
                self.run.pipeline,'convert_once',side_effect=AssertionError('convert')),patch.object(
                self.session,'receive_file',side_effect=AssertionError('receive')),patch.object(
                self.run.lifecycle,'start',side_effect=AssertionError('open intake')):
            result=self.run.revise_to_preview();self.assertEqual(result,self.run.revise_to_preview())
        self.assertEqual(result['state'],'awaiting_owner_decision');self.assertEqual(result['entry_id'],self.entry)
        self.assertFalse(result['service_admission_open']);self.assertEqual(self.f.wire.calls,3)
        self.assertFalse(self.f.registry.rows('SELECT * FROM publication_confirmations'))
        for name,raw in self.old_bytes.items():self.assertEqual(Path(name).read_bytes(),raw)

    def test_new_parent_must_freeze_stopped_receipt_and_executed_source(self):
        for name,value in self.receipt['files'].items():
            self.platform.frozen.pop(name)
            with self.assertRaisesRegex(ContractError,'complete previous stopped'):self.run.revise_to_preview()
            self.platform.frozen[name]=value
        self.assertEqual(self.f.wire.calls,2)

    def test_continuation_cannot_reopen_intake(self):
        with self.assertRaisesRegex(ContractError,'cannot reopen intake'):self.run.run_to_preview()
        self.assertEqual(self.f.wire.calls,2)

    def test_operator_requires_explicit_continue_action_before_marker(self):
        with patch.object(pilot_execute,'execution_inputs',return_value=(self.f.intake.runtime.worktree,
                dict(run_plan=str(self.run_path)),{})),patch.object(pilot_execute,'CliProcess') as factory:
            for action in ('revise','intake'):
                with self.assertRaisesRegex(ContractError,'action must match'):
                    pilot_execute.execute(self.platform.root,self.platform.digest,action)
            factory.assert_not_called()
        self.assertFalse(list(self.platform.root.glob('entry-*.local.json')))

    def test_continue_action_cannot_use_revision_only_plan(self):
        with patch.object(pilot_execute,'execution_inputs',return_value=(self.f.intake.runtime.worktree,
                dict(run_plan=str(self.r.run_path)),{})),patch.object(pilot_execute,'CliProcess') as factory:
            with self.assertRaisesRegex(ContractError,'action must match'):
                pilot_execute.execute(self.platform.root,self.platform.digest,'continue')
            factory.assert_not_called()

    def test_finite_operator_continue_succeeds_once_and_keeps_original_budget(self):
        self.run.close();self.session.close()
        platform=self.platform;root=platform.root
        executable=root/'synthetic-executable';executable.write_bytes(b'fixture only');executable.chmod(0o700)
        manifest=dict(worktree=str(self.f.intake.runtime.worktree),source_commit='synthetic-commit',
            source_snapshot=dict(files={},sha256='synthetic-source'),approved_files=dict(platform.frozen))
        manifest['approved_files'][str(executable)]=file_sha(executable)
        path=root/'manifest.synthetic.json';path.write_text(encoded(manifest))
        platform.approval.update(source_manifest=str(path),source_manifest_sha256=file_sha(path),
            run_plan=str(self.run_path),guide_plan=str(self.guide_path),runtime_config=dict(self.f.intake.runtime.config,
                runtime_relative=str(self.f.intake.runtime.root.relative_to(self.f.intake.runtime.worktree))))
        for name in ('cli_guard','text_node','text_script','codex_binary'):platform.approval[name]=str(executable)
        self.f.f.save_approval();journal=root/'journal.local.jsonl'
        self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=platform.approval))+'\n')
        process=Mock(side_effect=platform.runner)
        with patch.object(pilot_execute,'checked_source'),patch.object(pilot_execute,'CliProcess',return_value=process),patch.object(
                pilot_execute,'CodexTextWire',return_value=self.f.wire),patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(root,platform.digest,'continue')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
            self.assertEqual(result['result']['state'],'awaiting_owner_decision')
            count=process.call_count
            with self.assertRaises(FileExistsError):pilot_execute.execute(root,platform.digest,'continue')
            self.assertEqual(process.call_count,count)
        self.assertEqual(self.f.wire.calls,3)
        for name,raw in self.old_bytes.items():self.assertEqual(Path(name).read_bytes(),raw)
