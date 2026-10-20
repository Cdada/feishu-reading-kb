"""Question lifecycle, cost ceiling and evidence faults with synthetic models."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError, stable_key
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_store import DailyStore, Deferred
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.qa_engine import QAEngine
from kb_custody_v21.qa_index import QAIndex
from kb_custody_v21.qa_model import QAModel, validate, render_answer, render_evidence, comparison_coverage
from kb_custody_v21.qa_store import QAStore
from kb_custody_v21.registry import encoded
from test_qa_index import source, Provider


class Wire:
    def __init__(self): self.calls=[]; self.failure=False; self.transform=lambda v:v
    def exchange(self, packet, check):
        check(); self.calls.append(packet)
        if self.failure: raise OSError('synthetic timeout')
        evidence=json.loads(packet['request'])['evidence'][0]
        value=dict(status='grounded',claims=[dict(text='合成材料使用证据地图。',
                   evidence=[dict(evidence_id=evidence['evidence_id'],quote=evidence['text'])])],gaps=[])
        return dict(status='received',text=encoded(self.transform(value)))


class QAEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.clock=1800000000
        self.daily=DailyStore(self.root/'daily.sqlite3',DailyPolicy('cli_synthetic','oc_synthetic','ou_owner','ou_bot',3),clock=lambda:self.clock)
        self.store=QAStore(self.daily)
        self.provider=Provider(source(text='城市演变研究使用证据地图。'), source('two',text='城市演变研究需要结合访谈。'))
        self.proof=self.root/'synthetic-proof.json';self.proof.write_text('{}')
        self.provider.verify=lambda hits:{str(self.proof):file_sha(self.proof)}
        self.index=QAIndex(self.root/'qa.sqlite3')
        self.a=SimpleNamespace(root=self.root,digest='a'*64,check=lambda:{str(self.proof):file_sha(self.proof)})
        self.profile=SimpleNamespace(generation=dict(account_sha256='f'*64,wait_seconds=60,minimum_remaining_percent=5))
        self.wire=Wire();self.model=QAModel(self.a,self.profile,self.store,self.provider,self.wire)
        self.engine=QAEngine(self.store,self.provider,self.index,self.model)
        self.number=0

    def job(self, text, conversation='chat'):
        self.number+=1
        return self.store.enqueue(stable_key('synthetic-qa',str(self.number)), 'om_question'+str(self.number), text,
                                  dict(receipts={str(self.proof):file_sha(self.proof)}),conversation=conversation)

    def run_job(self,text,**kwargs):
        job=self.job(text,**kwargs);self.engine.run(job);return self.store.get(job['id'])

    def test_search_then_followup_has_exact_quotes_and_one_shared_call(self):
        result=self.run_job('查文献 城市演变')
        self.assertEqual(len(self.wire.calls),0)
        self.assertEqual(self.daily.summary()['model_turns'],0)
        result=self.run_job('追问 有什么方法')
        self.assertEqual(result['result']['status'],'grounded')
        request=json.loads(self.wire.calls[0]['request'])
        self.assertEqual(request['prior_question'],'城市演变')
        self.assertEqual(self.daily.summary()['model_turns'],1)
        self.assertIn('原文：',result['result']['text'])

    def test_exhausted_existing_archive_budget_cannot_reset_for_qa(self):
        for c in 'abc': self.daily.reserve_model(c*64)
        result=self.run_job('综述 城市演变')
        self.assertEqual(result['result']['status'],'model_limit')
        self.assertIn('未生成综合回答',result['result']['text'])
        self.assertEqual(len(self.wire.calls),0)
        self.assertEqual(self.daily.summary()['model_turns'],3)

    def test_no_generation_grant_is_local_retrieval_only(self):
        self.profile.generation=None
        result=self.run_job('综述 城市演变')
        self.assertEqual(result['result']['status'],'qa_model_approval_required')
        self.assertEqual(self.daily.summary()['model_turns'],0)

    def test_unknown_call_is_not_replayed_after_restart(self):
        self.wire.failure=True
        job=self.job('综述 城市演变');self.engine.run(job)
        self.assertEqual(len(self.wire.calls),1)
        saved=self.store.get(job['id'])
        new=QAModel(self.a,self.profile,QAStore(self.daily),self.provider,self.wire)
        with self.assertRaisesRegex(Deferred,'no_resubmit'):new.run(saved,saved['plan'])
        self.assertEqual(len(self.wire.calls),1)
        self.assertEqual(self.daily.summary()['model_turns'],1)

    def test_received_result_recovers_without_second_turn(self):
        job=self.job('综述 城市演变')
        save=self.store.call_result
        self.store.call_result=lambda *a: (_ for _ in ()).throw(OSError('synthetic crash'))
        with self.assertRaises(OSError):self.engine.run(job)
        self.store.call_result=save
        self.engine.run(self.store.get(job['id']))
        self.assertEqual(len(self.wire.calls),1)
        self.assertEqual(self.store.get(job['id'])['result']['status'],'grounded')

    def test_fabricated_or_unbound_quote_is_rejected(self):
        def tamper(value):
            value['claims'][0]['evidence'][0]['quote']='不在任何原文中的证据。';return value
        self.wire.transform=tamper
        result=self.run_job('综述 城市演变')
        self.assertEqual(result['result']['status'],'qa_model_rejected')
        self.assertNotIn('不在任何原文',result['result']['text'])

    def test_no_hit_does_not_spend_a_model_turn(self):
        result=self.run_job('综述 天体轨道')
        self.assertEqual(result['result']['hits'],[]);self.assertEqual(self.wire.calls,[])

    def test_exact_quote_lookup_does_not_return_partial_words(self):
        result=self.run_job('查出处 “城市演变研究使用航天地图”')
        self.assertEqual(result['result']['hits'],[])

    def test_selected_missing_source_stops_comparison(self):
        self.run_job('查文献 城市演变')
        del self.provider.values['two']
        result=self.run_job('比较 1,2 方法')
        self.assertEqual(result['result']['status'],'partial_retrieval')
        self.assertEqual(self.wire.calls,[])

    def test_context_isolated_by_conversation_and_expires(self):
        self.run_job('查文献 城市演变',conversation='thread-a')
        self.assertEqual(self.store.context('thread-b'),{})
        self.clock+=6*3600+1
        self.assertEqual(self.store.context('thread-a'),{})

    def test_new_question_clears_prior_selection(self):
        self.run_job('查文献 城市演变');self.run_job('新问题')
        self.assertEqual(self.store.context('chat'),{})

    def test_duplicate_message_cannot_change_content_or_generate_again(self):
        job=self.job('查文献 城市演变')
        self.store.enqueue(job['id'],job['message_id'],job['text'],job['proof'])
        with self.assertRaises(ContractError):self.store.enqueue(job['id'],job['message_id'],'替换问题',job['proof'])
        self.assertEqual(len(self.daily.rows('SELECT * FROM qa_jobs')),1)

    def test_missing_citation_rejects_entire_model_answer(self):
        for bad in ({'status':'grounded','claims':[],'gaps':[]},
                    {'status':'grounded','claims':[{'text':'凭空结论','evidence':[]}],'gaps':[]}):
            with self.assertRaises(ContractError):validate(bad,[])

    def test_answer_citation_numbers_match_search_material_numbers(self):
        self.index.sync(self.provider);hits=self.index.search('城市演变')
        last=hits[-1]
        answer=dict(status='grounded',claims=[dict(text='合成结论',evidence=[dict(evidence_id=last['evidence_id'],quote=last['text'])])],gaps=[])
        rendered=render_answer(answer,hits)
        number=list(dict.fromkeys(h['material_id'] for h in hits)).index(last['material_id'])+1
        self.assertIn('合成结论 ['+str(number)+']',rendered)

    def test_thread_seed_uses_only_parent_evidence_and_question(self):
        result=self.run_job('查文献 城市演变')
        self.store.delivered(result['id'],dict(message_id='om_answer'))
        self.store.seed_thread('thread-a','om_answer')
        self.assertEqual(self.store.context('thread-a')['question'],'城市演变')
        self.assertNotIn('answer',self.store.context('thread-a'))
        self.store.seed_thread('thread-b','om_foreign')
        self.assertEqual(self.store.context('thread-b'),{})


    def test_comparison_cannot_claim_success_after_citing_only_one_selection(self):
        self.run_job('查文献 城市演变')
        job=self.run_job('比较 1,2 方法')
        self.assertEqual(job['result']['status'],'partial')
        self.assertIn('尚未完成所选材料的比较',job['result']['text'])
        request=json.loads(self.wire.calls[0]['request'])
        self.assertEqual(len(request['selected_materials']),2)
        self.assertTrue(all('material_id' in e for e in request['evidence']))

    def test_comparison_with_each_selected_source_keeps_grounded_status(self):
        def both(value):
            evidence=json.loads(self.wire.calls[-1]['request'])['evidence']
            other=next(e for e in evidence if e['material_id']!=evidence[0]['material_id'])
            value['claims'].append(dict(text='另一份材料强调访谈。',evidence=[dict(evidence_id=other['evidence_id'],quote=other['text'])]))
            return value
        self.wire.transform=both
        self.run_job('查文献 城市演变')
        job=self.run_job('比较 1,2 方法')
        self.assertEqual(job['result']['status'],'grounded')

    def test_followup_can_focus_on_one_previously_selected_source(self):
        self.run_job('查文献 城市演变');self.run_job('比较 1,2 方法')
        job=self.run_job('追问 第一份材料的方法是什么')
        self.assertEqual(job['result']['status'],'grounded')

    def test_short_answer_keeps_whole_claims_and_expands_original_evidence(self):
        self.index.sync(self.provider);hits=self.index.search('城市演变');hit=hits[0]
        output=dict(status='grounded',claims=[dict(text=('第'+str(i)+'条完整结论。')*20,
            evidence=[dict(evidence_id=hit['evidence_id'],quote=hit['text'])]*2) for i in range(6)],gaps=[])
        short=render_answer(output,hits);full=render_evidence(output,hits)
        self.assertIn(output['claims'][0]['text'],short)
        self.assertNotIn(output['claims'][2]['text'],short)
        self.assertIn(output['claims'][5]['text'],full)
        self.assertLess(len(short),len(full))
        self.assertNotIn('[1,1]',short);self.assertNotIn('[1,1]',full)

    def test_saved_answer_can_be_reformatted_without_new_model_call(self):
        job=self.run_job('综述 城市演变')
        self.assertIsNotNone(self.model.saved(job))
        self.assertEqual(len(self.wire.calls),1)
        receipt=self.root/'qa-jobs'/job['id']/'response.local.json'
        receipt.write_text('{}')
        with self.assertRaisesRegex(ContractError,'回执变化'):self.model.saved(job)
