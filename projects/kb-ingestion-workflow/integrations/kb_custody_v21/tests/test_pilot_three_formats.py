"""Three-format first version: exact image retention, explicitly text-led guides."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pypdf import PdfReader
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_inbox_guide import PilotInboxGuide
from test_pilot_download import download_response
import test_pilot_inbox_guide as guide_fixture
import test_pilot_service as service_fixture
from test_markdown_bundle import bundle, png
from test_webarchive_assets import fixture as webpage


def profile(g):
    g.plan.update(reference='P27-three-format-guide-v1', formats=['.md','.zip','.webarchive'],
        policy=dict(reference='P27-preserve-originals-v1', reading='not_requested', max_images=100, max_total_bytes=10_000_000))


class ThreeFormatGuideTests(unittest.TestCase):
    def setUp(self):
        self.g = guide_fixture.InboxGuideTests(); self.g.setUp(); self.addCleanup(self.g.doCleanups)
        profile(self.g)
        self.g.f.approval['inbox']['formats'] = ['.md','.zip','.webarchive']
        self.g.save()

    def accept(self, name='article.zip', raw=None):
        g=self.g;raw=bundle() if raw is None else raw
        g.f.message['body']['content']=encoded(dict(file_key='file_synthetic',file_name=name))
        original=g.f.runner.side_effect
        def respond(control):
            return download_response(control,raw) if control['kind']=='download' else original(control)
        g.f.runner.side_effect=respond
        return g.accept()

    def test_zip_guide_retains_original_images_and_reports_unread(self):
        g=self.g;entry=self.accept()
        with patch('kb_custody_v21.pilot_vision.OfficialVisionWire.exchange', side_effect=AssertionError('no image calls')):
            result=g.guide(g.pipeline,entry)
            self.assertEqual(g.guide(g.pipeline,entry)['publication_id'],result['publication_id'])
        self.assertEqual(g.wire.calls,1)
        tree=Path(result['preview_path']).parents[1]
        metadata=json.loads((tree/'metadata.json').read_bytes())
        self.assertEqual(metadata['source_record']['source_reading']['figures']['status'],'not_read')
        curation=json.loads((tree/'curation.json').read_bytes())
        self.assertEqual(curation['visual_observations'],[])
        images=list((tree/'提取/images').glob('*.png'))
        self.assertEqual(len(images),1);self.assertEqual(images[0].read_bytes(),png())
        text=''.join(p.extract_text() for p in PdfReader(result['preview_path']).pages)
        self.assertIn('未识读 1 幅',text)
        self.assertFalse(g.registry.rows('SELECT * FROM publication_confirmations'))

    def test_webarchive_and_single_md_use_same_profile(self):
        for name,raw in [('article.webarchive',webpage()[0]),('article.md',b'# Synthetic\n\nNew standalone body.\n')]:
            with self.subTest(name=name):
                child=ThreeFormatGuideTests();child.setUp();self.addCleanup(child.doCleanups)
                entry=child.accept(name,raw)
                result=child.g.guide(child.g.pipeline,entry)
                self.assertTrue(result['preview_path']);self.assertEqual(child.g.wire.calls,1)

    def test_original_image_change_prevents_reuse_without_second_body_call(self):
        g=self.g;entry=self.accept();g.guide(g.pipeline,entry)
        value=g.guide._material(g.pipeline,entry)[1]['image_material']
        Path(value['images'][0]['path']).write_bytes(b'changed')
        with self.assertRaises(ContractError):g.guide(g.pipeline,entry)
        self.assertEqual(g.wire.calls,1)

    def test_prepared_policy_change_cannot_claim_a_reading_or_new_permission(self):
        g=self.g;entry=self.accept();g.guide._material(g.pipeline,entry)
        event=g.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='pilot_inbox_images_prepared'")[0]
        payload=json.loads(event['payload_json'])
        (g.intake.runtime.root/payload['relative_path']).write_text('{}')
        with self.assertRaisesRegex(ContractError,'preparation changed'):g.guide(g.pipeline,entry)
        self.assertEqual(g.wire.calls,0)

    def test_parent_limits_prevent_silent_image_drop(self):
        g=self.g;g.plan['policy']['max_total_bytes']=1;g.save();entry=self.accept()
        with self.assertRaisesRegex(ContractError,'retention limits'):g.guide(g.pipeline,entry)
        self.assertEqual(g.wire.calls,0)

    def test_profile_cannot_add_image_model_calls(self):
        g=self.g;g.plan['policy']['reading']='all';g.save()
        session=g.i.make()
        with self.assertRaisesRegex(ContractError,'without model reading'):
            PilotInboxGuide(session,g.path,wire=g.wire)

    def test_pdf_input_stays_outside_new_automatic_scope(self):
        g=self.g;g.f.approval['inbox']['formats'].append('.pdf');g.save()
        g.f.message['body']['content']=encoded(dict(file_key='file_synthetic',file_name='article.pdf'))
        entry=g.accept(convert=False)
        with self.assertRaisesRegex(ContractError,'accepted text original'):g.guide(g.pipeline,entry)
        self.assertEqual(g.wire.calls,0)

    def test_local_image_preparation_reuses_snapshot_after_new_guide_instance(self):
        g=self.g;entry=self.accept()
        first=g.guide._material(g.pipeline,entry)
        again=PilotInboxGuide(g.session,g.path,wire=g.wire)._material(g.pipeline,entry)
        self.assertEqual(first,again)
        self.assertEqual(len(g.registry.rows("SELECT * FROM custody_events WHERE event_type='pilot_inbox_images_prepared'")),1)
        self.assertEqual(g.wire.calls,0)


class ThreeFormatServiceTests(unittest.TestCase):
    def fixture(self):
        f=service_fixture.ServiceTests();f.setUp();self.addCleanup(f.doCleanups)
        profile(f.g)
        f.g.path.write_text(encoded(f.g.plan));f.f.frozen[str(f.g.path)]=file_sha(f.g.path)
        f.f.approval['new_material_guide']['plan_sha256']=file_sha(f.g.path)
        f.f.approval['inbox']['formats']=['.md','.zip','.webarchive']
        f.f.approval['inbox']['max_files']=3
        f.f.approval['new_material_guide']['max_business_turns']=3
        f.plan['max_scans']=4
        f.save()
        first,second=list(f.messages.values())
        first['body']['content']=encoded(dict(file_key='file_synthetic',file_name='article.zip'))
        second['body']['content']=encoded(dict(file_key='file_second',file_name='article.webarchive'))
        third=copy.deepcopy(first);third.update(message_id='om_third',body=dict(content=encoded(dict(file_key='file_third',file_name='article.md'))))
        f.messages[third['message_id']]=third
        f.binaries.update(file_synthetic=bundle(),file_second=webpage()[0],file_third=b'# Third\n\nA separate synthetic text material.\n')
        f.f.pages=[dict(items=[v],has_more=False) for v in (first,second,third)]+[dict(items=[],has_more=False)]
        return f

    def test_three_formats_continue_to_exact_confirmations_and_notifications(self):
        f=self.fixture()
        def replies():
            if len(f.deliveries)==3:f.f.pages=[dict(items=f.replies(),has_more=False)]
        f.after_wait=replies
        result=f.make().run()
        self.assertEqual(len(result['materials']),3)
        self.assertTrue(all(m['published'] for m in result['materials']),result)
        self.assertTrue(all(m['notification']['delivery_state']=='delivered' for m in result['materials']))
        self.assertEqual(f.g.wire.calls,3)
        for material in result['materials']:
            pdf=Path(material['decision']['path'])/'发布记录/已确认入库预览.pdf'
            self.assertEqual(file_sha(pdf),material['delivery']['pdf_sha256'])
        for category,count in f.counts().items():
            self.assertLessEqual(count,f.session.limits[category])

    def test_broken_zip_keeps_original_and_does_not_block_other_two_formats(self):
        f=self.fixture();f.binaries['file_synthetic']=bundle([('main.md','![missing](missing.png)')])
        result=f.make().run()
        self.assertEqual([m['state'] for m in result['materials']],['needs_review','awaiting_owner_decision','awaiting_owner_decision'])
        self.assertEqual(f.g.wire.calls,2)
        self.assertEqual(f.intake.status(result['materials'][0]['entry_id'])['original_status'],'accepted')
