"""New file receipt -> V1 lookup -> conversion -> one exact synthetic guide."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded,sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.pilot_inbox_guide import PilotInboxGuide
from kb_custody_v21.pilot_pipeline import PilotPipeline
from pilot_pipeline_fixture import ready
from test_pilot_text import Wire
import test_pilot_inbox as inbox_fixture
import test_pilot_existing as existing_fixture


class InboxGuideTests(unittest.TestCase):
    def setUp(self):
        self.i=inbox_fixture.PilotInboxTests();self.i.setUp();self.addCleanup(self.i.doCleanups)
        self.e=existing_fixture.ExistingCatalogTests();self.e.setUp();self.addCleanup(self.e.doCleanups)
        self.f=self.i.f;self.intake=self.f.flow;self.registry=self.f.registry
        self.f.approval['inbox']['max_files']=1;self.f.approval['limits']['download']=1
        font=Path(os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc')).resolve()
        self.f.frozen[str(font)]=file_sha(font)
        engine=ImagePolicies(self.registry)
        policy=engine.execute(engine.decide([],dict(material_type='unknown',reliable=False,basis_refs=[])),[],
                              self.intake.runtime.root/'conversion-work/new-text-no-images')
        self.plan=dict(reference='P21-new-material-guide-v1',legacy_catalog=self.e.catalog,
            formats=['.md','.txt'],max_body_bytes=4096,max_characters=4096,
            metadata_policy='filename-provisional-v1',policy=policy,font_path=str(font),
            account_sha256=sha(b'synthetic-account@example.invalid'),wait_seconds=600,
            minimum_remaining_percent=0)
        self.path=self.f.root/'guide-profile.local.json';self.save()
        self.wire=Wire();self.pipeline=PilotPipeline(self.intake,resources=ready(self.registry))

    def save(self):
        self.path.write_text(encoded(self.plan));self.f.frozen[str(self.path)]=file_sha(self.path)
        self.f.approval['new_material_guide']=dict(reference='P21-guide-delegation-v1',
            plan_sha256=file_sha(self.path),max_business_turns=1)
        self.i.save_approval()

    def accept(self,*,convert=True):
        self.session=self.i.ready()
        result=self.i.receive(self.session);eid=result['original']['entry_id']
        if convert:self.assertEqual(self.pipeline.convert_once(entry_id=eid)['processing_state'],'ready_candidate')
        self.guide=PilotInboxGuide(self.session,self.path,wire=self.wire)
        return eid

    def children(self):return list((self.intake.runtime.root/'conversion-work').glob('pilot-text-*'))

    def test_unknown_future_body_is_bound_after_custody_then_one_candidate_reused(self):
        self.assertNotIn('body_sha256',self.plan)
        eid=self.accept();result=self.guide(self.pipeline,eid)
        again=self.guide(self.pipeline,eid)
        self.assertEqual(result['publication_id'],again['publication_id']);self.assertEqual(self.wire.calls,1)
        child=json.loads((self.children()[0]/'approval.local.json').read_bytes())
        derived=json.loads((self.children()[0]/'derivation.local.json').read_bytes())
        self.assertEqual(derived['original_sha256'],sha(inbox_fixture.CONTENT))
        self.assertEqual(child['request_sha256'],[derived['request_sha256']])
        self.assertEqual(child['max_business_turns'],1);self.assertEqual(child['minimum_remaining_percent'],0)
        self.assertEqual(child['parent_approval_sha256'],self.session.approval_sha)
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertFalse(self.registry.rows('SELECT * FROM notification_attempts'))

    def test_existing_v1_bytes_renamed_block_before_child_and_model(self):
        eid=self.accept();self.e.add_plan('renamed.md',sha(inbox_fixture.CONTENT))
        with self.assertRaisesRegex(ContractError,'existing V1'):self.guide(self.pipeline,eid)
        self.assertEqual(self.children(),[]);self.assertEqual(self.wire.calls,0)

    def test_same_filename_changed_bytes_also_requires_review(self):
        self.e.add_plan('synthetic.md','b'*64);eid=self.accept()
        with self.assertRaisesRegex(ContractError,'existing V1'):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,0)

    def test_catalog_match_appearing_during_child_setup_prevents_model(self):
        eid=self.accept();original=self.guide._material;calls=0
        def changed(*a,**k):
            nonlocal calls
            calls+=1
            if calls==3:self.e.add_plan('synthetic.md',sha(inbox_fixture.CONTENT))
            return original(*a,**k)
        with patch.object(self.guide,'_material',side_effect=changed):
            with self.assertRaisesRegex(ContractError,'existing V1'):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,0)

    def test_missing_delegation_does_not_self_authorize(self):
        self.f.approval.pop('new_material_guide');self.i.save_approval();s=self.i.make()
        with self.assertRaises(ContractError):PilotInboxGuide(s,self.path,wire=self.wire)
        self.assertEqual(self.wire.calls,0)

    def test_multiple_material_parent_rejected(self):
        self.f.approval['inbox']['max_files']=2;self.f.approval['limits']['download']=2;self.i.save_approval()
        s=self.i.make()
        with self.assertRaises(ContractError):PilotInboxGuide(s,self.path,wire=self.wire)
        self.assertEqual(self.wire.calls,0)

    def test_conversion_missing_or_technical_review_blocks_child(self):
        eid=self.accept(convert=False)
        with self.assertRaises(ContractError):self.guide(self.pipeline,eid)
        self.pipeline.convert_once(entry_id=eid)
        original=self.intake.conversion.service.ledger.get_task
        with patch.object(self.intake.conversion.service.ledger,'get_task',
                          side_effect=lambda task:dict(original(task),status='needs_review')):
            with self.assertRaisesRegex(ContractError,'conversion review'):self.guide(self.pipeline,eid)
        self.assertEqual(self.children(),[]);self.assertEqual(self.wire.calls,0)

    def test_full_body_cannot_be_silently_truncated_to_byte_limit(self):
        self.plan['max_body_bytes']=4;self.save();eid=self.accept()
        with self.assertRaisesRegex(ContractError,'no silent truncation'):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,0)

    def test_original_and_converted_bytes_are_rechecked(self):
        eid=self.accept();row=self.registry.entry(eid)
        (self.intake.runtime.root/row['relative_path']).write_bytes(b'changed')
        with self.assertRaises(ContractError):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,0)

    def test_registered_images_cannot_be_omitted(self):
        eid=self.accept()
        with patch.object(self.intake.conversion,'registered_images',return_value=[
                dict(asset_id='asset_synthetic',role='extracted_original')]):
            with self.assertRaisesRegex(ContractError,'image inventory'):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,0)

    def test_original_from_another_parent_cannot_acquire_new_budget(self):
        eid=self.accept()
        original=self.registry.rows
        def other_parent(sql,params=()):
            rows=original(sql,params)
            if params==('pilot-inbox-custody:'+eid,):
                row=dict(rows[0]);payload=json.loads(row['payload_json'])
                payload['parent_approval_sha256']='a'*64
                return [dict(row,payload_json=encoded(payload))]
            return rows
        with patch.object(self.registry,'rows',side_effect=other_parent):
            with self.assertRaisesRegex(ContractError,'only this parent'):self.guide(self.pipeline,eid)
        self.assertEqual(self.children(),[]);self.assertEqual(self.wire.calls,0)

    def test_prior_source_slot_cannot_be_reset_by_another_window(self):
        eid=self.accept()
        with self.registry.transaction() as db:
            self.registry.event(db,'pilot_inbox_guide_reserved',eid,
                'pilot-inbox-guide-source:'+sha(inbox_fixture.CONTENT),dict(parent_approval_sha256='a'*64))
        with self.assertRaisesRegex(ContractError,'slot already reserved'):self.guide(self.pipeline,eid)
        self.assertEqual(self.children(),[]);self.assertEqual(self.wire.calls,0)

    def test_unknown_model_receipt_does_not_resubmit(self):
        eid=self.accept();self.wire.mutate=lambda _: (_ for _ in ()).throw(OSError('synthetic timeout'))
        with self.assertRaises(ContractError):self.guide(self.pipeline,eid)
        calls=self.wire.calls
        with self.assertRaises(ContractError):self.guide(self.pipeline,eid)
        self.assertEqual(calls,1);self.assertEqual(self.wire.calls,1)

    def test_profile_or_saved_child_mutation_prevents_reuse(self):
        eid=self.accept();self.guide(self.pipeline,eid)
        (self.children()[0]/'derivation.local.json').write_text('{}')
        with self.assertRaises(ContractError):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,1)

    def test_filename_is_provisional_metadata_with_unknown_authors(self):
        eid=self.accept();record=self.guide._material(self.pipeline,eid)[4]
        self.assertEqual(record['work']['title'],'synthetic.md')
        self.assertEqual(record['canonical_status'],'provisional');self.assertEqual(record['work']['authors'],[])
        self.assertIsNone(record['work']['publication_year'])
        self.assertIn('文件名',record['evidence_scope']['explicit_constraints'][0]['description'])

    def test_full_body_character_limit_is_not_silently_truncated(self):
        self.plan['max_characters']=1;self.save();eid=self.accept()
        with self.assertRaisesRegex(ContractError,'character scope'):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,0)

    def test_in_memory_profile_cannot_expand_delegated_scope(self):
        eid=self.accept();self.guide.profile['max_body_bytes']+=1
        with self.assertRaisesRegex(ContractError,'explicitly delegate'):self.guide(self.pipeline,eid)
        self.assertEqual(self.children(),[]);self.assertEqual(self.wire.calls,0)

    def test_converted_mapping_cannot_point_to_another_original(self):
        eid=self.accept();original=self.intake.conversion.publication_extraction
        def changed(*args,**kwargs):
            row,mapping,root=original(*args,**kwargs)
            return row,dict(mapping,source_sha256='a'*64),root
        with patch.object(self.intake.conversion,'publication_extraction',side_effect=changed):
            with self.assertRaisesRegex(ContractError,'belongs to'):self.guide(self.pipeline,eid)
        self.assertEqual(self.wire.calls,0)

    def test_received_pdf_stays_outside_the_automatic_text_guide_route(self):
        self.f.message['body']['content']=encoded(dict(file_key='file_synthetic',file_name='synthetic.pdf'))
        eid=self.accept(convert=False)
        with self.assertRaisesRegex(ContractError,'accepted text original'):self.guide(self.pipeline,eid)
        self.assertEqual(self.children(),[]);self.assertEqual(self.wire.calls,0)
