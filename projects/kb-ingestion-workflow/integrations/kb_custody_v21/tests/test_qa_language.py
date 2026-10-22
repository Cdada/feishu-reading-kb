"""Synthetic regressions for aliases and explicit time meanings."""
from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from kb_custody_v21.contracts import ContractError, stable_key
from kb_custody_v21.daily_cards import CardStore, render_card, commands
from kb_custody_v21.qa_language import variants, declared_aliases
from kb_custody_v21.qa_query import parse
from kb_custody_v21.qa_time import CHOICES
import test_qa_index as index_fixture
import test_qa_engine as engine_fixture


class RetrievalLanguageTests(unittest.TestCase):
    def setUp(self):
        self.f=index_fixture.QAIndexTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.index=self.f.index

    def test_common_abbreviation_retrieves_original_chinese_name(self):
        body='地理信息系统可辅助分析空间关系。'
        self.index.sync(index_fixture.Provider(index_fixture.source(text=body,title='空间分析',keywords=[])))
        self.assertEqual(self.index.search('GIS',expand=False),[])
        hits=self.index.search('GIS')
        self.assertEqual(hits[0]['text'],body)
        self.assertIn('地理信息系统',hits[0]['query_expansions'])

    def test_source_declared_alias_retrieves_but_related_terms_are_not_synonyms(self):
        body='“空间单元观察规程”（以下简称“观察规程”）。\n\n空间单元观察规程适用于合成案例。'
        self.index.sync(index_fixture.Provider(index_fixture.source(text=body,title='合成方法',keywords=[])))
        self.assertTrue(self.index.search('观察规程'))
        self.assertEqual(declared_aliases('空间单元方法与统计方法密切相关。'),[])
        self.assertEqual(variants('片区规划'),['片区规划'])

    def test_latin_word_boundaries_and_bounded_expansion(self):
        self.assertEqual(variants('RAIL'),['RAIL'])
        self.assertIn('人工智能',variants('ai'))
        self.assertLessEqual(len(variants('GIS AI NLP LLM OSM OCR')),8)

    def test_three_year_meanings_are_distinct(self):
        self.index.sync(index_fixture.Provider(index_fixture.source(
            text='城市演变的记录提及 1992 年的观察。\n\n城市演变的说明未指明年份。',publication_year=2020)))
        self.assertTrue(self.index.search('城市演变',year_from=2020,year_to=2020,time_scope='publication'))
        self.assertFalse(self.index.search('城市演变',year_from=1992,year_to=1992,time_scope='publication'))
        hits=self.index.search('城市演变',year_from=1992,year_to=1992,time_scope='content')
        self.assertEqual(len(hits),1);self.assertIn('1992',hits[0]['text'])
        self.assertTrue(self.index.search('城市演变',year_from=2026,year_to=2026,time_scope='ingestion'))
        self.assertFalse(self.index.search('城市演变',year_from=2020,year_to=2020,time_scope='ingestion'))

    def test_maximum_year_and_invalid_time_meaning(self):
        self.index.sync(index_fixture.Provider(index_fixture.source()))
        self.assertTrue(self.index.search('城市演变',year_from=1000,year_to=9999,time_scope='ingestion'))
        with self.assertRaises(ContractError):self.index.search('城市演变',time_scope='invented')


class QueryTimeTests(unittest.TestCase):
    now=datetime(2026,10,22,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    def test_bare_year_asks_before_answer(self):
        q=parse('查文献 城市演变 2020年',now=self.now)
        self.assertEqual(q['mode'],'clarify');self.assertEqual(q['original_mode'],'search')
        self.assertEqual(q['year_from'],2020)
        for command,scope in CHOICES.items():
            selected=parse(command,dict(pending_time=q),now=self.now)
            self.assertEqual(selected['mode'],'search');self.assertEqual(selected['time_scope'],scope)

    def test_relative_and_decade_ranges_use_question_time(self):
        for term,years in [('去年',(2025,2025)),('近三年',(2024,2026)),('20世纪90年代',(1990,1999))]:
            q=parse('查文献 城市演变 '+term,now=self.now)
            self.assertEqual((q['year_from'],q['year_to']),years)
            self.assertEqual(q['mode'],'clarify')

    def test_explicit_meaning_and_followup_preserved(self):
        for prefix,scope in [('发表','publication'),('入库','ingestion'),('内容','content')]:
            q=parse('查文献 城市演变 '+prefix+':2020-2025',now=self.now)
            self.assertEqual(q['mode'],'search');self.assertEqual(q['time_scope'],scope)
            follow=parse('追问 方法',dict(q),now=self.now)
            self.assertEqual(follow['time_scope'],scope);self.assertEqual(follow['year_to'],2025)

    def test_year_in_exact_quote_is_not_removed(self):
        q=parse('查出处 “2020年的空间观察记录”',now=self.now)
        self.assertEqual(q['mode'],'source');self.assertIn('2020年',q['question'])
        self.assertIsNone(q['year_from'])

    def test_latest_status_still_requires_time_clarification(self):
        q=parse('查文献 2020年最新的城市演变规定',now=self.now)
        self.assertEqual(q['mode'],'clarify');self.assertEqual(q['original_mode'],'version')

    def test_missing_or_ambiguous_time_context_rejected(self):
        for text in ('按发表时间','查文献 城市演变 2020年 2025年','查文献 发表:2020 入库:2025'):
            with self.assertRaises(ContractError):parse(text,now=self.now)


class TimeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f=engine_fixture.QAEngineTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_clarification_and_search_do_not_use_model(self):
        f=self.f
        parent=f.run_job('查文献 城市演变 2020年')
        self.assertEqual(parent['result']['menu'],'time');self.assertEqual(f.wire.calls,[])
        result=f.run_job('按发表时间')
        self.assertEqual(result['result']['status'],'retrieval');self.assertTrue(result['result']['hits'])
        self.assertEqual(f.daily.summary()['model_turns'],0)

    def test_crash_after_plan_recovers_clarification_without_search(self):
        f=self.f;job=f.job('综述 城市演变 2020年')
        q=parse(job['text'],now=job['created'])
        f.store.prepare(job['id'],dict(query=q,hits=[]))
        f.engine.run(f.store.get(job['id']))
        self.assertEqual(f.store.get(job['id'])['result']['status'],'clarification')
        self.assertEqual(f.wire.calls,[])

    def test_card_uses_its_own_parent_even_after_another_question(self):
        f=self.f;parent=f.run_job('查文献 城市演变 2020年')
        f.store.delivered(parent['id'],dict(message_id='om_clarify'))
        f.run_job('查文献 城市演变 1990年')
        child=f.store.enqueue(stable_key('time-card','one'),'om_clarify','按发表时间',
            dict(receipts={},qa_time_choice=dict(job_id=parent['id'],command='按发表时间')))
        f.engine.run(child)
        self.assertEqual(f.store.get(child['id'])['plan']['query']['year_from'],2020)
        self.assertTrue(f.store.get(child['id'])['result']['hits'])

    def test_expired_card_cannot_spend_model(self):
        f=self.f;parent=f.run_job('综述 城市演变 2020年')
        f.store.delivered(parent['id'],dict(message_id='om_clarify'));f.clock+=21601
        child=f.store.enqueue(stable_key('time-card','expired'),'om_clarify','按发表时间',
            dict(receipts={},qa_time_choice=dict(job_id=parent['id'],command='按发表时间')))
        with self.assertRaises(ContractError):f.engine.run(child)
        self.assertEqual(f.wire.calls,[])

    def test_clarification_thread_and_fixed_buttons(self):
        f=self.f;parent=f.run_job('查文献 城市演变 2020年')
        f.store.delivered(parent['id'],dict(message_id='om_clarify'))
        f.store.seed_thread('thread-time','om_clarify')
        self.assertEqual(f.store.context('thread-time')['pending_time']['mode'],'clarify')
        result=f.run_job('按发表时间',conversation='thread-time')
        self.assertTrue(result['result']['hits'])
        binding=dict(purpose='qa',document_id='om_question',source_sha256='a'*64,
                     target_message_id='om_clarify',job_id=parent['id'],details='合成时间选择',menu='time')
        card=CardStore(f.daily).prepare(binding,'选择时间口径')
        self.assertEqual(tuple(commands(binding)),tuple(CHOICES))
        self.assertIn('按入库时间',str(render_card(card)))
        with self.assertRaises(ContractError):CardStore(f.daily).prepare(dict(binding,menu='custom'),'异常选项')
