"""Controlled failure revision against the existing intake/text/crash fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.content import ReadingGuideAdapter
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.pilot_revision import digest, rejected_receipt
from kb_custody_v21.pilot_text import MODEL
from kb_custody_v21.pilot_text_grounding import EVIDENCE_PROTOCOL
from kb_custody_v21.registry import encoded
from kb_custody_v21.conversion import sha
from kb_custody_v21.staging import IntegrityError
import test_pilot_pipeline as fixture


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PipelineTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.flow,self.registry,self.entry=self.f.flow,self.f.registry,self.f.entry
        self.original_data=deepcopy(self.f.data)

    def fail(self,legacy=False):
        if legacy:
            root=self.f.text_root
            approval=json.loads((root/'approval.local.json').read_bytes())
            approval.pop('evidence_protocol')
            # Preserve the historical provider identity when deriving old bytes.
            provider=SimpleNamespace(model=MODEL,version='pilot-text/v1',is_mock=False)
            request=ReadingGuideAdapter(provider).prepare_request(self.f.data['prepare']['record'],
                self.f.body,self.f.mapping,self.f.data['prepare']['consent'])
            key=digest(request);approval['request_sha256']=[key]
            (root/'approval.local.json').write_text(encoded(approval))
            self.f.data['text_approval_sha']=digest(approval);self.f.save()
            def generate(*args,**kwargs):
                (root/(key+'.request.local.json')).write_text(encoded(request))
                (root/(key+'.response.local.json')).write_text('{}')
                events=[dict(approval_sha256=digest(approval)),dict(request=key,state='intent',
                    at=datetime.now(timezone.utc).isoformat()),dict(request=key,state='received',response_sha256=sha(b'{}')),
                    dict(request=key,state='received_rejected')]
                (root/'journal.local.jsonl').write_text(''.join(encoded(e)+'\n' for e in events))
                raise ContractError('synthetic historical rejection')
            with self.assertRaises(ContractError):
                self.flow.prepare(**self.f.data['prepare'],text_root=root,text_approval_sha=digest(approval),
                    checked_source=lambda:self.f.data['source'],generate_guide=generate)
        else:
            with patch('pilot_pipeline_fixture.SyntheticQuoteGuide.generate',return_value={}):
                with self.assertRaisesRegex(ContractError,'received_rejected'):self.f.run_pipeline()
        self.old=deepcopy(self.f.data)
        self.previous_plan=self.flow._event(self.entry,'plan')
        self.receipt=self.inspect()
        self.revision=dict(previous_plan_sha256=digest(self.previous_plan),
            previous_text_root=self.old['text_root'],previous_text_approval_sha256=self.old['text_approval_sha'],
            previous_consent=self.old['prepare']['consent'],previous_receipt_sha256=digest(self.receipt))
        self.before={path:Path(path).read_bytes() for path in self.receipt['files']}
        return self.revision

    def inspect(self):
        old=self.old
        return rejected_receipt(old['text_root'],old['text_approval_sha'],old['prepare']['record'],
            self.f.body,self.f.mapping,old['prepare']['consent'],curation_data=None,reader_context=None)

    def new_attempt(self):
        root=self.f.text_root.parent/'pilot-text-revision-synthetic';root.mkdir()
        data=self.f.data
        data['prepare']['revision']=deepcopy(self.revision)
        data['prepare']['consent']=dict(data['prepare']['consent'],event_id='synthetic-revision-consent')
        request=ReadingGuideAdapter(fixture.PilotTextSession).prepare_request(data['prepare']['record'],
            self.f.body,self.f.mapping,data['prepare']['consent'])
        approval=json.loads((self.f.text_root/'approval.local.json').read_bytes())
        approval.update(root=str(root),request_sha256=[digest(request)],revision_sha256=digest(self.revision),
            evidence_protocol=EVIDENCE_PROTOCOL)
        (root/'approval.local.json').write_text(encoded(approval))
        data.update(text_root=str(root),text_approval_sha=digest(approval),marker=str(root/'synthetic-generation.txt'))
        data['source'].update(self.receipt['files'])
        self.f.save()
        return root

    def assert_old_unchanged(self):
        self.assertEqual(self.flow._event(self.entry,'plan'),self.previous_plan)
        for path,raw in self.before.items():self.assertEqual(Path(path).read_bytes(),raw)
        self.assertIsNone(self.flow._event(self.entry,'prepared'))

    def test_explicit_success_keeps_entry_old_events_and_exact_owner_confirmation(self):
        self.fail();self.new_attempt()
        first=self.f.run_pipeline();self.assertEqual(self.f.run_pipeline(),first)
        self.assertEqual(first['entry_id'],self.entry)
        self.assertEqual(self.flow.status(self.entry)['state'],'preview_ready_pending_delivery')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)
        self.assertEqual(len(self.registry.rows("SELECT * FROM asset_references WHERE kind='staging_dependency'")),1)
        self.assert_old_unchanged()
        previews=self.flow.previews;scope=self.f.intake.scope;pid=first['publication_id']
        previews.bind_delivery(pid,app_id=scope.app_id,chat_id=scope.chat_id,message_id='synthetic-revision-preview',
            pdf_sha256=first['preview_sha256'],receipt_ref='synthetic-receipt',outcome='sent')
        self.assertEqual(self.flow.status(self.entry)['state'],'awaiting_owner_decision')
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        decision=dict(app_id=scope.app_id,chat_id=scope.chat_id,user_id=scope.user_id,
            message_id='synthetic-owner-reply',reply_to='synthetic-revision-preview',text='确认入库')
        previews.decide(**decision)
        self.assertEqual(self.flow.status(self.entry)['state'],'published')
        self.assertEqual(previews.publisher._row(pid)['preview_sha256'],first['preview_sha256'])

    def test_legacy_complete_rejection_can_get_explicit_new_protocol_success(self):
        self.fail(legacy=True);self.new_attempt()
        self.assertEqual(self.f.run_pipeline()['state'],'preview_ready_pending_delivery')
        self.assert_old_unchanged()

    def test_only_complete_received_rejected_chain_is_eligible(self):
        self.fail()
        path=Path(self.old['text_root'])/'journal.local.jsonl'
        events=[json.loads(line) for line in path.read_bytes().splitlines()]
        invalid=[events[:2],events[:3],events[:3]+[dict(events[3],state='unknown')],
                 events[:3]+[dict(events[3],state='blocked_before_turn')],
                 events[:3]+[dict(events[3],state='validated')],events+events[1:2],
                 events[:2]+[dict(events[2],request='f'*64)]+events[3:]]
        for chain in invalid:
            with self.subTest(states=[e.get('state') for e in chain]):
                path.write_text(''.join(encoded(e)+'\n' for e in chain))
                with self.assertRaises(ContractError):self.inspect()

    def test_missing_or_changed_previous_evidence_is_rejected(self):
        self.fail()
        for name in self.receipt['files']:
            path=Path(name);raw=path.read_bytes()
            with self.subTest(file=path.name):
                path.write_bytes(b'changed')
                with self.assertRaises(ContractError):self.inspect()
                path.unlink()
                with self.assertRaises(ContractError):self.inspect()
                path.write_bytes(raw)

    def test_partial_validated_artifact_prevents_revision(self):
        self.fail()
        root=Path(self.old['text_root']);key=self.receipt['request_sha256']
        for suffix in ('validated','grounded','grounding'):
            path=root/(key+'.'+suffix+'.local.json');path.write_text('{}')
            with self.assertRaisesRegex(ContractError,'partial or completed'):self.inspect()
            path.unlink()

    def test_changed_old_response_and_updated_journal_still_fail_frozen_fingerprint(self):
        self.fail();self.new_attempt()
        root=Path(self.old['text_root']);key=self.receipt['request_sha256']
        (root/(key+'.response.local.json')).write_text('{"changed":true}')
        path=root/'journal.local.jsonl';events=[json.loads(v) for v in path.read_bytes().splitlines()]
        events[2]['response_sha256']=sha((root/(key+'.response.local.json')).read_bytes())
        path.write_text(''.join(encoded(e)+'\n' for e in events))
        with self.assertRaisesRegex(ContractError,'fingerprint'):self.f.run_pipeline()
        self.assertFalse(Path(self.f.data['marker']).exists())

    def test_original_corruption_blocks_before_new_generation(self):
        self.fail();self.new_attempt()
        (self.f.runtime.root/self.registry.entry(self.entry)['relative_path']).write_bytes(b'changed')
        with self.assertRaises(IntegrityError):self.f.run_pipeline()
        self.assertFalse(Path(self.f.data['marker']).exists())

    def test_changed_extraction_or_old_plan_inputs_cannot_be_rebound(self):
        self.fail();self.new_attempt()
        original=deepcopy(self.f.data['prepare'])
        for key in ('record','policy'):
            with self.subTest(key=key):
                self.f.data['prepare'][key]['synthetic-change']=True;self.f.save()
                with self.assertRaises(ContractError):self.f.run_pipeline()
                self.f.data['prepare']=deepcopy(original)
        path=Path(next(iter(self.f.data['source'])))
        path.write_bytes(b'changed extraction');self.f.save()
        with self.assertRaises(ContractError):self.f.run_pipeline()
        self.assertFalse(Path(self.f.data['marker']).exists())

    def test_old_or_unbound_approval_cannot_authorize_revision(self):
        self.fail();root=self.new_attempt()
        path=root/'approval.local.json';original=json.loads(path.read_bytes())
        for updates in (dict(approved=False),dict(revision_sha256='a'*64),dict(max_business_turns=2),
                        dict(automatic_business_retries=1)):
            with self.subTest(updates=updates):
                approval=dict(original,**updates);path.write_text(encoded(approval))
                self.f.data['text_approval_sha']=digest(approval);self.f.save()
                with self.assertRaises(ContractError):self.f.run_pipeline()
        self.f.data.update(text_root=self.old['text_root'],text_approval_sha=self.old['text_approval_sha']);self.f.save()
        with self.assertRaises(ContractError):self.f.run_pipeline()
        self.assertIsNone(self.flow._event(self.entry,'revision'))

    def test_base_candidate_state_and_missing_dependency_prevent_successor(self):
        self.fail();self.new_attempt()
        original=self.flow._event
        for phase in ('preview_started','prepared','staged'):
            with self.subTest(phase=phase),patch.object(fixture.PilotPipeline,'_event',
                    side_effect=lambda e,p:dict(synthetic=True) if p==phase else original(e,p)):
                with self.assertRaises(ContractError):self.f.run_pipeline()
        with self.registry.transaction() as db:
            db.execute("UPDATE asset_references SET released_at='synthetic-release' WHERE kind='staging_dependency'")
        with self.assertRaisesRegex(ContractError,'retention'):self.f.run_pipeline()
        self.assertFalse(Path(self.f.data['marker']).exists())

    def test_revision_unknown_is_not_reissued_or_replaced_with_another_budget(self):
        self.fail();self.new_attempt()
        with patch('pilot_pipeline_fixture.SyntheticQuoteGuide.generate',side_effect=OSError('synthetic unknown')) as generate:
            with self.assertRaises(ContractError):self.f.run_pipeline()
            with self.assertRaises(ContractError):self.f.run_pipeline()
            self.assertEqual(generate.call_count,1)
        self.assertEqual(self.flow.status(self.entry)['state'],'text_receipt_needs_check')
        self.f.data['prepare']['consent']['event_id']='another-attempt';self.f.save()
        with self.assertRaisesRegex(ContractError,'no second successor'):self.f.run_pipeline()
        self.assert_old_unchanged()

    def test_actual_child_death_before_text_does_not_submit_on_restart(self):
        self.fail();self.new_attempt()
        with self.assertRaises(ContractError):self.f.crash('before_text')
        self.assertFalse(Path(self.f.data['marker']).exists())
        self.assertEqual(self.flow.status(self.entry)['state'],'text_receipt_needs_check')

    def test_actual_child_death_after_submission_preserves_unknown_without_resend(self):
        self.fail();self.new_attempt()
        with self.assertRaises(ContractError):self.f.crash('text_submitted')
        self.assertTrue(Path(self.f.data['marker']).exists())
        events=[json.loads(line) for line in (Path(self.f.data['text_root'])/'journal.local.jsonl').read_bytes().splitlines()]
        self.assertEqual([e.get('state') for e in events],[None,'intent'])
        with self.assertRaises(ContractError):self.f.run_pipeline()
        self.assertEqual(self.flow.status(self.entry)['state'],'text_receipt_needs_check')
        self.assert_old_unchanged()

    def test_actual_child_death_after_text_reuses_one_result(self):
        self.fail();self.new_attempt()
        result=self.f.crash('after_text')
        self.assertEqual(self.f.run_pipeline(),result)
        self.assert_old_unchanged()

    def test_actual_child_death_after_candidate_commit_reuses_same_pdf(self):
        self.fail();self.new_attempt()
        result=self.f.crash('after_preview_commit')
        self.assertEqual(self.f.run_pipeline(),result)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)

    def test_revision_render_failure_is_not_rerendered_or_resubmitted(self):
        self.fail();self.new_attempt()
        with patch('kb_custody_v21.publication.render_preview',side_effect=OSError('synthetic render')) as render:
            with self.assertRaises(OSError):self.f.run_pipeline()
            with self.assertRaisesRegex(ContractError,'interrupted before commit'):self.f.run_pipeline()
            self.assertEqual(render.call_count,1)

    def test_old_entry_cannot_resume_after_explicit_successor(self):
        self.fail();self.new_attempt();self.f.run_pipeline()
        self.f.data=self.old;self.f.save()
        with self.assertRaisesRegex(ContractError,'old entry cannot resume'):self.f.run_pipeline()

    def test_old_evidence_changed_during_new_generation_stops_preview(self):
        self.fail();self.new_attempt()
        def change(point):
            if point=='after_text':
                root=Path(self.old['text_root']);(root/(self.receipt['request_sha256']+'.response.local.json')).write_bytes(b'changed')
        with self.assertRaises(ContractError):self.f.run_pipeline(hook=change)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))

    def test_changed_previous_plan_fingerprint_is_rejected(self):
        self.fail();self.new_attempt()
        original=self.flow._event
        with patch.object(fixture.PilotPipeline,'_event',side_effect=lambda e,p:
                dict(self.previous_plan,at='changed') if p=='plan' else original(e,p)):
            with self.assertRaisesRegex(ContractError,'previous pipeline'):self.f.run_pipeline()
        self.assertFalse(Path(self.f.data['marker']).exists())
