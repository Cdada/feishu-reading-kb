"""Synthetic retrieval and boundary regressions; no personal question corpus."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_index import QAIndex, chunks, tokens
from kb_custody_v21.qa_query import parse, render_search, render_weekly


def source(key='one', text='# 方法\n\n城市演变研究使用证据地图。\n\n规划指标需要结合地方情境。', **values):
    return dict(dict(material_id=key, document_id='om_'+key, title='城市演变的证据方法', text=text,
        body_sha256=hashlib.sha256(text.encode()).hexdigest(), source_sha256='a'*64,
        binding=dict(source_id=key), keywords=['证据地图'], excluded_ranges=[], is_paper=True,
        publication_year=2020, released_at=None, ingested_at=1767268800,
        brief_visibility='include', completeness='full_text', content_nature='nonfiction',
        source_path=key+'/source.md', evidence={}), **values)


class Provider:
    def __init__(self, *sources):
        self.values = {s['material_id']: s for s in sources}
    def ids(self): return list(self.values)
    def get(self, key): return deepcopy(self.values[key])


class QAIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.index = QAIndex(Path(self.tmp.name).resolve()/'index.sqlite3')

    def test_two_character_chinese_and_latin_tokens(self):
        self.assertEqual(tokens('更新 GIS'), ['更新', 'gis'])
        self.assertEqual(tokens('城市更新'), ['城市', '市更', '更新'])

    def test_original_offsets_and_source_diversity(self):
        a, b = source(), source('two', text='城市演变采用访谈，证据地图不能代替访谈。')
        self.index.sync(Provider(a, b))
        hits = self.index.search('城市演变', limit=3)
        self.assertEqual({h['material_id'] for h in hits[:2]}, {'one', 'two'})
        for h in hits:
            original = a if h['material_id']=='one' else b
            self.assertEqual(original['text'][h['char_start']:h['char_end']], h['text'])
            self.assertNotIn('page', h)

    def test_only_provider_completed_materials_enter_index(self):
        p = Provider(source())
        self.assertEqual(self.index.sync(p), dict(total=1, changed=1, removed=0))
        self.assertEqual(self.index.sync(p)['changed'], 0)
        p.values.clear()
        self.assertEqual(self.index.sync(p)['removed'], 1)
        self.assertEqual(self.index.search('城市演变'), [])

    def test_changed_version_invalidates_old_evidence(self):
        p = Provider(source())
        self.index.sync(p); old = self.index.search('城市演变')[0]['evidence_id']
        p.values['one'] = source(text='城市演变采用新测量边界。')
        self.index.sync(p)
        self.assertNotEqual(old, self.index.search('城市演变')[0]['evidence_id'])
        self.assertEqual(self.index.search('地方情境'), [])

    def test_repeated_headers_excluded_footnote_retained(self):
        text = '期刊页眉\n\n正文方法\n\n注：补充限制。'
        s = source(text=text, title='合成材料', keywords=[], excluded_ranges=[dict(char_start=0, char_end=4)])
        self.index.sync(Provider(s))
        self.assertEqual(self.index.search('期刊页眉'), [])
        self.assertTrue(self.index.search('补充限制'))
        for a, z, *_ in chunks(s): self.assertFalse(a < 4 and z > 0)

    def test_time_filter_excludes_unknown_publication_year(self):
        self.index.sync(Provider(source(), source('unknown', publication_year=None), source('old', publication_year=1990)))
        hits = self.index.search('城市演变', year_from=2010, year_to=2020)
        self.assertEqual({h['material_id'] for h in hits}, {'one'})

    def test_trusted_page_map_only_labels_overlapping_physical_pages(self):
        s = source(text='城市演变研究。\n\n访谈方法。', pages=[dict(page=7,char_start=0,char_end=7),dict(page=8,char_start=9,char_end=14)])
        self.index.sync(Provider(s))
        hits = self.index.search('城市演变')
        self.assertEqual(hits[0]['physical_pages'],[7])

    def test_sql_and_fts_operators_are_data(self):
        self.index.sync(Provider(source()))
        self.assertEqual(self.index.search('" OR DROP TABLE qa_sources; -- nonexistent'), [])
        self.assertEqual(len(self.index.materials()), 1)

    def test_comparison_restricts_sources(self):
        self.index.sync(Provider(source(), source('two'), source('three')))
        hits = self.index.search('城市演变', material_ids=['two','three'])
        self.assertEqual({h['material_id'] for h in hits}, {'two','three'})

    def test_unrelated_query_is_not_forced_to_match(self):
        self.index.sync(Provider(source()))
        self.assertEqual(self.index.search('天体轨道'), [])
        self.assertIn('未找到', render_search([]))

    def test_file_path_symlink_rejected(self):
        link = Path(self.tmp.name).resolve()/'link'; link.symlink_to(self.index.path)
        with self.assertRaises(ContractError): QAIndex(link)


class QAQueryTests(unittest.TestCase):
    def test_explicit_modes_and_defaults(self):
        self.assertEqual(parse('问库：查文献 城市演变')['mode'], 'search')
        self.assertEqual(parse('问库：如何理解城市演变')['mode'], 'answer')
        self.assertEqual(parse('问库：新问题')['mode'], 'reset')

    def test_comparison_numbers_and_context_are_bounded(self):
        result = parse('比较 2,1 适用条件', {'materials':['a','b','c']})
        self.assertEqual(result['selected'], ['b','a'])
        for text in ('比较 1,9 方法', '比较 1,1 方法', '比较 两篇'):
            with self.assertRaises(ContractError): parse(text, {'materials':['a','b']})

    def test_followup_keeps_object_time_and_not_previous_answer(self):
        c = dict(question='城市演变', selected=['a'], year_from=2010, year_to=2020,
                 answer='没有根据的旧回答')
        r = parse('追问 有什么限制', c)
        self.assertIn('城市演变', r['query']); self.assertEqual(r['year_to'], 2020)
        self.assertNotIn(c['answer'], str(r))
        with self.assertRaises(ContractError): parse('追问 为什么')

    def test_latest_requires_explicit_version_not_ingestion_order(self):
        self.assertEqual(parse('当前最新的城市演变规则')['mode'], 'version')

    def test_explicit_publication_year_range(self):
        r = parse('查文献 城市演变 发表:2001-2010')
        self.assertEqual((r['year_from'], r['year_to']), (2001, 2010))
        with self.assertRaises(ContractError): parse('查文献 方法 发表:2020-2010')

    def test_weekly_excludes_private_titles_and_counts(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026,1,2,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        public = source(ingested_at=now-60)
        private = source('private', title='隐私标题', brief_visibility='exclude', ingested_at=now-60)
        r = render_weekly([public,private], '本周周报', now)
        self.assertIn('1 份', r); self.assertNotIn('隐私标题', r)
        self.assertIn('不代表你已阅读', r)

    def test_weekly_bad_or_excessive_dates_do_not_default_silently(self):
        for text in ('周报 昨天', '周报 2026-02-30 至 2026-03-01', '周报 2020-01-01 至 2026-01-01'):
            with self.assertRaises(ContractError): render_weekly([], text, 1767355200)
