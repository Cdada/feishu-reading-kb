"""One connected local chain using the existing platform and model fixtures."""
import copy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock

from kb_custody_v21.content import ReadingGuideAdapter, empty_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.pilot_run import PilotBoundedRun
from kb_custody_v21.pilot_text import MODEL, PilotTextSession
from kb_custody_v21.staging import IntegrityError
from pilot_pipeline_fixture import call, ready
from test_pilot_decision import owner_reply
from test_pilot_delivery import delivery_response
from test_pilot_download import CONTENT, download_response
import test_pilot_availability as availability
import test_pilot_notices as notices


class RunTests(unittest.TestCase):
    def setUp(self):
        self.f = notices.NoticeTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.a = availability.AvailabilityTests(); self.a.setUp(); self.addCleanup(self.a.doCleanups)
        self.intake, self.registry = self.a.intake, self.a.registry
        self.sample = self.f.f.root/'synthetic.md'; self.sample.write_bytes(CONTENT)
        self.f.f.frozen[str(self.sample)] = file_sha(self.sample)
        self.f.f.approval.update(limits=dict(self.f.f.approval['limits'], auth=20), maximum_total=60)
        self.f.save_approval()
        original = self.f.f.runner.side_effect
        def transport(c):
            if c.get('preview'): return delivery_response(c)
            if c['kind']=='download': return download_response(c, CONTENT)
            if c['kind']=='send':
                self.f.item['payload'] = json.loads(json.loads(c['body'])['content'])['text']
            return original(c)
        self.f.f.runner.side_effect = transport
        self.session = self.f.make(False)
        self.plan = self.f.f.root/'run-plan.synthetic.json'
        self.plan.write_text(encoded(dict(reference='P11-single-material-v1', sample_path=str(self.sample),
            sample_sha256=file_sha(self.sample), max_pages=1, lookback_seconds=3600)))
        self.f.f.frozen[str(self.plan)] = file_sha(self.plan)
        self.preparer = Mock(side_effect=self.prepare)
        self.run = PilotBoundedRun(self.session, self.intake, self.plan, prepare_candidate=self.preparer,
            checked_ready=lambda: True, resources=ready(self.registry))
        self.addCleanup(self.run.close)
        self.generated = []

    def prepare(self, pipeline, eid):
        job = self.intake._job(eid)
        task = self.intake.conversion.service.ledger.get_task(job['task_id'])
        mapping = json.loads(task['quality_json'])['extraction']
        _, _, source = self.intake.conversion.publication_extraction(mapping['extraction_id'])
        body = (source/'source.md').read_bytes()
        now = datetime.now(timezone.utc)
        record = empty_record('合成收件', 'synthetic-title')
        consent = dict(event_id='synthetic-consent', asset_id=mapping['output_asset_id'], sha256=sha(body),
            extraction_id=mapping['extraction_id'], ranges=[[0,len(body.decode())]], model=MODEL,
            expires_at=(now+timedelta(minutes=20)).isoformat())
        request = ReadingGuideAdapter(PilotTextSession).prepare_request(record, body, mapping, consent)
        root = self.intake.runtime.root/'conversion-work/pilot-text-synthetic'; root.mkdir()
        approval = dict(approved=True,root=str(root),model=MODEL,effort='medium',billing='chatgpt_subscription_only',
            evidence_protocol='authorized-source-index-exact-quote/v1',
            automatic_business_retries=0,max_business_turns=1,wait_seconds=600,
            starts_at=(now-timedelta(minutes=1)).isoformat(),expires_at=(now+timedelta(minutes=30)).isoformat(),
            request_sha256=[sha(encoded(request).encode())],account_sha256=sha(b'synthetic-account@example.invalid'))
        (root/'approval.local.json').write_text(encoded(approval))
        engine = ImagePolicies(self.registry)
        policy = engine.execute(engine.decide([],dict(material_type='unknown',reliable=False,basis_refs=[])),[],
            self.intake.runtime.root/'conversion-work/no-images')
        data = dict(runtime=str(self.intake.runtime.root),scope=asdict(self.intake.scope),text_root=str(root),
            text_approval_sha=file_sha(root/'approval.local.json'),source={str(source/'source.md'):sha(body)},
            marker=str(root/'one-synthetic-generation.txt'),prepare=dict(entry_id=eid,
            extraction_id=mapping['extraction_id'],record=record,consent=consent,policy=policy,
            font_path=os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc')))
        path = root/'pipeline.synthetic.json'; path.write_text(encoded(data))
        result = call(path)
        self.generated.append(result)
        return result

    def test_connected_receive_custody_exact_conversion_guide_pdf_and_pause_await_owner(self):
        # An older local job must not be processed by this material's run.
        other = dict(appId='cli_synthetic',chatId='oc_synthetic',senderId='ou_synthetic',chatType='group',
            messageId='om_older',resources=[dict(type='file',fileKey='file_old',fileName='old.md')])
        older = self.intake.receive(other,0,MockAttachment(b'# Older synthetic input').download)['entry_id']
        result = self.run.run_to_preview()
        self.assertEqual(result['state'], 'awaiting_owner_decision')
        self.assertFalse(result['published']); self.assertFalse(result['service_admission_open'])
        self.assertEqual(result['pause_notice']['delivery_state'], 'delivered')
        self.assertIsNone(self.intake._job(older)['task_id'])
        self.assertIsNotNone(self.intake._job(result['entry_id'])['task_id'])
        self.assertEqual(len(self.generated),1)
        self.assertEqual(Path(self.generated[0]['preview_path']).read_bytes(),
            next(p for p in self.f.f.root.glob('*-download.response.local') if p.read_bytes().startswith(b'%PDF')).read_bytes())
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        before=self.f.f.runner.call_count
        with self.assertRaises(ContractError): self.run.run_to_preview()
        self.assertEqual(before,self.f.f.runner.call_count)

    def test_real_owner_reply_to_exact_delivered_candidate_is_required_for_publication(self):
        result = self.run.run_to_preview()
        self.f.f.message = owner_reply()
        self.f.f.pages = [dict(items=[copy.deepcopy(self.f.f.message)],has_more=False)]
        self.session.scan('scan_owner',start=self.f.f.now-timedelta(minutes=30),end=self.f.f.now)
        decision = self.run.owner_reply('scan_owner','om_owner_reply')
        self.assertEqual(decision['status'], 'published')
        self.assertEqual((Path(decision['path'])/'发布记录/已确认入库预览.pdf').read_bytes(),
                         Path(self.generated[0]['preview_path']).read_bytes() if Path(self.generated[0]['preview_path']).exists()
                         else next(p.read_bytes() for p in self.f.f.root.glob('*-download.response.local') if p.read_bytes().startswith(b'%PDF')))
        evidence=json.loads(self.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='pilot_decision_observed'")[0]['payload_json'])
        self.assertEqual(evidence['reference'],'P11-owner-reply-v1')
        self.assertEqual(self.run.lifecycle.status()['state'],'stopped')

    def test_reply_for_another_preview_cannot_publish_the_saved_candidate(self):
        self.run.run_to_preview()
        self.f.f.message = dict(owner_reply(),parent_id='om_other_preview')
        self.f.f.pages = [dict(items=[self.f.f.message],has_more=False)]
        self.session.scan('scan_wrong',start=self.f.f.now-timedelta(minutes=30),end=self.f.f.now)
        with self.assertRaises(ContractError): self.run.owner_reply('scan_wrong','om_owner_reply')
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_absent_material_is_reported_without_fake_intake_or_preparation(self):
        self.f.f.pages = [dict(items=[],has_more=False)]
        result=self.run.run_to_preview()
        self.assertEqual(result['state'],'waiting_planned_material')
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'))
        self.preparer.assert_not_called()
        self.assertFalse(result['accepted'])

    def test_same_filename_twice_requires_selection_without_downloading_either(self):
        self.f.f.pages[0]['items'].append(dict(self.f.f.message,message_id='om_another'))
        result=self.run.run_to_preview()
        self.assertEqual(result['state'],'ambiguous_planned_material')
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'))
        self.assertEqual(len(result['report']['materials']),2)
        self.preparer.assert_not_called()

    def test_preparation_failure_preserves_accepted_original_and_stops_without_extra_sends(self):
        self.preparer.side_effect=OSError('synthetic model interruption')
        with self.assertRaises(OSError): self.run.run_to_preview()
        self.assertEqual(self.run.lifecycle.status()['state'],'interrupted')
        rows=self.registry.rows('SELECT entry_id FROM intake_entries')
        self.assertEqual(self.registry.entry(rows[0]['entry_id'])['status'],'accepted')
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))
        n=self.f.f.runner.call_count
        with self.assertRaises(ContractError): self.run.run_to_preview()
        self.assertEqual(n,self.f.f.runner.call_count)
        self.assertEqual(self.preparer.call_count,1)

    def test_resource_pause_during_candidate_preparation_stays_pending_without_preview(self):
        self.preparer.side_effect=lambda pipeline,eid:dict(state='paused_resources')
        result=self.run.run_to_preview()
        self.assertEqual(result['state'],'paused_resources')
        self.assertFalse(result['service_admission_open'])
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_changed_plan_rejects_before_any_platform_call(self):
        self.plan.write_text('{}')
        with self.assertRaises(ContractError): self.run.run_to_preview()
        self.f.f.runner.assert_not_called()

    def test_original_damage_after_preparation_blocks_preview_and_records_integrity_hold(self):
        def damage(pipeline,eid):
            result=self.prepare(pipeline,eid)
            (self.intake.runtime.root/self.registry.entry(eid)['relative_path']).write_bytes(b'changed')
            return result
        self.preparer.side_effect=damage
        with self.assertRaises(IntegrityError): self.run.run_to_preview()
        eid=self.registry.rows('SELECT entry_id FROM intake_entries')[0]['entry_id']
        self.assertEqual(self.registry.entry(eid)['custody_health'],'suspect')
        self.assertEqual(self.registry.entry(eid)['status'],'accepted')
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))
