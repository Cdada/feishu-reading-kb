"""Proven pre-turn continuation preserves both historical attempts and budgets."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kb_custody_v21.content import ReadingGuideAdapter
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_continuation import blocked_receipt, continuation_fields
from kb_custody_v21.pilot_revision import digest
from kb_custody_v21.pilot_text import PilotTextSession
from kb_custody_v21.registry import encoded
import test_pilot_revision as fixture


def source_evidence(parent, approval):
    parent.mkdir(parents=True,exist_ok=True)
    files={'synthetic.py':sha(b'synthetic source')}
    manifest=dict(source_commit='1'*40,source_snapshot=dict(files=files,
        sha256=sha(json.dumps(files,sort_keys=True).encode())))
    path=parent/'source-manifest.local.json';path.write_text(encoded(manifest))
    (parent/'executed-source.local.json').write_text(encoded(manifest))
    approval.update(approved=True,root=str(parent),reference='P11',source_manifest=str(path),
        source_manifest_sha256=file_sha(path))
    (parent/'approval.local.json').write_text(encoded(approval))
    return digest(approval)


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.r=fixture.RevisionTests();self.r.setUp();self.addCleanup(self.r.doCleanups)
        self.r.fail();self.blocked_root=self.r.new_attempt()
        self.f=self.r.f;self.flow=self.r.flow;self.entry=self.r.entry
        self.parent=self.blocked_root.parent/'stopped-parent'/'p11-requests'
        parent_sha=source_evidence(self.parent,{})
        path=self.blocked_root/'approval.local.json';a=json.loads(path.read_bytes())
        a['parent_approval_sha256']=parent_sha;path.write_text(encoded(a))
        self.f.data.update(text_approval_sha=digest(a),wire_status='blocked_before_turn');self.f.save()
        with self.assertRaisesRegex(ContractError,'blocked_before_turn'):self.f.run_pipeline()
        self.stopped=deepcopy(self.f.data)
        self.pointer=self.flow._event(self.entry,'revision')
        self.previous_plan=self.flow.attempt_event(self.entry,'plan')
        self.binding=dict(previous_plan_sha256=digest(self.previous_plan),previous_text_root=str(self.blocked_root),
            previous_text_approval_sha256=digest(a),previous_consent=self.stopped['prepare']['consent'],
            previous_receipt_sha256='0'*64,previous_parent_root=str(self.parent),previous_revision_sha256=digest(self.pointer))
        self.receipt=self.inspect();self.binding['previous_receipt_sha256']=digest(self.receipt)
        self.before={name:Path(name).read_bytes() for name in self.receipt['files']}

    def inspect(self):
        return blocked_receipt(self.binding,self.f.data['prepare']['record'],self.f.body,self.f.mapping,
            curation_data=None,reader_context=None)

    def new_attempt(self):
        root=self.blocked_root.parent/'pilot-text-continuation-synthetic';root.mkdir()
        data=self.f.data;data.pop('wire_status')
        data['prepare']['continuation']=deepcopy(self.binding)
        data['prepare']['consent']=dict(data['prepare']['consent'],event_id='synthetic-continuation-consent')
        request=ReadingGuideAdapter(PilotTextSession).prepare_request(data['prepare']['record'],
            self.f.body,self.f.mapping,data['prepare']['consent'])
        a=json.loads((self.blocked_root/'approval.local.json').read_bytes())
        a.update(root=str(root),request_sha256=[digest(request)],continuation_sha256=digest(self.binding),
            parent_approval_sha256='2'*64)
        (root/'approval.local.json').write_text(encoded(a))
        data.update(text_root=str(root),text_approval_sha=digest(a),marker=str(root/'synthetic-generation.txt'))
        data['source'].update(self.receipt['files']);self.f.save()
        return root

    def assert_history(self):
        self.r.assert_old_unchanged()
        self.assertEqual(self.flow._event(self.entry,'revision'),self.pointer)
        for name,raw in self.before.items():self.assertEqual(Path(name).read_bytes(),raw)

    def test_one_success_reuses_original_candidate_and_requires_exact_owner_reply(self):
        self.new_attempt();first=self.f.run_pipeline();self.assertEqual(first,self.f.run_pipeline())
        self.assertEqual(first['entry_id'],self.entry);self.assert_history()
        self.assertEqual(len(self.r.registry.rows('SELECT * FROM publications_v21')),1)
        self.assertFalse(self.r.registry.rows('SELECT * FROM publication_confirmations'))
        p=self.flow.previews;s=self.f.intake.scope;pid=first['publication_id']
        p.bind_delivery(pid,app_id=s.app_id,chat_id=s.chat_id,message_id='continuation-preview',
            pdf_sha256=first['preview_sha256'],receipt_ref='synthetic-receipt',outcome='sent')
        self.assertEqual(self.flow.status(self.entry)['state'],'awaiting_owner_decision')
        p.decide(app_id=s.app_id,chat_id=s.chat_id,user_id=s.user_id,message_id='continuation-reply',
            reply_to='continuation-preview',text='确认入库')
        self.assertEqual(self.flow.status(self.entry)['state'],'published')

    def test_only_complete_blocked_chain_is_eligible(self):
        path=self.blocked_root/'journal.local.jsonl';original=path.read_bytes()
        events=[json.loads(e) for e in original.splitlines()]
        invalid=[events[:1],events[:2],events+events[1:2]]
        invalid.extend(events[:2]+[dict(events[2],state=s)]
            for s in ('unknown','received','received_rejected','validated','dry_run'))
        for chain in invalid:
            with self.subTest(states=[e.get('state') for e in chain]):
                path.write_text(''.join(encoded(e)+'\n' for e in chain))
                with self.assertRaises(ContractError):self.inspect()
        path.write_bytes(original)

    def test_response_or_validation_artifacts_prevent_continuation(self):
        for suffix in ('response','validated','grounded','grounding'):
            path=self.blocked_root/(self.receipt['request_sha256']+'.'+suffix+'.local.json')
            path.write_text('{}')
            with self.assertRaisesRegex(ContractError,'artifact'):self.inspect()
            path.unlink()

    def test_each_previous_evidence_and_source_file_must_remain_exact(self):
        for name,raw in self.before.items():
            with self.subTest(name=Path(name).name):
                Path(name).write_text('{}')
                with self.assertRaises(ContractError):self.inspect()
                Path(name).write_bytes(raw)

    def test_stopped_fingerprint_and_original_revision_pointer_are_immutable(self):
        self.new_attempt()
        for key in ('previous_receipt_sha256','previous_plan_sha256','previous_revision_sha256'):
            self.f.data['prepare']['continuation']=dict(self.binding,**{key:'f'*64});self.f.save()
            with self.assertRaises(ContractError):self.f.run_pipeline()
        self.assertFalse(Path(self.f.data['marker']).exists())

    def test_binding_rejects_extra_fields_relative_paths_and_missing_parent(self):
        for updates in (dict(extra=True),dict(previous_parent_root='p11-requests'),
                        dict(previous_revision_sha256='unknown')):
            with self.subTest(updates=updates),self.assertRaises(ContractError):
                continuation_fields(dict(self.binding,**updates))

    def test_new_approval_must_bind_continuation_and_one_turn(self):
        root=self.new_attempt();path=root/'approval.local.json';base=json.loads(path.read_bytes())
        for updates in (dict(continuation_sha256='0'*64),dict(max_business_turns=2),
                        dict(automatic_business_retries=1),dict(approved=False),
                        dict(parent_approval_sha256=file_sha(self.parent/'approval.local.json'))):
            path.write_text(encoded(dict(base,**updates)));self.f.data['text_approval_sha']=file_sha(path);self.f.save()
            with self.assertRaises(ContractError):self.f.run_pipeline()
        self.assertIsNone(self.flow._event(self.entry,'continuation'))

    def test_old_revision_and_original_cannot_resume_after_continuation(self):
        self.new_attempt();self.f.run_pipeline()
        for old in (self.stopped,self.r.old):
            self.f.data=deepcopy(old);self.f.save()
            with self.assertRaisesRegex(ContractError,'cannot resume'):self.f.run_pipeline()

    def test_unknown_cannot_repeat_or_receive_second_continuation_budget(self):
        self.new_attempt()
        with patch('pilot_pipeline_fixture.SyntheticQuoteGuide.generate',side_effect=OSError('unknown')) as generate:
            with self.assertRaises(ContractError):self.f.run_pipeline()
            with self.assertRaises(ContractError):self.f.run_pipeline()
            self.assertEqual(generate.call_count,1)
        self.f.data['prepare']['consent']['event_id']='another';self.f.save()
        with self.assertRaisesRegex(ContractError,'no second continuation'):self.f.run_pipeline()
        self.assert_history()

    def test_actual_crash_before_text_cannot_submit_on_restart(self):
        self.new_attempt()
        with self.assertRaises(ContractError):self.f.crash('before_text')
        self.assertFalse(Path(self.f.data['marker']).exists());self.assert_history()

    def test_actual_crash_after_submission_cannot_resend(self):
        self.new_attempt()
        with self.assertRaises(ContractError):self.f.crash('text_submitted')
        with self.assertRaises(ContractError):self.f.run_pipeline()
        self.assertTrue(Path(self.f.data['marker']).exists());self.assert_history()

    def test_actual_crash_after_text_reuses_result(self):
        self.new_attempt();result=self.f.crash('after_text')
        self.assertEqual(result,self.f.run_pipeline());self.assert_history()

    def test_actual_crash_after_preview_commit_reuses_pdf(self):
        self.new_attempt();result=self.f.crash('after_preview_commit')
        self.assertEqual(result,self.f.run_pipeline());self.assert_history()

    def test_changed_previous_source_after_model_blocks_preview(self):
        self.new_attempt()
        def change(point):
            if point=='after_text':(self.parent/'executed-source.local.json').write_text('{}')
        with self.assertRaises(ContractError):self.f.run_pipeline(hook=change)
        self.assertFalse(self.r.registry.rows('SELECT * FROM publications_v21'))

    def test_existing_revision_preview_state_prevents_continuation(self):
        self.new_attempt();original=self.flow._event;prefix='revision:'+self.pointer['attempt_id']+':'
        for phase in ('preview_started','prepared','staged'):
            with patch('kb_custody_v21.pilot_pipeline.PilotPipeline._event',side_effect=lambda e,p:
                    dict(synthetic=True) if p==prefix+phase else original(e,p)):
                with self.assertRaises(ContractError):self.f.run_pipeline()
        self.assertFalse(Path(self.f.data['marker']).exists())
