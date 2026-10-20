"""Prepared image expressions reuse existing intake, PDF and transport fixtures."""
import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from kb_custody_v21.content import empty_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.material_review import MaterialReviews
from kb_custody_v21.pdf_assets import PdfAssetSupplement
from kb_custody_v21.pilot_guide_preparation import PilotGuidePreparation
from kb_custody_v21.pilot_run import PilotBoundedRun
from kb_custody_v21.registry import encoded
from kb_custody_v21.visual_reading import VisualReader
from kb_custody_v21.pilot_resources import ResourceSample
import pilot_execute
from pilot_pipeline_fixture import ready
from test_material_review import report_for
from test_pdf_assets import illustrated_pdf
from test_pilot_delivery import delivery_response
from test_pilot_text import Wire
import test_pilot_availability as availability
import test_pilot_notices as notices


class PreparedMaterialTests(unittest.TestCase):
    def setUp(self):
        self.f=notices.NoticeTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.a=availability.AvailabilityTests();self.a.setUp();self.addCleanup(self.a.doCleanups)
        self.intake,self.registry,self.runtime=self.a.intake,self.a.registry,self.a.runtime
        self.wire=Wire()

    def configure(self, *, pdf=False, qualify=True, reading=False, edit=lambda plan: None, omit_image_binding=False):
        if pdf:
            raw,_=illustrated_pdf();name='synthetic.pdf'
        else:
            output=BytesIO();Image.new('RGB',(30,20),'navy').save(output,'PNG')
            raw=('# 合成图文材料\n\n树荫影响步行路线。\n\n![合成图](data:image/png;base64,'+
                 base64.b64encode(output.getvalue()).decode()+')\n').encode();name='synthetic.md'
        scope=self.intake.scope
        message=dict(appId=scope.app_id,chatId=scope.chat_id,senderId=scope.user_id,chatType='group',
            messageId='om_prepared_synthetic',resources=[dict(type='file',fileKey='file_synthetic',fileName=name,sizeBytes=len(raw))])
        self.entry=self.intake.receive(message,0,MockAttachment(raw).download)['entry_id']
        converted=self.intake.work_once();self.task_id=converted['task_id']
        task=self.intake.conversion.service.ledger.get_task(self.task_id)
        original_mapping=json.loads(task['quality_json'])['extraction']
        if pdf:
            self.assertEqual(task['status'],'needs_review')
            supplement=PdfAssetSupplement(self.registry).prepare(self.entry,original_mapping['extraction_id'])
            mapping=supplement['mapping']
        else:
            self.assertEqual(task['status'],'ready_candidate');mapping=original_mapping
        images=self.intake.conversion.registered_images(mapping['extraction_id'])
        engine=ImagePolicies(self.registry)
        reader=None
        if reading:
            images=[dict(i,visual_type='dense',basis_refs=['synthetic']) for i in images]
            class Provider:
                is_mock=True;model='synthetic-image-reader'
                def read(self,*args,**kwargs):
                    return dict(transcribed_text='合成图形',interpretation='合成图示说明。',
                        reading_status='partially_read',unresolved=['小字无法辨认'])
            consents={i['asset_id']:dict(event_id='synthetic-image-consent',asset_id=i['asset_id'],
                sha256=i['sha256'],model=Provider.model,
                expires_at=(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat()) for i in images}
            decision=engine.decide(images,dict(material_type='unknown',reliable=True,basis_refs=['synthetic']),
                current=dict(strategy=4,confirmation_ref='synthetic-choice'),selected=dict(reading=[images[0]['asset_id']]),
                dense_choices={i['asset_id']:'text_only' for i in images},consent_assets=list(consents),
                capabilities=dict(archive='configured_pending_window',preview=True,reading=True))
            reader=VisualReader(self.runtime,Provider(),consents)
        else:
            decision=engine.decide(images,dict(material_type='unknown',reliable=False,basis_refs=[]))
        policy=engine.execute(decision,images,self.runtime.root/'conversion-work/prepared-images',reader=reader)
        if not pdf:
            replacements={}
            for item in images:
                output=next((o for o in policy['execution_result']['per_asset_results'] if item['asset_id'] in o['derived_from']),None)
                replacements[item['original_target']]=dict(asset_id=item['asset_id'],local_path='images/'+Path(output['path']).name if output else None,
                    representation_asset_id=output['asset_id'] if output else None)
            _,mapping=self.intake.conversion.reexpress(mapping['extraction_id'],replacements)
        _,_,extracted=self.intake.conversion.extraction(mapping['extraction_id'])
        if pdf and qualify:
            MaterialReviews(self.registry).record(self.entry,mapping['extraction_id'],report_for(self.runtime,mapping))
        self.mapping,self.images,self.policy=mapping,images,policy
        body=(extracted/'source.md').read_bytes()
        sample=self.f.f.root/name;sample.write_bytes(raw)
        self.run_path=self.f.f.root/'run.synthetic.json'
        self.material=dict(entry_id=self.entry,task_id=self.task_id,extraction_id=mapping['extraction_id'])
        self.run_plan=dict(reference='P11-single-material-v1',sample_path=str(sample),sample_sha256=sha(raw),
            max_pages=1,lookback_seconds=3600,prepared_material=self.material)
        self.run_path.write_text(encoded(self.run_plan))
        font=Path(os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc')).resolve()
        self.plan=dict(reference='P11-guide-derivation-v2',run_plan_path=str(self.run_path),run_plan_sha256=file_sha(self.run_path),
            body_sha256=sha(body),record=empty_record('合成图文材料','synthetic-title'),ranges=[[0,len(body.decode())]],
            reader_context=None,curation_data={},policy=policy,font_path=str(font),
            account_sha256=sha(b'synthetic-account@example.invalid'),wait_seconds=600,material=self.material,images=images)
        edit(self.plan)
        self.path=self.f.f.root/'guide.synthetic.json';self.path.write_text(encoded(self.plan))
        entry=self.registry.entry(self.entry)
        paths=[sample,self.run_path,self.path,font,self.runtime.root/entry['relative_path'],extracted/'source.md',extracted/'extraction-map.json']
        paths += [Path(i['path']) for i in images+policy['execution_result']['per_asset_results']]
        self.f.f.frozen.update({str(p):file_sha(p) for p in paths})
        if omit_image_binding:self.f.f.frozen.pop(images[0]['path'])
        self.f.f.approval.update(operation='prepare',guide_plan_sha256=file_sha(self.path),
            limits=dict(auth=6,identity=3,list=0,message=0,download=1,upload=1,send=1),maximum_total=12)
        self.f.save_approval()
        original=self.f.f.runner.side_effect
        self.f.f.runner.side_effect=lambda c:delivery_response(c) if c.get('preview') else original(c)
        self.session=self.f.make(False)
        self.guide=PilotGuidePreparation(self.session,self.path,wire=self.wire)
        self.run=PilotBoundedRun(self.session,self.intake,self.run_path,prepare_candidate=self.guide,
            checked_ready=lambda:True,resources=ready(self.registry))
        self.addCleanup(self.run.close)

    def test_markdown_images_reach_same_preview_without_conversion_or_vision_calls(self):
        self.configure()
        with patch.object(self.intake.conversion.worker,'run_once',side_effect=AssertionError('no conversion')):
            result=self.run.prepared_to_preview()
            count=self.f.f.runner.call_count
            self.assertEqual(self.run.prepared_to_preview(),result)
        self.assertEqual(self.f.f.runner.call_count,count)
        self.assertEqual(result['state'],'awaiting_owner_decision');self.assertEqual(self.wire.calls,1)
        row=self.run.pipeline.publisher._row(result['publication_id'])
        tree=self.run.pipeline.publisher._tree(row,result['publication_id']+'.candidate')
        metadata=json.loads((tree/'metadata.json').read_bytes())
        self.assertEqual(metadata['source_record']['source_reading']['figures']['status'],'not_read')
        self.assertEqual((tree/'提取/source.md').read_bytes(),self.intake.conversion.extraction(self.mapping['extraction_id'])[2].joinpath('source.md').read_bytes())
        for output in self.policy['execution_result']['per_asset_results']:
            self.assertEqual((tree/'提取/images'/Path(output['path']).name).read_bytes(),Path(output['path']).read_bytes())
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_qualified_pdf_continues_without_changing_needs_review_task(self):
        self.configure(pdf=True)
        before=self.intake.conversion.service.ledger.get_task(self.task_id)
        result=self.run.prepared_to_preview()
        self.assertEqual(result['state'],'awaiting_owner_decision')
        self.assertEqual(self.intake.conversion.service.ledger.get_task(self.task_id),before)
        self.assertEqual(before['status'],'needs_review');self.assertEqual(self.wire.calls,1)
        self.assertEqual(len(self.images),2)

    def test_unqualified_pdf_stops_before_platform_or_model(self):
        self.configure(pdf=True,qualify=False)
        with self.assertRaisesRegex(ContractError,'quality review'):self.run.prepared_to_preview()
        self.assertEqual(self.wire.calls,0);self.f.f.runner.assert_not_called()

    def test_image_file_not_in_approved_manifest_stops_before_model(self):
        self.configure(omit_image_binding=True)
        with self.assertRaisesRegex(ContractError,'explicitly frozen'):self.run.prepared_to_preview()
        self.assertEqual(self.wire.calls,0)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_wrong_task_in_guide_plan_cannot_select_another_expression(self):
        self.configure(edit=lambda p:p.update(material=dict(p['material'],task_id='task_'+'a'*32)))
        with self.assertRaisesRegex(ContractError,'approved material and task'):self.run.prepared_to_preview()
        self.assertEqual(self.wire.calls,0)

    def test_image_changed_during_model_wait_stops_before_preview(self):
        self.configure()
        def mutate(value):
            Path(self.images[0]['path']).write_bytes(b'changed');return value
        self.wire.mutate=mutate
        with self.assertRaises(ContractError):self.run.prepared_to_preview()
        self.assertEqual(self.wire.calls,1)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_unknown_text_result_cannot_be_reissued_by_prepared_route(self):
        self.configure()
        self.wire.mutate=lambda _:(_ for _ in ()).throw(OSError('synthetic timeout'))
        with self.assertRaises(ContractError):self.run.prepared_to_preview()
        with self.assertRaises(ContractError):self.run.prepared_to_preview()
        self.assertEqual(self.wire.calls,1)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_prepared_plan_cannot_reopen_intake(self):
        self.configure()
        for call in (self.run.run_to_preview,self.run.run_to_conversion):
            with self.assertRaisesRegex(ContractError,'cannot reopen intake'):call()
        self.assertEqual(self.wire.calls,0);self.f.f.runner.assert_not_called()

    def test_resource_pause_preserves_existing_material_without_platform_calls(self):
        self.configure()
        with patch.object(self.run.pipeline.resources,'check',return_value=dict(state='paused_resources')):
            result=self.run.prepared_to_preview()
        self.assertEqual(result['state'],'paused_resources');self.assertFalse(result['service_admission_open'])
        self.assertEqual(self.wire.calls,0);self.f.f.runner.assert_not_called()

    def test_saved_partial_visual_observation_is_reused_without_another_image_read(self):
        self.configure(pdf=True,reading=True)
        with patch.object(VisualReader,'read',side_effect=AssertionError('no second image reading')):
            result=self.run.prepared_to_preview()
        row=self.run.pipeline.publisher._row(result['publication_id'])
        tree=self.run.pipeline.publisher._tree(row,result['publication_id']+'.candidate')
        curation=json.loads((tree/'curation.json').read_bytes())
        self.assertEqual(curation['visual_observations'],self.policy['execution_result']['visual_observations'])
        figures=json.loads((tree/'metadata.json').read_bytes())['source_record']['source_reading']['figures']
        self.assertEqual(figures['status'],'partial')
        self.assertIn('离线合成识读',figures['coverage_note'])
        self.assertEqual(self.wire.calls,1)

    def test_cli_prepared_operation_uses_the_same_frozen_image_plan_and_cannot_restart(self):
        self.configure()
        self.run.close();self.session.close()
        root=self.f.f.root
        executable=root/'synthetic-executable';executable.write_bytes(b'fixture only');executable.chmod(0o700)
        self.f.f.frozen[str(executable)]=file_sha(executable)
        manifest_path=root/'source-manifest.synthetic.json'
        manifest=dict(worktree=str(self.runtime.worktree),source_commit='synthetic-commit',
            source_snapshot=dict(files={},sha256='synthetic-snapshot'),approved_files=dict(self.f.f.frozen))
        manifest_path.write_text(encoded(manifest))
        approval=dict(self.session.approval,source_manifest=str(manifest_path),source_manifest_sha256=file_sha(manifest_path),
            run_plan=str(self.run_path),guide_plan=str(self.path),runtime_config=dict(self.runtime.config,
                runtime_relative=str(self.runtime.root.relative_to(self.runtime.worktree))))
        for key in ('cli_guard','text_node','text_script','codex_binary'):approval[key]=str(executable)
        approval_path=root/'approval.local.json';approval_path.write_text(encoded(approval));digest=file_sha(approval_path)
        journal=root/'journal.local.jsonl'
        self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=approval))+'\n')
        process=Mock(side_effect=self.f.f.runner)
        with patch.object(pilot_execute,'checked_source'),patch.object(pilot_execute,'CliProcess',return_value=process),patch.object(
                pilot_execute,'CodexTextWire',return_value=self.wire),patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(root,digest,'prepare')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
            self.assertEqual(result['result']['state'],'awaiting_owner_decision')
            count=process.call_count
            with self.assertRaises(FileExistsError):pilot_execute.execute(root,digest,'prepare')
            self.assertEqual(process.call_count,count)
        self.assertEqual(self.wire.calls,1)
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
