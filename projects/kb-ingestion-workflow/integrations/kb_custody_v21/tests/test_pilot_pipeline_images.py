import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader

from kb_custody_v21.content import ReadingGuideAdapter
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.material_review import MaterialReviews
from kb_custody_v21.pdf_assets import PdfAssetSupplement
from kb_custody_v21.pilot_images import checked_image_facts
from kb_custody_v21.pilot_text import PilotTextSession
from kb_custody_v21.registry import encoded
from kb_custody_v21.visual_reading import VisualReader
import test_pilot_pipeline as pipeline_fixture
import test_pilot_intake as intake_fixture
import test_pdf_assets as pdf_fixture
from test_material_review import report_for


class PipelineImageTests(unittest.TestCase):
    def setUp(self):
        markdown = '# 合成图文材料\n\n第一段城市研究。\n'
        for color in ('navy', 'red'):
            out = BytesIO(); Image.new('RGB', (50,40), color).save(out, 'PNG')
            markdown += '\n![合成图](data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()+')\n'
        self.fixture = pipeline_fixture.PipelineTests()
        with patch.object(intake_fixture, 'CONTENT', markdown.encode()):
            self.fixture.setUp()
        f = self.fixture
        self.registry, self.flow = f.registry, f.flow
        row, _, root = f.intake.conversion.extraction(f.mapping['extraction_id'])
        self.original_mapping = f.mapping
        self.images = [dict(i, path=str(root/i['relative_path']), visual_type='dense', basis_refs=['synthetic'],
            extraction_id=f.mapping['extraction_id']) for i in json.loads(row['inventory_json'])]
        self.assertEqual(len(self.images), 2)

    def configure(self, strategy=4, *, partial=True):
        f = self.fixture
        class Provider:
            is_mock=True; model='synthetic-image-reader'
            def read(self, *args, **kwargs):
                return dict(transcribed_text='合成图形', interpretation='合成图示说明。',
                    reading_status='partially_read' if partial else 'read',
                    unresolved=['小字无法辨认'] if partial else [])
        consents = {i['asset_id']:dict(event_id='synthetic-image-consent', asset_id=i['asset_id'],
            sha256=i['sha256'], model=Provider.model,
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()) for i in self.images}
        engine = ImagePolicies(self.registry)
        plan = engine.decide(self.images, dict(material_type='unknown', reliable=True, basis_refs=['synthetic']),
            current=dict(strategy=strategy, confirmation_ref='synthetic-image-choice'),
            selected=dict(reading=[self.images[0]['asset_id']]),
            dense_choices={i['asset_id']:'text_only' for i in self.images} if strategy!=1 else {},
            consent_assets=list(consents), capabilities=dict(archive='configured_pending_window', preview=True, reading=True))
        self.policy = engine.execute(plan, self.images, f.runtime.root/'conversion-work/managed-images',
            reader=VisualReader(f.runtime, Provider(), consents))
        outputs = self.policy['execution_result']['per_asset_results']
        replacements = {}
        for i in self.images:
            output = next((o for o in outputs if i['asset_id'] in o['derived_from']), None)
            replacements[i['original_target']] = dict(asset_id=i['asset_id'],
                local_path='images/'+Path(output['path']).name if output else None,
                representation_asset_id=output['asset_id'] if output else None)
        if getattr(self, 'pdf_supplement', False):
            body, mapping = f.body, f.mapping
        else:
            body, mapping = f.intake.conversion.reexpress(f.mapping['extraction_id'], replacements)
        self.assertNotEqual(mapping['extraction_id'], self.original_mapping['extraction_id'])
        f.body, f.mapping = body, mapping
        args = f.data['prepare']
        args.update(extraction_id=mapping['extraction_id'], images=self.images, policy=self.policy)
        args['consent'].update(asset_id=mapping['output_asset_id'], sha256=sha(body), extraction_id=mapping['extraction_id'],
                               ranges=[[0,len(body.decode())]])
        request = ReadingGuideAdapter(PilotTextSession).prepare_request(args['record'], body, mapping, args['consent'])
        approval_path = f.text_root/'approval.local.json'
        approval = json.loads(approval_path.read_bytes()); approval['request_sha256']=[sha(encoded(request).encode())]
        approval_path.write_text(encoded(approval)); f.data['text_approval_sha']=sha(approval_path.read_bytes())
        _, _, root = f.intake.conversion.extraction(mapping['extraction_id'])
        f.data['source']={str(root/'source.md'):sha(body)}; f.save()

    def test_expression_keeps_partial_image_facts_and_does_not_promote_unread_figure(self):
        self.configure()
        f = self.fixture; result = f.run_pipeline(); tree = Path(result['preview_path']).parents[1]
        curation = json.loads((tree/'curation.json').read_bytes())
        self.assertEqual(curation['visual_observations'], self.policy['execution_result']['visual_observations'])
        self.assertEqual(curation['extraction_id'], f.mapping['extraction_id'])
        metadata = json.loads((tree/'metadata.json').read_bytes())
        figures = metadata['source_record']['source_reading']['figures']
        self.assertEqual(figures['status'], 'partial')
        self.assertEqual(figures['asset_ids'], [self.images[0]['asset_id']])
        self.assertEqual(figures['extraction_id'], self.original_mapping['extraction_id'])
        pdf = '\n'.join(p.extract_text() for p in PdfReader(result['preview_path']).pages)
        for text in ('正文导读的阅读范围', '部分可读', '小字无法辨认', '离线合成识读', '未识读 1 幅'):
            self.assertIn(text, pdf)
        self.assertNotIn('observation_', pdf)
        self.assertEqual(f.run_pipeline()['publication_id'], result['publication_id'])
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_local_original_policy_preserves_actual_image_bytes_in_expression_package(self):
        self.configure(strategy=1)
        result = self.fixture.run_pipeline(); tree = Path(result['preview_path']).parents[1]
        for image in self.images:
            kept = next(o for o in self.policy['execution_result']['per_asset_results'] if o['original_asset_id']==image['asset_id'])
            self.assertEqual((tree/'提取/images'/Path(kept['path']).name).read_bytes(), Path(image['path']).read_bytes())
        figures = json.loads((tree/'metadata.json').read_bytes())['source_record']['source_reading']['figures']
        self.assertEqual(figures['status'], 'not_read')

    def test_foreign_image_or_observation_stops_before_body_request(self):
        self.configure()
        f = self.fixture; good = deepcopy(f.data)
        for change in ('path', 'observation', 'missing', 'duplicate', 'sha', 'unfinished'):
            with self.subTest(change=change):
                f.data = deepcopy(good); args = f.data['prepare']; observations=args['policy']['execution_result']['visual_observations']
                if change=='path': args['images'][0]['path']=args['images'][1]['path']
                if change=='observation': observations[0]['source_locator']['extraction_id']='extraction_'+'a'*32
                if change=='missing': observations.clear()
                if change=='duplicate': observations.append(deepcopy(observations[0]))
                if change=='sha': observations[0]['original_sha256']='0'*64
                if change=='unfinished': args['policy']['execution_result']['status']='planned'
                f.save()
                with self.assertRaises(ContractError): f.run_pipeline()
                self.assertFalse(Path(f.data['marker']).exists())
                self.assertIsNone(self.flow._event(f.entry,'plan'))

    def test_image_changed_during_body_generation_is_rejected_before_preview(self):
        self.configure()
        def corrupt(point):
            if point=='after_text': Path(self.images[0]['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError, 'image bytes changed'):
            self.fixture.run_pipeline(hook=corrupt)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_expression_must_belong_to_intake_conversion_task(self):
        self.configure()
        f = self.fixture
        with patch.object(f.intake.__class__, '_job', return_value=dict(task_id='foreign-task', stop_reason=None)):
            with self.assertRaisesRegex(ContractError, 'selected conversion task'): f.run_pipeline()
        self.assertFalse(Path(f.data['marker']).exists())

    def test_existing_quality_gate_still_applies_to_expression(self):
        self.configure()
        with (patch('kb_custody_v21.material_review.MaterialReviews.checked', return_value=dict(qualification='incomplete')),
                patch('kb_custody_v21.material_review.MaterialReviews.require_qualified', side_effect=ContractError('quality blocked'))):
            with self.assertRaisesRegex(ContractError, 'quality blocked'): self.fixture.run_pipeline()
        self.assertFalse(Path(self.fixture.data['marker']).exists())

    def test_local_binding_preserves_pending_real_observation_marker_without_claiming_validation(self):
        self.configure()
        policy = deepcopy(self.policy)
        observation = policy['execution_result']['visual_observations'][0]
        observation['validation_mode']='real_model_observation_pending_quality_review'
        observation['original_sha256']=self.images[0]['sha256']
        facts = checked_image_facts(self.fixture.intake.conversion, self.fixture.mapping['extraction_id'], self.images, policy)
        self.assertEqual(facts['observations'][0], observation)
        self.assertEqual(facts['figures']['status'], 'partial')
        self.assertIn('质量仍待复核', facts['figures']['coverage_note'])
        # This is only a synthetic provenance-preservation check, not a real call.

    def test_long_synthetic_image_notes_publish_the_exact_multipage_preview(self):
        self.configure()
        f=self.fixture
        note='图注合成段落。树荫覆盖和道路条件需要结合当前材料的范围理解。'*90+'图注结束。'
        self.policy['execution_result']['visual_observations'][0]['transcribed_text']=note
        f.save();result=f.run_pipeline();pdf=Path(result['preview_path']).read_bytes()
        reader=PdfReader(BytesIO(pdf));self.assertGreater(len(reader.pages),2)
        text=''.join(''.join(p.extract_text().splitlines()[1:]) for p in reader.pages)
        self.assertIn(note,''.join(text.split()))
        self.assertIn('小字无法辨认',text)
        scope=f.intake.scope;previews=self.flow.previews
        previews.bind_delivery(result['publication_id'],app_id=scope.app_id,chat_id=scope.chat_id,message_id='synthetic-preview',
            pdf_sha256=result['preview_sha256'],receipt_ref='synthetic-receipt',outcome='sent')
        published=previews.decide(app_id=scope.app_id,chat_id=scope.chat_id,user_id=scope.user_id,
            message_id='synthetic-decision',reply_to='synthetic-preview',text='确认入库')
        final=Path(published['path'])/'发布记录/已确认入库预览.pdf'
        self.assertEqual(final.read_bytes(),pdf)
        (f.runtime.base/'test-output/pilot-image-preview-sample.local.json').write_text(encoded(dict(path=str(final),pages=len(reader.pages))))

    def pdf_material(self, *, qualify=True):
        f=self.fixture;raw,_=pdf_fixture.illustrated_pdf()
        message=intake_fixture.message('synthetic-pdf')
        message['resources'][0].update(fileName='synthetic.pdf',sizeBytes=len(raw))
        f.entry=f.intake.receive(message,0,MockAttachment(raw).download)['entry_id']
        result=self.flow.convert_once();self.assertEqual(result['processing_state'],'needs_review')
        task=f.intake.conversion.service.ledger.get_task(result['task_id'])
        self.original_mapping=json.loads(task['quality_json'])['extraction']
        supplement=PdfAssetSupplement(self.registry).prepare(f.entry,self.original_mapping['extraction_id'])
        self.pdf_supplement=True;f.mapping=supplement['mapping']
        root=Path(supplement['root']);f.body=(root/'source.md').read_bytes()
        self.images=[dict(i,path=str(root/i['relative_path']),extraction_id=f.mapping['extraction_id'],
            visual_type='dense',basis_refs=['synthetic-pdf-page']) for i in supplement['images']]
        f.data['prepare']['entry_id']=f.entry
        self.configure(strategy=1)
        if qualify:MaterialReviews(self.registry).record(f.entry,f.mapping['extraction_id'],report_for(f.runtime,f.mapping))
        return root

    def test_pdf_supplement_uses_its_own_images_and_keeps_page_aliases(self):
        self.pdf_material()
        f=self.fixture;result=f.run_pipeline();tree=Path(result['preview_path']).parents[1]
        metadata=json.loads((tree/'metadata.json').read_bytes())
        self.assertEqual(metadata['source_record']['source_reading']['figures']['extraction_id'],f.mapping['extraction_id'])
        self.assertEqual(len(self.images),2);self.assertEqual(self.images[0]['sha256'],self.images[1]['sha256'])
        aliases=self.policy['per_asset'][0]['alias_asset_ids']
        self.assertEqual(set(aliases),{i['asset_id'] for i in self.images})
        kept=next((tree/'提取/images').iterdir())
        self.assertEqual(kept.read_bytes(),Path(self.images[0]['path']).read_bytes())
        self.assertTrue((tree/'发布记录/材料复核.json').is_file())
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_pdf_supplement_remains_blocked_without_quality_review(self):
        self.pdf_material(qualify=False)
        with self.assertRaisesRegex(ContractError,'quality review'):self.fixture.run_pipeline()
        self.assertFalse(Path(self.fixture.data['marker']).exists())

    def test_inherited_markdown_images_can_receive_version_bound_material_review(self):
        self.configure();f=self.fixture
        review=MaterialReviews(self.registry).record(f.entry,f.mapping['extraction_id'],report_for(f.runtime,f.mapping))
        self.assertEqual(review['qualification'],'qualified')
        result=f.run_pipeline()
        self.assertTrue((Path(result['preview_path']).parent/'材料复核.json').is_file())

    def test_missing_owner_image_never_falls_back_to_a_new_expression_copy(self):
        self.configure();f=self.fixture;image=self.images[0]
        row,_,root=f.intake.conversion.extraction(f.mapping['extraction_id'])
        stored=next(i for i in json.loads(row['inventory_json']) if i['asset_id']==image['asset_id'])
        copy=root/stored['relative_path'];copy.parent.mkdir(parents=True,exist_ok=True)
        copy.write_bytes(Path(image['path']).read_bytes());Path(image['path']).unlink()
        with self.assertRaises(FileNotFoundError):f.run_pipeline()
        self.assertFalse(Path(f.data['marker']).exists())
