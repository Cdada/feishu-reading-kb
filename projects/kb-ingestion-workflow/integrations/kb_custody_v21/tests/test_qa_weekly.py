"""Weekly privacy, time anchoring and one-call synthesis with synthetic data."""
from copy import deepcopy
import json
import unittest

from kb_custody_v21.contracts import ContractError, stable_key
from kb_custody_v21 import qa_weekly
from kb_custody_v21.qa_model import render_plan_answer, request
from kb_custody_v21.qa_query import parse
import test_qa_engine as fixture
from test_qa_index import source


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.QAEngineTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        for item in self.f.provider.values.values():item['ingested_at']=self.f.clock-60

    def test_local_weekly_groups_topics_without_model(self):
        f=self.f;job=f.run_job('本周周报')
        self.assertIn('本期主题',job['result']['text'])
        self.assertIn('2 份资料',job['result']['text'])
        self.assertEqual(job['result']['menu'],'weekly');self.assertEqual(f.wire.calls,[])

    def test_summary_uses_one_call_and_excludes_hidden_sources(self):
        f=self.f
        f.provider.values['hidden']=source('hidden',text='私密合成正文。',title='私密合成标题',
            keywords=['私密合成关键词'],ingested_at=f.clock-30,brief_visibility='exclude')
        job=f.run_job('周报解读')
        self.assertEqual(job['result']['status'],'grounded')
        self.assertEqual(f.daily.summary()['model_turns'],1)
        request=json.loads(f.wire.calls[0]['request'])
        self.assertEqual(request['intent'],'weekly_answer')
        self.assertNotIn('私密合成',json.dumps(job,ensure_ascii=False))
        self.assertNotIn('私密合成',json.dumps(request,ensure_ascii=False))
        self.assertIn('2 份',job['result']['text']);self.assertIn('原文',job['result']['text'])
        self.assertEqual(render_plan_answer(f.model.saved(job),job['plan']),job['result']['text'])

    def test_summary_passes_verified_ingestion_scope_without_private_metadata(self):
        f=self.f
        f.provider.values['private']=source('private',ingested_at=f.clock-30,brief_visibility='exclude')
        job=f.run_job('周报解读')
        request=json.loads(f.wire.calls[0]['request'])
        scope=request['weekly_scope']
        self.assertEqual(scope['basis'],'completed_ingestion')
        self.assertEqual({s['material_id'] for s in scope['sources']},{'one','two'})
        self.assertTrue(all(s['ingested_at']==f.clock-60 for s in scope['sources']))
        self.assertIn('+08:00',scope['start_inclusive'])
        self.assertEqual(scope['end_exclusive'],qa_weekly.weekly_window('本周周报',f.clock)[1].isoformat())
        self.assertNotIn('private',json.dumps(request))

    def test_body_evidence_beats_pages_and_headings_in_long_document(self):
        f=self.f
        body='城市演变需要证据地图，但地图不能代替访谈。'
        note='注：证据地图只覆盖样本区域。'
        text='\n\n'.join(['---\ntitle: 城市演变的证据地图\nsource: synthetic.example\n---',
            *(str(i) for i in range(200)), '# 城市演变的证据地图', '城市演变的证据地图 合成作者',body,note])
        f.provider.values={'one':source(text=text,ingested_at=f.clock-60)}
        f.index.sync(f.provider)
        snapshot=qa_weekly.plan(f.index.materials(),'本周周报',f.clock)
        hits=qa_weekly.evidence(snapshot,f.index)
        self.assertEqual({h['text'] for h in hits},{body,note})
        for h in hits:
            self.assertEqual(text[h['char_start']:h['char_end']],h['text'])

    def test_heading_only_week_does_not_call_model(self):
        f=self.f
        f.provider.values={'one':source(text='# 城市演变\n\n## 证据地图\n\n29',ingested_at=f.clock-60)}
        result=f.run_job('周报解读')['result']
        self.assertIn('未调用模型',result['text'])
        self.assertEqual(f.wire.calls,[])

    def test_no_materials_and_exhausted_quota_keep_local_report(self):
        f=self.f
        for item in f.provider.values.values():item['brief_visibility']='exclude'
        job=f.run_job('周报解读')
        self.assertIn('未调用模型',job['result']['text']);self.assertEqual(f.wire.calls,[])
        for item in f.provider.values.values():item['brief_visibility']='include'
        for key in 'abc':f.daily.reserve_model(key*64)
        job=f.run_job('周报解读')
        self.assertIn('本期主题',job['result']['text']);self.assertIn('额度已用完',job['result']['text'])

    def test_visibility_change_after_planning_blocks_model(self):
        f=self.f;f.index.sync(f.provider)
        snapshot=qa_weekly.plan(f.index.materials(),'本周周报',f.clock)
        hits=qa_weekly.evidence(snapshot,f.index)
        job=f.job('周报解读')
        plan=dict(query=dict(mode='weekly_answer',selected=[],question='本周周报',weekly=snapshot),hits=hits)
        f.store.prepare(job['id'],plan)
        f.provider.values['two']['brief_visibility']='exclude'
        with self.assertRaises(ContractError):f.engine.run(f.store.get(job['id']))
        self.assertEqual(f.wire.calls,[])

    def test_visibility_change_during_model_access_recheck_blocks(self):
        f=self.f;f.index.sync(f.provider)
        snapshot=qa_weekly.plan(f.index.materials(),'本周周报',f.clock)
        hits=qa_weekly.evidence(snapshot,f.index)
        job=f.job('周报解读')
        plan=dict(query=dict(mode='weekly_answer',selected=[],question='本周周报',weekly=snapshot),hits=hits)
        f.provider.values['one']['brief_visibility']='exclude'
        with self.assertRaises(ContractError):f.model.run(job,plan)
        self.assertEqual(f.wire.calls,[])

    def test_weekly_card_preserves_original_time_window(self):
        f=self.f;parent=f.run_job('本周周报')
        f.store.delivered(parent['id'],dict(message_id='om_weekly'))
        f.clock+=3600
        f.provider.values['later']=source('later',ingested_at=f.clock-20)
        child=f.store.enqueue(stable_key('weekly-card','one'),'om_weekly','周报解读',
            dict(receipts={},qa_weekly_parent=parent['id']))
        f.engine.run(child);saved=f.store.get(child['id'])
        self.assertEqual(saved['plan']['query']['weekly']['at'],parent['created'])
        self.assertNotIn('later',[m['material_id'] for m in saved['plan']['query']['weekly']['sources']])

    def test_report_preserves_previous_search_context(self):
        f=self.f;f.run_job('查文献 城市演变');context=deepcopy(f.store.context('chat'))
        f.run_job('本周周报');self.assertEqual(f.store.context('chat'),context)
        f.run_job('周报解读');self.assertEqual(f.store.context('chat'),context)

    def test_large_week_bounds_evidence_and_discloses_sample(self):
        f=self.f
        for n in range(18):f.provider.values[str(n)]=source(str(n),ingested_at=f.clock-60)
        f.index.sync(f.provider);snapshot=qa_weekly.plan(f.index.materials(),'本周周报',f.clock)
        hits=qa_weekly.evidence(snapshot,f.index)
        self.assertLessEqual(len(hits),12);self.assertEqual(len({h['material_id'] for h in hits}),8)
        self.assertIn('20 份',qa_weekly.scope(snapshot,hits))
        packet=request(dict(query=dict(mode='weekly_answer',question='本周周报',weekly=snapshot),hits=hits))
        self.assertEqual(packet['weekly_scope']['visible_source_count'],20)
        self.assertEqual(len(packet['weekly_scope']['sources']),8)

    def test_explicit_weekly_range_and_prior_keywords(self):
        q=parse('周报解读 2026-01-01 至 2026-01-07')
        self.assertEqual(q['question'],'周报 2026-01-01 至 2026-01-07')
        f=self.f;items=list(f.provider.values.values())
        prior=source('prior',ingested_at=f.clock-10*86400)
        summary=qa_weekly.plan([*items,prior],'本周周报',f.clock)
        self.assertNotIn('首次',summary['text'])
