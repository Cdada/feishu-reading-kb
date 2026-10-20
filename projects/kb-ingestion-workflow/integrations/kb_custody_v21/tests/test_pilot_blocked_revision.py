"""One new grant for a proved pre-turn stop of the same prepared PDF."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_guide_preparation import PilotGuidePreparation
from kb_custody_v21.pilot_revision import digest, failed_receipt, check_new_approval
from kb_custody_v21.pilot_run import PilotBoundedRun
from kb_custody_v21.registry import encoded
from kb_custody_v21.visual_reading import VisualReader
from kb_custody_v21.pilot_resources import ResourceSample
import pilot_execute
from pilot_pipeline_fixture import ready
from test_pilot_continuation import source_evidence
import test_pilot_prepared_material as fixture


class BlockedRevisionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PreparedMaterialTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;f.configure(pdf=True,reading=True)
        f.run.close();f.session.close()
        self.platform=p=f.f.f
        source_evidence(p.root,p.approval);f.f.save_approval()
        (p.root/'journal.local.jsonl').write_text(encoded(dict(approval=p.approval))+'\n')
        f.session=f.f.make(False)
        f.guide=PilotGuidePreparation(f.session,f.path,wire=f.wire)
        f.run=PilotBoundedRun(f.session,f.intake,f.run_path,prepare_candidate=f.guide,
            checked_ready=lambda:True,resources=ready(f.registry))
        self.addCleanup(f.run.close)
        f.wire.mutate=lambda _:dict(status='blocked_before_turn')
        with self.assertRaisesRegex(ContractError,'blocked_before_turn'):f.run.prepared_to_preview()
        self.old_root=next((f.runtime.root/'conversion-work').glob('pilot-text-*'))
        _,_,consent,_=f.guide._inputs(f.run.pipeline,f.entry)
        self.old_plan=f.run.pipeline._event(f.entry,'plan')
        self.binding=dict(previous_plan_sha256=digest(self.old_plan),previous_text_root=str(self.old_root),
            previous_text_approval_sha256=file_sha(self.old_root/'approval.local.json'),
            previous_consent=consent,previous_receipt_sha256='0'*64,previous_parent_root=str(p.root))
        self.receipt=self.inspect();self.binding['previous_receipt_sha256']=digest(self.receipt)
        self.before={name:Path(name).read_bytes() for name in self.receipt['files']}
        self.old_task=deepcopy(f.intake.conversion.service.ledger.get_task(f.task_id))
        self.old_journal=(p.root/'journal.local.jsonl').read_bytes()
        f.run.close();f.session.close()
        p.root=p.root.parent/'blocked-revision'/'p11-requests';p.root.mkdir(parents=True)
        p.session=p.root/'approval.local.json'
        self.run_path=p.root/'run.synthetic.json'
        self.run_plan=dict(f.run_plan,revision=dict(entry_id=f.entry,binding=self.binding))
        self.run_path.write_text(encoded(self.run_plan))
        self.guide_path=p.root/'guide.synthetic.json'
        self.plan=dict(f.plan,run_plan_path=str(self.run_path),run_plan_sha256=file_sha(self.run_path))
        self.guide_path.write_text(encoded(self.plan))
        p.frozen.update({str(self.run_path):file_sha(self.run_path),str(self.guide_path):file_sha(self.guide_path),
            **self.receipt['files']})
        p.approval.update(root=str(p.root),operation='revise',guide_plan_sha256=file_sha(self.guide_path))
        f.f.save_approval()
        self.session=f.f.make(False);f.wire.mutate=lambda value:value
        self.guide=PilotGuidePreparation(self.session,self.guide_path,wire=f.wire)
        self.run=PilotBoundedRun(self.session,f.intake,self.run_path,prepare_candidate=self.guide,
            checked_ready=lambda:True,resources=ready(f.registry))
        self.addCleanup(self.run.close)

    def inspect(self):
        f=self.f
        _,mapping,root=f.intake.conversion.publication_extraction(f.mapping['extraction_id'])
        return failed_receipt(self.binding,f.plan['record'],(root/'source.md').read_bytes(),mapping,
            curation_data=f.plan['curation_data'],reader_context=f.plan['reader_context'])

    def assert_history(self):
        f=self.f
        for name,raw in self.before.items():self.assertEqual(Path(name).read_bytes(),raw)
        self.assertEqual((Path(self.binding['previous_parent_root'])/'journal.local.jsonl').read_bytes(),self.old_journal)
        self.assertEqual(f.intake.conversion.service.ledger.get_task(f.task_id),self.old_task)
        self.assertEqual(self.run.pipeline._event(f.entry,'plan'),self.old_plan)

    def test_same_pdf_images_and_partial_observation_reach_one_preview_without_reconversion(self):
        f=self.f
        with patch.object(self.run,'_scan',side_effect=AssertionError('no scan')),patch.object(
                self.session,'receive_file',side_effect=AssertionError('no receive')),patch.object(
                self.run.pipeline,'convert_once',side_effect=AssertionError('no conversion')),patch.object(
                VisualReader,'read',side_effect=AssertionError('no new vision')):
            result=self.run.revise_to_preview();self.assertEqual(result,self.run.revise_to_preview())
        self.assertEqual(result['entry_id'],f.entry)
        self.assertEqual(result['state'],'awaiting_owner_decision')
        self.assertEqual(f.wire.calls,2) # retained blocked response plus one successor
        self.assertEqual(self.old_task['status'],'needs_review')
        self.assert_history()
        self.assertFalse(f.registry.rows('SELECT * FROM publication_confirmations'))
        row=self.run.pipeline.publisher._row(result['publication_id'])
        tree=self.run.pipeline.publisher._tree(row,result['publication_id']+'.candidate')
        self.assertEqual(json.loads((tree/'curation.json').read_bytes())['visual_observations'],
            f.policy['execution_result']['visual_observations'])
        for item in f.policy['execution_result']['per_asset_results']:
            self.assertEqual((tree/'提取/images'/Path(item['path']).name).read_bytes(),Path(item['path']).read_bytes())

    def test_incomplete_unknown_or_received_journal_cannot_authorize_revision(self):
        path=self.old_root/'journal.local.jsonl';raw=path.read_bytes()
        events=[json.loads(line) for line in raw.splitlines()]
        chains=[events[:2],events+events[1:2]]+[events[:2]+[dict(events[2],state=s)]
            for s in ('unknown','received','received_rejected','validated')]
        for chain in chains:
            path.write_text(''.join(encoded(e)+'\n' for e in chain))
            with self.assertRaises(ContractError):self.inspect()
        path.write_bytes(raw)

    def test_old_receipt_files_and_partial_artifacts_are_checked(self):
        for name,raw in self.before.items():
            Path(name).write_text('{}')
            with self.assertRaises(ContractError):self.inspect()
            Path(name).write_bytes(raw)
        for suffix in ('response','validated','grounded','grounding'):
            path=self.old_root/(self.receipt['request_sha256']+'.'+suffix+'.local.json');path.write_text('{}')
            with self.assertRaisesRegex(ContractError,'artifact'):self.inspect()
            path.unlink()
        previous=self.binding.pop('previous_parent_root')
        with self.assertRaisesRegex(ContractError,'received_rejected'):self.inspect()
        self.binding['previous_parent_root']=previous

    def test_new_parent_must_freeze_entire_stopped_receipt(self):
        self.platform.frozen.pop(str(self.old_root/'journal.local.jsonl'))
        with self.assertRaisesRegex(ContractError,'complete previous failed'):self.run.revise_to_preview()
        self.assertEqual(self.f.wire.calls,1)

    def test_new_child_cannot_reuse_old_parent_approval(self):
        self.run.revise_to_preview()
        root=next(p for p in (self.f.runtime.root/'conversion-work').glob('pilot-text-*') if p!=self.old_root)
        path=root/'approval.local.json';approval=json.loads(path.read_bytes())
        approval['parent_approval_sha256']=file_sha(Path(self.binding['previous_parent_root'])/'approval.local.json')
        path.write_text(encoded(approval))
        with self.assertRaisesRegex(ContractError,'separately approved new parent'):
            check_new_approval(root,file_sha(path),self.binding)

    def test_unknown_successor_cannot_reissue_or_replace_budget(self):
        f=self.f
        f.wire.mutate=lambda _:(_ for _ in ()).throw(OSError('synthetic timeout'))
        with self.assertRaisesRegex(ContractError,'unknown'):self.run.revise_to_preview()
        with self.assertRaises(ContractError):self.run.revise_to_preview()
        self.assertEqual(f.wire.calls,2)
        self.assertFalse(f.registry.rows('SELECT * FROM publications_v21'));self.assert_history()

    def test_modified_old_source_during_generation_prevents_preview(self):
        def change(value):
            (Path(self.binding['previous_parent_root'])/'executed-source.local.json').write_text('{}')
            return value
        self.f.wire.mutate=change
        with self.assertRaises(ContractError):self.run.revise_to_preview()
        self.assertFalse(self.f.registry.rows('SELECT * FROM publications_v21'))
        self.assertFalse(any(e.get('kind') in {'upload','send'} for e in self.session.http_events()))

    def test_prepared_revision_requires_explicit_revision_action_and_same_entry(self):
        with self.assertRaisesRegex(ContractError,'revision action'):self.run.prepared_to_preview()
        self.run.close()
        plan=deepcopy(self.run_plan);plan['prepared_material']['entry_id']='entry_'+'b'*32
        self.run_path.write_text(encoded(plan));self.platform.frozen[str(self.run_path)]=file_sha(self.run_path)
        with self.assertRaisesRegex(ContractError,'same existing entry'):
            PilotBoundedRun(self.session,self.f.intake,self.run_path,prepare_candidate=self.guide,
                checked_ready=lambda:True,resources=ready(self.f.registry))
        self.assertEqual(self.f.wire.calls,1)


    def test_finite_revise_operator_uses_new_grant_and_cannot_restart(self):
        self.run.close();self.session.close()
        p=self.platform;root=p.root
        executable=root/'synthetic-executable';executable.write_bytes(b'fixture only');executable.chmod(0o700)
        p.frozen[str(executable)]=file_sha(executable)
        manifest=dict(worktree=str(self.f.runtime.worktree),source_commit='synthetic-commit',
            source_snapshot=dict(files={},sha256='synthetic-source'),approved_files=dict(p.frozen))
        manifest_path=root/'manifest.synthetic.json';manifest_path.write_text(encoded(manifest))
        p.approval.update(source_manifest=str(manifest_path),source_manifest_sha256=file_sha(manifest_path),
            run_plan=str(self.run_path),guide_plan=str(self.guide_path),runtime_config=dict(self.f.runtime.config,
                runtime_relative=str(self.f.runtime.root.relative_to(self.f.runtime.worktree))))
        for name in ('cli_guard','text_node','text_script','codex_binary'):p.approval[name]=str(executable)
        self.f.f.save_approval()
        journal=root/'journal.local.jsonl';self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=p.approval))+'\n')
        process=Mock(side_effect=p.runner)
        with patch.object(pilot_execute,'checked_source'),patch.object(pilot_execute,'CliProcess',return_value=process),patch.object(
                pilot_execute,'CodexTextWire',return_value=self.f.wire),patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(root,p.digest,'revise')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
            self.assertEqual(result['result']['state'],'awaiting_owner_decision')
            self.assertEqual(result['physical_requests'],dict(auth=6,identity=3,list=0,message=0,download=1,upload=1,send=1))
            count=process.call_count
            with self.assertRaises(FileExistsError):pilot_execute.execute(root,p.digest,'revise')
            self.assertEqual(process.call_count,count)
        self.assertEqual(self.f.wire.calls,2);self.assert_history()
