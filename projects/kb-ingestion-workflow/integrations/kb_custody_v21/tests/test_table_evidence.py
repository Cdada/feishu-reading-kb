"""Synthetic table fidelity, conservative continuation and bounded QA tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21 import table_evidence as tables
from kb_custody_v21.qa_index import QAIndex, chunks
from kb_custody_v21.qa_model import request, render_evidence
from kb_custody_v21.qa_query import citation, render_search
from kb_custody_v21.qa_sources import QASources
from kb_custody_v21.reading_bundle import build_bundle
from kb_custody_v21.reading_contract import request as reading_request, validate_draft
from kb_custody_v21.qa_answer_contract import validate
from test_qa_index import source, Provider
from test_reading_content import sample, assets, reviewed
import test_qa_engine


def table(value='−12.50', header='面积（km²）'):
    return '<table><tr><th>区域</th><th>'+header+'</th></tr><tr><td>合成甲区</td><td>'+value+'</td></tr></table>'


def two_pages(first, second, **kwargs):
    text = first+'\n\n'+second
    pages = [dict(page=7, char_start=0, char_end=len(first)),
             dict(page=8, char_start=len(first)+2, char_end=len(text))]
    return text, pages, tables.inventory(text, pages, **kwargs)


class TableInventoryTests(unittest.TestCase):
    def test_merged_cells_empty_zero_minus_and_entity_keep_exact_coordinates(self):
        raw = ('<table><tr><th rowspan="2">区😀域</th><th colspan="2">指标</th></tr>'
               '<tr><th>km<sup>2</sup></th><th>比例（%）</th></tr>'
               '<tr><td>A &amp; B</td><td>−12.50</td><td>0</td></tr>'
               '<tr><td>乙</td><td></td><td>—</td></tr></table>')
        text = '合成前言😀\n\n表 1 指标\n\n单位：km²、%\n\n'+raw
        result = tables.inventory(text)['tables'][0]
        self.assertEqual((result['rows'], result['columns']), (4, 3))
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['header_rows'], [1, 2])
        self.assertEqual(result['cells'][2]['column'], 2)
        self.assertEqual([c['text'] for c in result['cells']][-6:], ['A & B', '−12.50', '0', '乙', '', '—'])
        for cell in result['cells']:
            raw_cell = text[cell['char_start']:cell['char_end']]
            self.assertTrue(raw_cell.startswith('<'+cell['tag']))
            self.assertTrue(raw_cell.endswith('</'+cell['tag']+'>'))
        self.assertEqual(text[result['char_start']:result['char_end']], raw)
        self.assertEqual(result['content_verification'], 'not_checked_against_pdf')

    def test_code_examples_are_not_tables_and_multiple_tables_keep_positions(self):
        text = '```html\n'+table()+'\n```\n\n'+table()+'\n\n'+table('0')
        found = tables.inventory(text)['tables']
        self.assertEqual(len(found), 2)
        self.assertTrue(all(text[t['char_start']:t['char_end']].endswith('</table>') for t in found))
        self.assertEqual(found[0]['id'], 'T001')

    def test_blank_lines_closing_tag_whitespace_and_comments_preserve_exact_range(self):
        raw = table().replace('</tr>', '</tr>\n\n').replace('</td>', '</td >').replace('</table>', '</table >')
        text = '<!-- '+table()+' -->\n\n'+raw+'\n\n'+table('0')
        found = tables.inventory(text)['tables']
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]['issues'], [])
        self.assertEqual(text[found[0]['char_start']:found[0]['char_end']], raw)
        self.assertEqual(text[found[0]['cells'][2]['char_start']:found[0]['cells'][2]['char_end']], '<td>合成甲区</td >')

    def test_malformed_and_bounded_tables_remain_preserved_but_not_answerable(self):
        cases = [('<table><tr><td>1</td></tr>', 'unclosed_table'),
                 ('<table><tr><td rowspan="0">1</td></tr></table>', 'invalid_span'),
                 ('<table><tr><td rowspan="3">1</td></tr></table>', 'uneven_rows'),
                 ('<table><tr><td>1</td><td>2</td></tr><tr><td>3</td></tr></table>', 'uneven_rows'),
                 ('<table><tr><td colspan="9999">1</td></tr></table>', 'invalid_span'),
                 ('<table><tr><td><table><tr><td>1</td></tr></table></td></tr></table>', 'nested_table'),
                 ('<table><tr><td>1</tr></table>', 'invalid_structure'),
                 ('<table><tr><td>1</td><td rowspan="2">2</td></tr><tr><td colspan="2">3</td></tr></table>', 'overlap')]
        for raw, issue in cases:
            with self.subTest(issue=issue):
                result = tables.inventory(raw)['tables'][0]
                self.assertIn(issue, result['issues'])
                self.assertFalse(result['qa_usable'])
                self.assertEqual(raw[result['char_start']:result['char_end']], raw)

    def test_only_explicit_continuation_with_identical_headers_units_and_adjacent_pages_links(self):
        first = '表 2 测量结果\n\n单位：km²\n\n'+table()
        second = '表 2 测量结果（续）\n\n单位：km²\n\n'+table('0')
        text, pages, record = two_pages(first, second)
        self.assertEqual(record['tables'][1]['continuation_of'], 'T001')
        for changed in (second.replace('（续）', ''), second.replace('km²', 'm²'),
                        second.replace('面积', '人口'), second.replace('表 2', '表 3'),
                        second.replace('<th>', '<td>').replace('</th>', '</td>')):
            self.assertIsNone(two_pages(first, changed)[2]['tables'][1]['continuation_of'])
        pages[1]['page'] = 9
        self.assertIsNone(tables.inventory(text, pages)['tables'][1]['continuation_of'])
        self.assertIsNone(tables.inventory(text)['tables'][1]['continuation_of'])
        # Unit changes in separate notes also block a continuation.
        changed = second.replace('单位：km²', '单位：m²')
        self.assertIsNone(two_pages(first, changed)[2]['tables'][1]['continuation_of'])

    def test_continuation_never_casefolds_units_or_erases_unit_word_boundaries(self):
        for first_unit, second_unit in [('mW', 'MW'), ('m s', 'ms')]:
            first = '表 2 测量结果\n\n单位：'+first_unit+'\n\n'+table()
            second = '表 2 测量结果（续）\n\n单位：'+second_unit+'\n\n'+table()
            self.assertIsNone(two_pages(first, second)[2]['tables'][1]['continuation_of'])

    def test_caption_does_not_cross_pages_or_steal_an_unrelated_paragraph(self):
        text, _, record = two_pages('表 1 前页标题', table())
        self.assertEqual(record['tables'][0]['caption'], '')
        raw = '表 1 上文\n\n这是一段无关的正文。\n\n'+table()
        self.assertEqual(tables.inventory(raw)['tables'][0]['caption'], '')

    def test_table_overlapping_excluded_furniture_is_not_answer_evidence(self):
        text = table()
        excluded = [dict(char_start=10, char_end=20)]
        self.assertFalse(tables.inventory(text, excluded_ranges=excluded)['tables'][0]['qa_usable'])
        self.assertEqual(list(chunks(source(text=text, excluded_ranges=excluded))), [])


class TableArchiveAndQATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.index = QAIndex(Path(self.tmp.name).resolve()/'qa.sqlite3')

    def test_archive_adds_inventory_and_navigation_without_changing_original(self):
        s, draft = sample()
        s['original'] += '\n表 1 合成测量\n\n'+table()
        draft['claims'][0]['evidence'] = [dict(source='original', quote='<td>合成甲区</td><td>−12.50</td>')]
        bundle = build_bundle(s, draft, reviewed(s, draft), assets=assets(s), date='2026-10-28', existing_files={})
        group, files = bundle['group'], bundle['files']
        self.assertEqual(files[group+'/原文.md'], s['original'].encode())
        record = json.loads(files[group+'/入库依据/提取/tables.json'])
        self.assertEqual(record['source_sha256'], hashlib.sha256(s['original'].encode()).hexdigest())
        self.assertIn('表格目录', files[group+'/来源卡片.md'].decode())
        self.assertIn('尚未对照 PDF', files[group+'/表格目录.md'].decode())
        self.assertIn('&lt;td&gt;', files[group+'/文章客观整理.md'].decode())

    def test_whole_table_survives_the_old_900_character_boundary_and_exact_reverification(self):
        raw = table('12.5')[:-8] + ''.join('<tr><td>合成区域'+str(n)+'</td><td>0</td></tr>' for n in range(40))+'</table>'
        text = '前言。\n\n表 1 城市测量\n\n'+raw+'\n\n后文。'
        s = source(text=text, title='合成 PDF', keywords=[], pages=[dict(page=3, char_start=0, char_end=len(text))])
        self.index.sync(Provider(s))
        hit = self.index.search('合成区域39')[0]
        self.assertIn('<th>面积（km²）</th>', hit['text'])
        self.assertIn('<td>合成区域39</td>', hit['text'])
        self.assertTrue(hit['text'].endswith('</table>'))
        self.assertGreater(len(hit['text']), 900)
        self.assertIn('T001', citation(hit, 1))
        self.assertIn('PDF 物理页 3', citation(hit, 1))
        authority = QASources.__new__(QASources)
        authority.get = lambda _: s
        authority.verify([hit])
        bad = deepcopy(hit); bad['table_context'][0]['qa_usable'] = False
        with self.assertRaises(ContractError): authority.verify([bad])
        bad = deepcopy(hit); bad.pop('table_context')
        with self.assertRaises(ContractError): authority.verify([bad])
        packet = request(dict(query=dict(question='面积是多少', mode='answer'), hits=[hit]))
        self.assertEqual(packet['evidence'][0]['table_context'][0]['physical_pages'], [3])
        output = dict(status='grounded', claims=[dict(text='合成甲区为 12.5 km²（待核对原页）。',
                       evidence=[dict(evidence_id=hit['evidence_id'], quote='<td>合成甲区</td><td>12.5</td>')])], gaps=[])
        self.assertIn('尚未与 PDF 原页', render_evidence(output, [hit]))

    def test_oversize_values_remain_searchable_but_no_partial_rows_go_to_the_model(self):
        raw = table()[:-8]+''.join('<tr><td>合成条目'+str(i)+'</td><td>17.25</td></tr>' for i in range(500))+'</table>'
        self.index.sync(Provider(source(text=raw, title='大表', keywords=[])))
        hit = self.index.search('合成条目499')[0]
        self.assertEqual(hit['text'], '<table>')
        self.assertFalse(hit['table_context'][0]['qa_usable'])
        self.assertIn('表格过长', render_search([hit]))
        with self.assertRaises(ContractError): request(dict(hits=[hit]))
        with self.assertRaises(ContractError):
            validate(dict(status='grounded', claims=[dict(text='不能引用', evidence=[dict(evidence_id=hit['evidence_id'], quote='<table>')])], gaps=[]), [hit])

    def test_rectangular_table_without_explicit_headers_requires_original_review(self):
        raw = table().replace('<th>', '<td>').replace('</th>', '</td>')
        record = tables.inventory(raw)['tables'][0]
        self.assertEqual(record['structure_status'], 'consistent')
        self.assertEqual(record['header_status'], 'not_marked')
        self.assertFalse(record['qa_usable'])
        self.assertNotIn('表格过长', tables.notice([record]))
        s, draft = sample()
        s['original'] += '\n\n'+raw
        draft['claims'][0]['evidence'] = [dict(source='original', quote='<td>合成甲区</td><td>−12.50</td>')]
        with self.assertRaisesRegex(ContractError, 'unresolved structure or header'):
            validate_draft(draft, s)
        self.index.sync(Provider(source(text=raw)))
        hit = self.index.search('合成甲区')[0]
        with self.assertRaises(ContractError): request(dict(hits=[hit]))

    def test_reading_models_receive_table_limits_and_invalid_cells_cannot_be_claims(self):
        s, draft = sample()
        s['original'] += '\n\n<table><tr><th>城市测量</th><th>面积</th></tr><tr><td>合成甲区</td></tr></table>'
        rules = {k: 'synthetic' for k in ('archive_rules', 'organize_prompt', 'nlp_skill')}
        for purpose in ('organize', 'review'):
            packet = reading_request(purpose, s, rules, draft if purpose == 'review' else None)
            self.assertEqual(packet['table_analysis'][0]['structure_status'], 'needs_review')
            self.assertIn('空白、破折号和零不等价', packet['instruction'])
        draft['claims'][0]['evidence'] = [dict(source='original', quote='<td>合成甲区</td>')]
        with self.assertRaisesRegex(ContractError, 'unresolved structure'):
            validate_draft(draft, s)

    def test_old_index_records_rebuild_once_without_modifying_the_original(self):
        p = Provider(source(text=table()))
        self.index.sync(p)
        with self.index.connection() as db:
            db.execute("UPDATE qa_sources SET digest='old-pre-table-index'")
        self.assertEqual(self.index.sync(p)['changed'], 1)
        self.assertEqual(self.index.sync(p)['changed'], 0)
        self.assertEqual(p.get('one')['text'], table())

    def test_invalid_table_returns_navigation_without_spending_a_model_turn(self):
        f = test_qa_engine.QAEngineTests(); f.setUp(); self.addCleanup(f.doCleanups)
        bad = '<table><tr><th>城市测量</th><th>面积</th></tr><tr><td>99</td></tr></table>'
        f.provider.values = {'one': source(text=bad, title='合成测量', keywords=[])}
        result = f.run_job('综述 城市测量')
        self.assertEqual(result['result']['status'], 'table_review_required')
        self.assertEqual(f.wire.calls, [])
        self.assertEqual(f.daily.summary()['model_turns'], 0)


if __name__ == '__main__':
    unittest.main()
