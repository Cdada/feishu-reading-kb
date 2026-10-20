from datetime import datetime,timedelta,timezone
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from kb_custody_v21.content import ReadingGuideAdapter,empty_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.pilot_pipeline import PilotPipeline
from kb_custody_v21.pilot_text import MODEL,PilotTextSession
from kb_custody_v21.pilot_text_grounding import EVIDENCE_PROTOCOL
from kb_custody_v21.registry import encoded
from kb_custody_v21.staging import IntegrityError
import test_pilot_intake as intake_fixture
from pilot_pipeline_fixture import ready,call


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture=intake_fixture.PilotIntakeTests();self.fixture.setUp()
        self.intake=self.fixture.flow;self.registry=self.fixture.registry;self.runtime=self.fixture.runtime
        self.entry=self.fixture.receive()
        self.flow=PilotPipeline(self.intake,resources=ready(self.registry))
        converted=self.flow.convert_once()
        self.assertEqual(converted['processing_state'],'ready_candidate')
        task=self.intake.conversion.service.ledger.get_task(converted['task_id'])
        self.mapping=json.loads(task['quality_json'])['extraction']
        _,_,root=self.intake.conversion.publication_extraction(self.mapping['extraction_id'])
        self.body=(root/'source.md').read_bytes()
        now=datetime.now(timezone.utc)
        record=empty_record('街道研究合成样例','synthetic-title')
        consent=dict(event_id='synthetic-consent',asset_id=self.mapping['output_asset_id'],sha256=sha(self.body),
            extraction_id=self.mapping['extraction_id'],ranges=[[0,len(self.body.decode())]],model=MODEL,
            expires_at=(now+timedelta(minutes=20)).isoformat())
        request=ReadingGuideAdapter(PilotTextSession).prepare_request(record,self.body,self.mapping,consent)
        self.text_root=self.runtime.root/'conversion-work/pilot-text-synthetic';self.text_root.mkdir()
        approval=dict(approved=True,root=str(self.text_root),model=MODEL,effort='medium',billing='chatgpt_subscription_only',
            evidence_protocol=EVIDENCE_PROTOCOL,
            automatic_business_retries=0,max_business_turns=1,wait_seconds=600,
            starts_at=(now-timedelta(minutes=1)).isoformat(),expires_at=(now+timedelta(minutes=30)).isoformat(),
            request_sha256=[sha(encoded(request).encode())],account_sha256=sha(b'synthetic-account@example.invalid'))
        (self.text_root/'approval.local.json').write_text(encoded(approval))
        engine=ImagePolicies(self.registry)
        policy=engine.execute(engine.decide([],dict(material_type='unknown',reliable=False,basis_refs=[])),[],
            self.runtime.root/'conversion-work/no-images')
        self.data=dict(runtime=str(self.runtime.root),scope=asdict(self.intake.scope),text_root=str(self.text_root),
            text_approval_sha=sha(encoded(approval).encode()),source={str(root/'source.md'):sha(self.body)},
            marker=str(self.text_root/'synthetic-generation.txt'),prepare=dict(entry_id=self.entry,
                extraction_id=self.mapping['extraction_id'],record=record,consent=consent,policy=policy,
                font_path=os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc')))
        self.input=self.text_root/'synthetic-pipeline-input.json';self.save()

    def save(self):self.input.write_text(encoded(self.data))

    def run_pipeline(self,**kwargs):return call(self.input,**kwargs)

    def test_normal_intake_to_pdf_stops_before_delivery_or_owner_confirmation(self):
        result=self.run_pipeline()
        self.assertEqual(result['state'],'preview_ready_pending_delivery')
        self.assertEqual(result['delivery_state'],'NOT_SENT')
        self.assertEqual(sha(Path(result['preview_path']).read_bytes()),result['preview_sha256'])
        self.assertEqual(self.run_pipeline(),result)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertFalse(self.registry.rows('SELECT * FROM active_publications_v21'))
        self.assertEqual(self.flow.status(self.entry)['state'],'preview_ready_pending_delivery')

    def crash(self,point):
        # CI cannot inherit the parent's in-process sys.path. Remove repository
        # entries even on a developer machine; keep separately installed deps.
        env=dict(os.environ)
        env['PYTHONPATH']=os.pathsep.join(p for p in env.get('PYTHONPATH','').split(os.pathsep)
            if p and not Path(p).resolve().is_relative_to(intake_fixture.WORKTREE))
        child=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('pilot_pipeline_fixture.py')),str(self.input),point],
            capture_output=True,text=True,timeout=30,env=env)
        self.assertEqual(child.returncode,77,child.stderr)
        return self.run_pipeline()

    def test_real_child_death_after_text_reuses_exact_validated_guide(self):
        result=self.crash('after_text')
        self.assertEqual(result['publication_state'],'prepared')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)
        # Death-owned leases remain; taking a new lock is not proof of release.
        self.assertTrue(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))
        self.assertTrue(self.registry.rows("SELECT * FROM asset_references WHERE kind='staging_dependency' AND released_at IS NULL"))

    def test_real_child_death_after_candidate_commit_reuses_one_pdf(self):
        result=self.crash('after_preview_commit')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)
        self.assertEqual(self.run_pipeline()['publication_id'],result['publication_id'])

    def test_real_child_death_after_stage_preserves_same_version(self):
        result=self.crash('after_stage')
        self.assertEqual(result['version'],1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_candidates')),1)

    def test_unknown_text_never_reinvokes_generator(self):
        with patch('pilot_pipeline_fixture.PilotTextSession.generate_guide',side_effect=OSError('synthetic failure')) as generate:
            with self.assertRaises(OSError):self.run_pipeline()
            with self.assertRaises(ContractError):self.run_pipeline()
            self.assertEqual(generate.call_count,1)
        self.assertEqual(self.flow.status(self.entry)['state'],'text_receipt_needs_check')
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_precommit_render_failure_stays_explicit_and_does_not_render_again(self):
        with patch('kb_custody_v21.publication.render_preview',side_effect=OSError('synthetic render failure')) as render:
            with self.assertRaises(OSError):self.run_pipeline()
            with self.assertRaisesRegex(ContractError,'interrupted before commit'):self.run_pipeline()
            self.assertEqual(render.call_count,1)
        self.assertEqual(self.flow.status(self.entry)['state'],'preview_interrupted_needs_review')

    def test_resource_pause_after_guide_reuses_result_after_recovery(self):
        from kb_custody_v21.pilot_resources import PilotResources
        original=PilotResources.check
        count=0
        def check(gate):
            nonlocal count
            count+=1
            return dict(state='paused_resources') if count==2 else original(gate)
        with patch.object(PilotResources,'check',check):
            self.assertEqual(self.run_pipeline()['state'],'paused_resources')
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))
        self.assertEqual(self.run_pipeline()['state'],'preview_ready_pending_delivery')

    def test_busy_lock_and_resource_pause_start_no_text(self):
        with self.intake._serial():self.assertEqual(self.run_pipeline()['state'],'busy')
        with patch('kb_custody_v21.pilot_resources.PilotResources.check',return_value=dict(state='paused_resources')):
            self.assertEqual(self.run_pipeline()['state'],'paused_resources')
        self.assertFalse(Path(self.data['marker']).exists())

    def test_original_corruption_after_text_preserves_acceptance_and_blocks_preview(self):
        def corrupt(point):
            if point=='after_text':
                (self.runtime.root/self.registry.entry(self.entry)['relative_path']).write_bytes(b'corrupt')
        with self.assertRaises(IntegrityError):self.run_pipeline(hook=corrupt)
        receipt=self.registry.receipt(self.entry)
        self.assertEqual(receipt['source_staging_status'],'accepted')
        self.assertEqual(receipt['custody_health'],'suspect')
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_changed_policy_cannot_rebind_prepared_attempt(self):
        self.run_pipeline()
        self.data['prepare']['policy']['synthetic-extra']='changed';self.save()
        with self.assertRaisesRegex(ContractError,'plan changed'):self.run_pipeline()

    def test_changed_candidate_bytes_not_silently_replaced(self):
        result=self.run_pipeline();Path(result['preview_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError,'candidate changed'):self.run_pipeline()
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)

    def test_unqualified_conversion_does_not_invoke_text(self):
        with patch.object(self.intake.conversion.__class__,'publication_extraction',
                          side_effect=ContractError('conversion quality review is unresolved')):
            with self.assertRaisesRegex(ContractError,'quality review'):self.run_pipeline()
        self.assertFalse(Path(self.data['marker']).exists())

    def test_font_changed_during_generation_stops_before_preview(self):
        original=Path(self.data['prepare']['font_path'])
        font=self.text_root/'synthetic-font.ttf';font.write_bytes(original.read_bytes())
        self.data['prepare']['font_path']=str(font);self.save()
        def change(point):
            if point=='after_text':font.write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError,'inputs changed'):self.run_pipeline(hook=change)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_candidate_and_controller_identity_rollback_together(self):
        original=self.flow._save
        def save(flow,db,entry,phase,data):
            if phase=='prepared':raise OSError('synthetic commit callback failure')
            return original(db,entry,phase,data)
        with patch.object(PilotPipeline,'_save',save):
            with self.assertRaises(OSError):self.run_pipeline()
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))
        self.assertIsNone(self.flow._event(self.entry,'prepared'))
        with self.assertRaisesRegex(ContractError,'interrupted before commit'):self.run_pipeline()

    def test_status_tracks_delivered_owner_confirmation_publish_and_withdrawal(self):
        result=self.run_pipeline();pid=result['publication_id'];scope=self.intake.scope;previews=self.flow.previews
        self.assertEqual(self.flow.status(self.entry)['delivery_state'],'NO_VERIFIED_RECEIPT')
        previews.bind_delivery(pid,app_id=scope.app_id,chat_id=scope.chat_id,message_id='synthetic-preview',
            pdf_sha256=result['preview_sha256'],receipt_ref='synthetic-receipt',outcome='sent')
        self.assertEqual(self.flow.status(self.entry)['state'],'awaiting_owner_decision')
        decision=dict(app_id=scope.app_id,chat_id=scope.chat_id,user_id=scope.user_id,
            message_id='synthetic-decision',reply_to='synthetic-preview',text='确认入库')
        with patch.object(previews.publisher,'publish',side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):previews.decide(**decision)
        self.assertEqual(self.flow.status(self.entry)['state'],'confirmed')
        self.assertFalse(self.flow.status(self.entry)['active_publication'])
        previews.decide(**decision)
        self.assertEqual(self.flow.status(self.entry)['state'],'published')
        self.assertTrue(self.flow.status(self.entry)['active_publication'])
        previews.publisher.withdraw(pid,'synthetic-withdraw')
        self.assertEqual(self.flow.status(self.entry)['state'],'withdrawn')
        self.assertFalse(self.flow.status(self.entry)['active_publication'])
        # Lifecycle reporting never releases unfinished retention requirements.
        self.assertTrue(self.registry.rows("SELECT * FROM asset_references WHERE kind='staging_dependency' AND released_at IS NULL"))
