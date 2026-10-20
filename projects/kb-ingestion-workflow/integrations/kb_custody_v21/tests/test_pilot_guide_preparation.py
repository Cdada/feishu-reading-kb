"""Parent-to-request binding uses the existing platform/model fixtures."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from kb_custody_v21.content import empty_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.pilot_guide_preparation import PilotGuidePreparation
from kb_custody_v21.pilot_run import PilotBoundedRun
from pilot_pipeline_fixture import ready
from test_pilot_delivery import delivery_response
from test_pilot_download import CONTENT, download_response
from test_pilot_text import Wire
import test_pilot_availability as availability
import test_pilot_notices as notices


class GuidePreparationTests(unittest.TestCase):
    def setUp(self):
        self.f=notices.NoticeTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.a=availability.AvailabilityTests(); self.a.setUp(); self.addCleanup(self.a.doCleanups)
        self.intake,self.registry=self.a.intake,self.a.registry
        self.sample=self.f.f.root/'synthetic.md'; self.sample.write_bytes(CONTENT)
        self.f.f.frozen[str(self.sample)]=file_sha(self.sample)
        self.run_path=self.f.f.root/'run.synthetic.json'
        self.run_path.write_text(encoded(dict(reference='P11-single-material-v1',sample_path=str(self.sample),
            sample_sha256=file_sha(self.sample),max_pages=1,lookback_seconds=3600)))
        self.f.f.frozen[str(self.run_path)]=file_sha(self.run_path)
        font=Path(os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc')).resolve()
        self.f.f.frozen[str(font)]=file_sha(font)
        engine=ImagePolicies(self.registry)
        policy=engine.execute(engine.decide([],dict(material_type='unknown',reliable=False,basis_refs=[])),[],
            self.intake.runtime.root/'conversion-work/approved-no-images')
        self.plan=dict(reference='P11-guide-derivation-v1',run_plan_path=str(self.run_path),
            run_plan_sha256=file_sha(self.run_path),body_sha256=sha(CONTENT),
            record=empty_record('合成收件','synthetic-title'),ranges=[[0,len(CONTENT.decode())]],
            reader_context=None,curation_data={},policy=policy,font_path=str(font),
            account_sha256=sha(b'synthetic-account@example.invalid'),wait_seconds=600)
        self.path=self.f.f.root/'guide.synthetic.json'; self.path.write_text(encoded(self.plan))
        self.f.f.frozen[str(self.path)]=file_sha(self.path)
        self.f.f.approval.update(limits=dict(self.f.f.approval['limits'],auth=20),maximum_total=60,
            guide_plan_sha256=file_sha(self.path))
        self.f.save_approval()
        original=self.f.f.runner.side_effect
        def transport(c):
            if c.get('preview'):return delivery_response(c)
            if c['kind']=='download':return download_response(c,CONTENT)
            if c['kind']=='send':self.f.item['payload']=json.loads(json.loads(c['body'])['content'])['text']
            return original(c)
        self.f.f.runner.side_effect=transport
        self.session=self.f.make(False); self.wire=Wire()
        self.guide=PilotGuidePreparation(self.session,self.path,wire=self.wire)
        self.run=PilotBoundedRun(self.session,self.intake,self.run_path,prepare_candidate=self.guide,
            checked_ready=lambda:True,resources=ready(self.registry))
        self.addCleanup(self.run.close)

    def accept(self):
        self.session.verify_isolation(); sid=self.run._scan()
        accepted=self.session.receive_file(sid,'om_synthetic',self.intake,sample_path=self.sample)
        eid=accepted['receipt']['entry_id']; row=self.registry.entry(eid)
        self.session.bind(self.run.prefix+'original',dict(entry_id=eid,
            path=str(self.intake.runtime.root/row['relative_path']),sha256=row['sha256'],size_bytes=row['size_bytes']))
        self.assertEqual(self.run.pipeline.convert_once(entry_id=eid)['processing_state'],'ready_candidate')
        return eid

    def children(self):return list((self.intake.runtime.root/'conversion-work').glob('pilot-text-*'))

    def test_full_chain_uses_one_parent_bound_request_and_waits_for_owner(self):
        result=self.run.run_to_preview()
        self.assertEqual(result['state'],'awaiting_owner_decision')
        self.assertEqual(self.wire.calls,1)
        root=self.children()[0]
        child=json.loads((root/'approval.local.json').read_bytes())
        derivation=json.loads((root/'derivation.local.json').read_bytes())
        self.assertEqual(child['parent_approval_sha256'],self.session.approval_sha)
        self.assertEqual(child['expires_at'],self.session.approval['expires_at'])
        self.assertEqual(child['max_business_turns'],1)
        self.assertEqual(child['request_sha256'],[derivation['request_sha256']])
        self.assertEqual(derivation['entry_id'],result['entry_id'])
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_frozen_plan_without_explicit_parent_delegation_cannot_authorize_itself(self):
        self.session.approval.pop('guide_plan_sha256')
        with self.assertRaisesRegex(ContractError,'explicitly authorize'):
            PilotGuidePreparation(self.session,self.path,wire=self.wire)
        self.assertEqual(self.wire.calls,0)
        self.assertEqual(self.children(),[])

    def test_unselected_entry_cannot_derive_a_second_request(self):
        eid=self.accept()
        with patch.object(self.session,'value',return_value=None):
            with self.assertRaises(ContractError): self.guide(self.run.pipeline,eid)
        self.assertEqual(self.children(),[])
        self.assertEqual(self.wire.calls,0)

    def test_unapproved_converted_body_stops_before_child_approval_or_model(self):
        eid=self.accept()
        original=self.intake.conversion.publication_extraction
        def changed(*args,**kwargs):
            row,mapping,root=original(*args,**kwargs)
            return row,dict(mapping,output_sha256='a'*64),root
        with patch.object(self.intake.conversion,'publication_extraction',side_effect=changed):
            with self.assertRaisesRegex(ContractError,'body differs'):self.guide(self.run.pipeline,eid)
        self.assertEqual(self.children(),[])
        self.assertEqual(self.wire.calls,0)

    def test_in_memory_template_mutation_cannot_expand_frozen_scope(self):
        self.guide.plan['record']['work']['title']='changed metadata'
        with self.assertRaises(ContractError):self.guide.check()
        self.assertEqual(self.wire.calls,0)
        self.assertEqual(self.children(),[])

    def test_actual_images_cannot_be_omitted_to_use_text_only_plan(self):
        eid=self.accept()
        with patch.object(self.intake.conversion,'registered_images',return_value=[
                dict(asset_id='synthetic-image',role='extracted_original')]):
            with self.assertRaisesRegex(ContractError,'image inventory'):self.guide(self.run.pipeline,eid)
        self.assertEqual(self.wire.calls,0)
        self.assertEqual(self.children(),[])

    def test_unknown_model_attempt_is_reused_as_blocked_not_reissued(self):
        eid=self.accept()
        self.wire.mutate=lambda _: (_ for _ in ()).throw(OSError('synthetic timeout'))
        with self.assertRaisesRegex(ContractError,'unknown'):self.guide(self.run.pipeline,eid)
        with self.assertRaises(ContractError):self.guide(self.run.pipeline,eid)
        self.assertEqual(self.wire.calls,1)
        self.assertEqual(len(self.children()),1)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_success_reuses_same_child_and_candidate_without_second_business_turn(self):
        eid=self.accept()
        first=self.guide(self.run.pipeline,eid)
        second=self.guide(self.run.pipeline,eid)
        self.assertEqual(first,second)
        self.assertEqual(self.wire.calls,1)
        self.assertEqual(len(self.children()),1)

    def test_changed_child_provenance_is_rejected_without_overwrite_or_send(self):
        eid=self.accept()
        self.wire.mutate=lambda _:dict(status='blocked_before_turn')
        with self.assertRaises(ContractError):self.guide(self.run.pipeline,eid)
        path=self.children()[0]/'derivation.local.json'; path.write_text('{}')
        with self.assertRaisesRegex(ContractError,'saved child derivation'):self.guide(self.run.pipeline,eid)
        self.assertEqual(path.read_text(),'{}')
        self.assertEqual(self.wire.calls,1)

    def test_plan_changed_during_model_wait_stops_before_preview(self):
        eid=self.accept()
        def changed(value):
            self.path.write_text('{}')
            return value
        self.wire.mutate=changed
        with self.assertRaises(ContractError):self.guide(self.run.pipeline,eid)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))
        self.assertEqual(self.wire.calls,1)


    def test_explicit_zero_reserve_reaches_child_approval(self):
        self.run.close();self.session.close()
        self.plan['minimum_remaining_percent']=0
        self.path.write_text(encoded(self.plan))
        platform=self.f.f
        platform.frozen[str(self.path)]=file_sha(self.path)
        platform.approval['guide_plan_sha256']=file_sha(self.path)
        self.f.save_approval()
        journal=platform.root/'journal.local.jsonl'
        self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=platform.approval))+'\n')
        self.session=self.f.make(False)
        self.guide=PilotGuidePreparation(self.session,self.path,wire=self.wire)
        self.run=PilotBoundedRun(self.session,self.intake,self.run_path,prepare_candidate=self.guide,
            checked_ready=lambda:True,resources=ready(self.registry))
        self.addCleanup(self.run.close)
        entry=self.accept();self.guide(self.run.pipeline,entry)
        child=json.loads((self.children()[0]/'approval.local.json').read_bytes())
        self.assertEqual(child['minimum_remaining_percent'],0)

    def test_invalid_reserve_is_rejected_before_model(self):
        for value in (-1,100,1.5,True,'0',None):
            with self.subTest(value=value):
                self.path.write_text(encoded(dict(self.plan,minimum_remaining_percent=value)))
                with self.assertRaisesRegex(ContractError,'reserve'):
                    PilotGuidePreparation(self.session,self.path,wire=self.wire)
        self.assertEqual(self.wire.calls,0)
