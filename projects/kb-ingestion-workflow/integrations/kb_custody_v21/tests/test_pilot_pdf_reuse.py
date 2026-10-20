"""A real local conversion plus synthetic saved image evidence, without network."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.material_review import MaterialReviews
from kb_custody_v21.pdf_assets import PdfAssetSupplement
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.pilot_pdf_reuse import PdfEvidenceReuse
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.pilot_images import checked_image_facts
from kb_custody_v21.registry import encoded
import test_pdf_assets as fixture
from test_material_review import report_for


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PdfSupplementTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.registry,self.runtime,self.conversion=self.f.registry,self.f.runtime,self.f.conversion
        self.old_entry,self.old_task,mapping=self.f.converted()
        self.old=PdfAssetSupplement(self.registry).prepare(self.old_entry,mapping['extraction_id'])['mapping']
        self.reviews=MaterialReviews(self.registry)
        self.review=self.reviews.record(self.old_entry,self.old['extraction_id'],report_for(self.runtime,self.old,limitation=True))
        self.old_images=self.conversion.registered_images(self.old['extraction_id'])
        image=self.old_images[0]
        self.observations=[dict(observation_id='observation_'+'a'*32,image_asset_id=image['asset_id'],
            cloud_original_asset_id=image['asset_id'],source_locator=dict(extraction_id=self.old['extraction_id'],source_sha256=self.old['source_sha256']),
            original_sha256=image['sha256'],reader='synthetic-saved-reader',read_at='2026-01-01T00:00:00+00:00',
            consent_event_id='synthetic-expired-old-consent',validation_mode='offline_mock',reading_status='partially_read',
            transcribed_text='合成内容',interpretation='已有解释',unresolved=['小字未读'],read_asset_id=image['asset_id'])]
        self.intake=PilotIntake(self.registry,PilotScope('cli_synthetic','oc_synthetic','ou_synthetic'),max_attachments=1,max_pending=8)
        self.selection=dict(action='convert_as_new_version',sha256=self.old['source_sha256'],prior_task_ids=[self.old_task['task_id']],
            qualified_prior={self.old_task['task_id']:dict(extraction_id=self.old['extraction_id'],review_id=self.review['review_id'])})
        raw=(self.runtime.root/self.registry.entry(self.old_entry)['relative_path']).read_bytes()
        message=dict(appId='cli_synthetic',chatId='oc_synthetic',senderId='ou_synthetic',chatType='group',messageId='om_new_synthetic',
            resources=[dict(type='file',fileKey='file_synthetic',fileName='synthetic-illustrated.pdf',sizeBytes=len(raw))])
        self.entry=self.intake.receive(message,0,MockAttachment(raw).download)['entry_id']

    def target(self):
        value=self.intake.work_once(entry_id=self.entry,duplicate_selection=self.selection)
        self.task=self.conversion.service.ledger.get_task(value['task_id'])
        extraction=json.loads(self.task['quality_json'])['extraction']['extraction_id']
        self.new=PdfAssetSupplement(self.registry).prepare(self.entry,extraction)['mapping']
        return self.adapter()

    def adapter(self,observations=None):
        return PdfEvidenceReuse(self.registry,prior_extraction_id=self.old['extraction_id'],prior_review_id=self.review['review_id'],
            target_entry_id=self.entry,target_extraction_id=self.new['extraction_id'],observations=self.observations if observations is None else observations)

    def test_explicit_qualified_prior_preserves_new_intake_and_old_task(self):
        adapter=self.target()
        self.assertNotEqual(self.task['task_id'],self.old_task['task_id'])
        self.assertEqual(self.old_task,self.conversion.service.ledger.get_task(self.old_task['task_id']))
        self.assertEqual(self.task['status'],'needs_review')
        with self.assertRaisesRegex(ContractError,'not a Pilot job'):self.intake._job(self.old_entry)
        saved=adapter.record_review(self.runtime.root/'conversion-work/reuse-proof.json')
        self.assertEqual(saved['qualification'],'qualified');self.assertEqual(saved['owner_choice'],'pending')
        self.assertEqual(self.task,self.conversion.service.ledger.get_task(self.task['task_id']))
        self.assertEqual(self.conversion.publication_extraction(self.new['extraction_id'],expected_task_id=self.task['task_id'])[1],self.new)
        self.assertEqual(MaterialReviews.limitations(saved),MaterialReviews.limitations(self.review))
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_saved_reading_reaches_policy_without_new_reader_or_full_coverage_claim(self):
        adapter=self.target();adapter.record_review(self.runtime.root/'conversion-work/reuse-proof.json')
        images=adapter.checked()['images'];selected=[adapter.initial['image_id_map'][self.observations[0]['image_asset_id']]]
        engine=ImagePolicies(self.registry)
        decision=engine.decide(images,dict(material_type='unknown',reliable=True,basis_refs=['synthetic']),
            current=dict(strategy=4,confirmation_ref='synthetic-reuse-only'),selected=dict(reading=selected),
            dense_choices={i['asset_id']:'local_original' for i in images},consent_assets=selected,capabilities=dict(archive='configured_pending_window',preview=True,reading=True))
        with patch('kb_custody_v21.visual_reading.VisualReader.read',side_effect=AssertionError('no new model reader')):
            policy=engine.execute(decision,images,self.runtime.root/'conversion-work/reused-policy',reader=adapter)
        facts=checked_image_facts(self.conversion,self.new['extraction_id'],images,policy)
        self.assertEqual(facts['figures']['status'],'partial')
        saved=facts['observations'][0];old=self.observations[0]
        for key in ('read_at','reader','consent_event_id','reading_status','transcribed_text','interpretation','unresolved','read_asset_id'):
            self.assertEqual(saved[key],old[key])
        self.assertFalse(saved['reused_from']['new_model_call'])
        other=next(i for i in images if i['asset_id'] not in selected)
        with self.assertRaisesRegex(ContractError,'no saved observation'):adapter.read(other)

    def test_needs_review_without_explicit_qualified_binding_is_rejected(self):
        self.selection.pop('qualified_prior')
        with self.assertRaisesRegex(ContractError,'healthy and terminal'):
            self.intake.work_once(entry_id=self.entry,duplicate_selection=self.selection)
        self.assertIsNone(self.intake._job(self.entry)['task_id'])

    def test_changed_prior_review_id_is_rejected_before_conversion(self):
        self.selection['qualified_prior'][self.old_task['task_id']]['review_id']='review_'+'b'*32
        with self.assertRaisesRegex(ContractError,'differs from selection'):
            self.intake.work_once(entry_id=self.entry,duplicate_selection=self.selection)
        self.assertIsNone(self.intake._job(self.entry)['task_id'])

    def test_original_or_image_damage_blocks_reuse(self):
        adapter=self.target();image=adapter.initial['images'][0];Path(image['path']).write_bytes(b'changed')
        with self.assertRaises(ContractError):adapter.record_review(self.runtime.root/'conversion-work/reuse-proof.json')
        self.assertIsNone(self.reviews.latest(self.new['extraction_id']))

    def test_prior_review_evidence_damage_cannot_be_inherited(self):
        adapter=self.target();(self.runtime.root/self.review['report']['evidence'][0]['relative_path']).write_text('changed')
        with self.assertRaisesRegex(ContractError,'evidence changed'):adapter.checked()

    def test_foreign_unfinished_duplicate_or_reused_observation_is_rejected(self):
        self.target()
        cases=[dict(self.observations[0],cloud_original_asset_id='asset_'+'e'*32),dict(self.observations[0],reading_status='failed'),
            dict(self.observations[0],reused_from={'anything':True}),
            dict(self.observations[0],source_locator=dict(self.observations[0]['source_locator'],physical_page=99))]
        for observation in cases:
            with self.subTest(observation=observation),self.assertRaises(ContractError):self.adapter([observation])
        with self.assertRaisesRegex(ContractError,'duplicate'):self.adapter(self.observations*2)

    def test_parser_or_page_mapping_difference_is_not_same_evidence(self):
        self.target()
        adapter=self.adapter();real=adapter.conversion.extraction
        def changed(eid):
            row,mapping,root=real(eid)
            if eid==self.new['extraction_id']:mapping=dict(mapping,extractor_version='different')
            return row,mapping,root
        with patch.object(adapter.conversion,'extraction',side_effect=changed),self.assertRaisesRegex(ContractError,'parser'):
            adapter.checked()

if __name__=='__main__':unittest.main()
