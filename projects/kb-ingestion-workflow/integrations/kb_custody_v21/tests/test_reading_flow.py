"""Feishu-to-reading integration with synthetic transport/model only."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_scheduler import MaterialProblem
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.reading_flow import ReadingFlow
from kb_custody_v21.reading_profile import ReadingProfile
from kb_custody_v21.registry import encoded
import test_daily_backend as daily_fixture
from test_reading_content import sample, reviewed


def draft_for(source):
    _,draft=sample()
    quote=source['original'].strip()
    draft['title']='合成材料整理'
    draft['identity_evidence']=[dict(source='filename',quote=source['filename'])]
    evidence=dict(source='original',quote=quote)
    draft['claims'][0].update(text='合成测试材料中的来源陈述。',evidence=[evidence])
    draft['keywords']['objective']=[]
    draft['cards']=[];draft['topics']=[]
    if source['image_boundary']:
        draft['limitations']=[source['image_boundary']]
    return draft


class SyntheticReadingWire:
    def __init__(self):
        self.calls=[];self.transform=lambda purpose,value: value;self.fail=False
    def exchange(self,packet,before_access):
        before_access()
        request=json.loads(packet['request']);self.calls.append(request)
        if self.fail:raise OSError('synthetic unknown remote result')
        source=request['input']
        if request['purpose']=='organize':
            value=draft_for(source)
        else:
            value=reviewed(source,request['draft'])
            if source['image_boundary']:value['validation_status']='human_reviewed_limited_source'
        value=self.transform(request['purpose'],value)
        return dict(status='received',stage='received',text=encoded(value))


class ReadingFlowTests(unittest.TestCase):
    def setUp(self):
        self.f=daily_fixture.DailyBackendTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.b=self.f.backend;self.a=self.f.authority;self.daily=self.f.store
        root=self.a.root/'synthetic-reading-rules';root.mkdir()
        script=root/'synthetic-reading.mjs';script.write_text('// offline synthetic adapter')
        frozen={str(script):file_sha(script)};rules={}
        for role in ('archive_rules','organize_prompt','nlp_skill'):
            path=root/(role+'.md');path.write_text('Synthetic standard. No production library.')
            frozen[str(path)]=file_sha(path);rules[role]=dict(path=str(path),sha256=file_sha(path))
        self.config=dict(reference='P41-reading-v1',root=str(self.a.runtime.root/'reading-library'),
            rules=rules,script=str(script),generation=dict(enabled=True,account_sha256='f'*64,wait_seconds=60,minimum_remaining_percent=5))
        self.frozen=frozen
        self.a.reading=ReadingProfile(self.a.runtime,self.config,frozen)
        self.wire=SyntheticReadingWire()
        self.flow=ReadingFlow(self.b,wire=self.wire);self.b.reading_flow=self.flow

    def approve(self):
        document=self.f.preview();self.f.owner_reply(document);self.b.advance_light(document)
        self.assertEqual(self.document()['phase'],'reading')
        return self.document()

    def document(self):return self.daily.document(self.f.mid)

    def step(self):self.b.advance_heavy(self.document())

    def finish(self):
        for _ in range(8):
            if self.document()['phase']!='reading':break
            self.step()
        self.assertEqual(self.document()['phase'],'published')
        self.b.advance_light(self.document())

    def test_approved_file_completes_notes_and_brief_with_two_shared_calls(self):
        self.approve();self.finish()
        self.assertEqual(self.document()['phase'],'notified')
        self.assertTrue(self.document()['data']['reading_completed'])
        self.assertEqual([r['purpose'] for r in self.wire.calls],['organize','review'])
        self.assertEqual(self.f.g.wire.calls,1) # Existing guide reused.
        self.assertEqual(self.daily.summary()['model_turns'],3) # Same cumulative table.
        group=self.a.reading.root/self.document()['data']['reading_group']
        self.assertTrue((group/'综合读书笔记.md').exists())
        self.assertEqual(json.loads((group/'metadata.json').read_text())['status'],'completed')
        text=json.loads(self.document()['data']['completion_notice']['receipt']['value']['data']['body']['content'])['text']
        self.assertIn('完整整理',text)

    def test_without_new_generation_approval_it_prepares_request_without_spending(self):
        config=dict(self.config);config.pop('generation')
        self.a.reading=ReadingProfile(self.a.runtime,config,self.frozen)
        self.flow=ReadingFlow(self.b,wire=self.wire);self.b.reading_flow=self.flow
        self.approve();self.step()
        with self.assertRaisesRegex(Deferred,'reading_model_approval_required'):self.step()
        self.assertEqual(self.daily.summary()['model_turns'],1)
        self.assertEqual(self.wire.calls,[])
        self.assertTrue(list(self.a.root.glob('reading-jobs/*/model/*/approval-draft.local.json')))
        self.assertFalse(self.a.reading.root.exists())

    def test_unknown_turn_is_never_replayed_after_restart(self):
        self.approve();self.step();self.wire.fail=True
        with self.assertRaises(Deferred):self.step()
        self.b.reading_flow=ReadingFlow(self.b,wire=self.wire)
        with self.assertRaisesRegex(Deferred,'unknown_no_resubmit'):self.step()
        self.assertEqual(len(self.wire.calls),1);self.assertEqual(self.daily.summary()['model_turns'],2)

    def test_received_envelope_recovers_without_second_generation(self):
        self.approve();self.step()
        save=self.flow.store.save_call_result
        def crash(*a,**k):raise OSError('synthetic crash before receipt validation commit')
        self.flow.store.save_call_result=crash
        with self.assertRaises(OSError):self.step()
        self.flow.store.save_call_result=save
        self.finish()
        self.assertEqual(len(self.wire.calls),2)

    def test_semantic_review_rejects_without_publishing_complete_content(self):
        def transform(purpose,value):
            if purpose=='review':value.update(status='reject',findings=['合成覆盖缺失'])
            return value
        self.wire.transform=transform
        self.approve();self.step();self.step()
        with self.assertRaises(MaterialProblem):self.step()
        material=self.flow.store.get(self.document()['data']['reading_material_id'])
        self.assertEqual(material['state'],'failed');self.assertFalse(self.a.reading.root.exists())
        self.assertFalse(self.document()['data'].get('reading_completed'))

    def test_changed_source_or_receipt_blocks_before_reading_model(self):
        self.approve();self.step()
        document=self.document();path=next(iter(document['data']['decision']['receipts']))
        Path(path).write_text('changed')
        with self.assertRaises(ContractError):self.step()
        self.assertEqual(self.wire.calls,[])

    def test_expired_standing_material_window_does_not_renew_itself(self):
        self.approve();material=self.flow.store.get(self.document()['data']['reading_material_id'])
        self.daily.clock=lambda:material['created']+11*3600
        self.step()
        with self.assertRaisesRegex(ContractError,'expired'):self.step()
        self.assertEqual(self.daily.summary()['model_turns'],1)
        self.assertEqual(self.wire.calls,[])

    def test_shared_cap_holds_before_review_without_resetting_guide_usage(self):
        self.approve();self.daily.policy=replace(self.daily.policy,model_turn_limit=2)
        self.step();self.step()
        with self.assertRaisesRegex(Deferred,'model_limit'):self.step()
        self.assertEqual(len(self.wire.calls),1);self.assertEqual(self.daily.summary()['model_turns'],2)
        self.assertFalse(self.a.reading.root.exists())

    def test_question_keeps_original_and_resumes_only_from_owner_reply(self):
        self.daily.policy=replace(self.daily.policy,model_turn_limit=6)
        asked=[]
        def transform(purpose,value):
            if purpose=='organize' and not asked:
                value.update(status='needs_user_input',question='这是课程讲义还是独立网页文章？');asked.append(True)
            return value
        self.wire.transform=transform
        self.approve();self.step();self.step()
        self.assertEqual(self.document()['phase'],'reading_wait')
        self.assertFalse(self.a.reading.root.exists())
        self.b.advance_light(self.document())
        material=self.flow.store.get(self.document()['data']['reading_material_id'])
        key='notice:reading-question:'+material['material_id']+':0'
        notice=self.f.transport.results[key]
        parent=notice['value']['data']['message_id']
        item=dict(message_id='om_clarification',chat_id=self.a.policy.chat_id,parent_id=parent,
            msg_type='text',deleted=False,updated=False,sender=dict(id=self.a.policy.account_id,id_type='open_id',sender_type='user'),
            body=dict(content=encoded(dict(text='这是独立网页文章。'))))
        path=self.a.root/'synthetic-answer.local.json';path.write_text(encoded(dict(code=0,data=dict(items=[item]))))
        receipt=dict(value=json.loads(path.read_text()),receipt=str(path),evidence={str(path):file_sha(path)})
        scan=self.daily.scan('chat',initial_start=self.daily.now()-60);self.daily.save_page(scan,[item],has_more=False)
        self.b.collector.message=lambda _:(item,receipt,receipt)
        self.b.advance_light(self.document())
        self.assertEqual(self.document()['phase'],'reading')
        self.finish()
        self.assertEqual([r['purpose'] for r in self.wire.calls],['organize','organize','review'])
        self.assertEqual(self.daily.summary()['model_turns'],4)
        self.assertEqual(self.wire.calls[1]['input']['user_answers'][0]['text'],'这是独立网页文章。')

    def test_progress_card_failure_has_bounded_text_fallback_without_finishing_business(self):
        self.approve()
        document=self.document()
        with self.daily.transaction() as db:
            data=dict(document['data'],card_id='synthetic_card')
            db.execute('UPDATE daily_documents SET data=? WHERE message_id=?',(encoded(data),document['message_id']))
        def update(card,text):raise MaterialProblem('synthetic card display failure')
        self.b.card_flow=SimpleNamespace(cards=SimpleNamespace(get=lambda _:dict(receipt=True)),delivery=SimpleNamespace(update=update))
        self.flow.report();calls=len(self.f.transport.calls)
        self.flow.report();self.assertEqual(len(self.f.transport.calls),calls)
        self.assertEqual(self.document()['phase'],'reading')
        self.assertEqual(self.wire.calls,[])

    def test_enabling_profile_does_not_backfill_old_completed_archives(self):
        self.b.reading_flow=None
        document=self.f.preview();self.f.owner_reply(document);self.b.advance_light(document)
        self.assertEqual(self.document()['phase'],'notified')
        self.b.reading_flow=self.flow;self.b.recover()
        self.assertEqual(self.flow.store.store.rows('SELECT * FROM reading_materials'),[])
        self.assertEqual(self.wire.calls,[])


if __name__=='__main__':unittest.main()
