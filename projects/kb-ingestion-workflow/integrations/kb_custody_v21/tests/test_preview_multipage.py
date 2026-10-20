"""Synthetic layout stress cases; these do not validate model writing quality."""
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.preview import render_preview
import test_publication_v21 as publication_tests


def complex_guide():
    # One paragraph is taller than a page; the others exercise natural breaks
    # alongside mixed Chinese, Latin text, punctuation and a long unbroken word.
    paragraph = '研究比较树荫覆盖、步行距离与道路连接情况，说明样本条件如何影响观察结果。每个判断都需要结合材料的证据范围使用。'
    return '\n\n'.join([
        '段落甲。' + paragraph * 26 + '段落甲结束。',
        '段落乙。Urban routes 2026：距离 120 m，样本比例 35%。' + paragraph * 8 + '段落乙结束。',
        '段落丙。' + 'SyntheticLongIdentifier' * 40 + '。' + paragraph * 8 + '段落丙结束。',
        '段落丁。这里比较条件 A < B 与 B > C，保留 & 符号。' + paragraph * 8 + '段落丁结束。',
    ])


class MultipagePreviewTests(unittest.TestCase):
    setUp = publication_tests.PublicationTests.setUp
    prepare = publication_tests.PublicationTests.prepare

    def test_long_paragraphs_page_numbers_and_exact_confirmed_bytes(self):
        self.guide = complex_guide()
        # This is an explicit synthetic layout fixture, not a new model result.
        self.curation['guide_generation']['guide_sha256'] = sha(self.guide.encode())
        prepared = self.prepare()
        pdf = Path(prepared['preview_path']).read_bytes()
        reader = PdfReader(BytesIO(pdf))
        self.assertGreaterEqual(len(reader.pages), 3)
        page_texts = [page.extract_text() for page in reader.pages]
        combined = ''.join(''.join(text.split()) for text in page_texts)
        cursor = 0
        for paragraph in self.guide.split('\n\n'):
            # Page numbers are deliberately separate from source content.
            text_without_footers = ''.join(''.join(text.splitlines()[1:]) for text in page_texts)
            compact = ''.join(text_without_footers.split())
            needle = ''.join(paragraph.split())
            found = compact.find(needle, cursor)
            self.assertGreaterEqual(found, cursor)
            cursor = found + len(needle)
        for number, (page, text) in enumerate(zip(reader.pages, page_texts), 1):
            self.assertEqual(text.splitlines()[0], str(number))
            self.assertNotIn(text.splitlines()[1], ['段落甲结束。', '段落乙结束。', '段落丙结束。', '段落丁结束。'])
            self.assertFalse(page.get('/Annots'))
        self.assertEqual(combined.count('当前材料与完整作品的覆盖关系尚需核对。'), 1)
        self.assertEqual(combined.count('请决定：确认入库/补充材料后再入库/暂不入库。'), 1)
        self.publisher.confirm(prepared['publication_id'], sha(pdf), 'synthetic-reviewer', 'synthetic-multipage-confirmation')
        with patch('kb_custody_v21.publication.render_preview', side_effect=AssertionError('must not rerender')):
            root = self.publisher.publish(prepared['publication_id'])
        self.assertEqual((root / '发布记录/已确认入库预览.pdf').read_bytes(), pdf)
        (self.runtime.base / 'test-output/preview-multipage-sample.local.json').write_text(json.dumps(
            dict(path=str(root / '发布记录/已确认入库预览.pdf'), pages=len(reader.pages))))

    def test_input_markup_is_literal_and_never_creates_pdf_actions(self):
        guide = '文字 <a href="https://example.invalid/synthetic">引用</a> 与 <img src="synthetic.png"/> 均为原样文字。'
        pdf = render_preview(self.record, guide, '合成排版检查。', font_path=self.font)
        reader = PdfReader(BytesIO(pdf))
        text = ''.join(page.extract_text() for page in reader.pages)
        self.assertIn('<a href=', text)
        self.assertIn('synthetic.png', text)
        self.assertFalse(reader.trailer['/Root'].get('/OpenAction'))
        self.assertFalse(reader.trailer['/Root'].get('/Names'))
        self.assertTrue(all(not page.get('/Annots') for page in reader.pages))

    def test_unsupported_glyph_fails_instead_of_silent_missing_character(self):
        with self.assertRaisesRegex(ContractError, 'every character'):
            render_preview(self.record, '合成缺字\U0010ffff', '合成排版检查。', font_path=self.font)
