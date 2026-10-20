"""Assert rendered formatting and passive PDF output, not just parser tokens."""
from io import BytesIO
from pathlib import Path
import os
import unittest
from pypdf import PdfReader
from kb_custody_v21.content import empty_record
from kb_custody_v21.preview import render_preview


class MarkdownPreviewTests(unittest.TestCase):
    def render(self, guide):
        pdf=render_preview(empty_record('Markdown 排版','synthetic'),guide,'仅合成材料。',
            font_path=os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc'))
        return PdfReader(BytesIO(pdf))

    def text(self, reader):
        return '\n'.join(page.extract_text() for page in reader.pages)

    def test_strong_is_painted_bold_and_following_text_returns_to_normal(self):
        reader=self.render('ordinary **BOLD** tail')
        mode, seen=0,[]
        for operands, operator in reader.pages[0].get_contents().operations:
            if operator==b'Tr': mode=int(operands[0])
            if operator==b'Tj': seen.append((str(operands[0]),mode))
        self.assertTrue(any('BOLD' in text and mode==2 for text,mode in seen),seen)
        self.assertTrue(any('tail' in text and mode==0 for text,mode in seen),seen)
        self.assertNotIn('**',self.text(reader))

    def test_headings_lists_quotes_and_literal_code_preserve_reading_order(self):
        reader=self.render('## 小标题\n\n- 条目 **加粗**\n- 第二项\n\n3. 有序甲\n4. 有序乙\n\n> 引用文字\n\n`**代码**`\n\n```text\n**block code**\n```')
        text=self.text(reader)
        for value in ['小标题','条目 加粗','第二项','3. 有序甲','4. 有序乙','引用文字','**代码**','**block code**']:
            self.assertIn(value,text)
        self.assertNotIn('##',text)
        self.assertNotIn('```',text)

    def test_links_images_and_html_remain_passive_text(self):
        reader=self.render('[链接](https://example.invalid/paper) ![图](file:///missing.png)\n\n<img src="https://example.invalid/a.png"/> <a href="https://example.invalid">原样</a>')
        text=self.text(reader)
        for value in ['https://example.invalid/paper','file:///missing.png','<img src=','<a href=']:
            self.assertIn(value,text)
        self.assertFalse(reader.trailer['/Root'].get('/OpenAction'))
        self.assertFalse(reader.trailer['/Root'].get('/Names'))
        self.assertTrue(all(not page.get('/Annots') for page in reader.pages))

    def test_escaped_markers_are_not_reinterpreted(self):
        reader=self.render(r'\*\*原样星号\*\* 与 **实际加粗**。')
        self.assertIn('**原样星号**',self.text(reader))
        self.assertNotIn('**实际加粗**',self.text(reader))

    def test_bold_continues_across_pages_without_losing_text(self):
        body='跨页加粗需要保留每个字，并继续正确分页。'*180
        reader=self.render('**'+body+'**\n\n普通尾句。')
        self.assertGreater(len(reader.pages),2)
        for page in reader.pages[:-1]:
            self.assertTrue(any(operator==b'Tr' and int(args[0])==2 for args,operator in page.get_contents().operations))
        compact=''.join(''.join(page.extract_text().splitlines()[1:]).replace(' ', '') for page in reader.pages)
        self.assertEqual(compact.count('跨页加粗需要保留每个字，并继续正确分页。'),180)
        self.assertIn('普通尾句。',compact)

    def test_simplified_prose_and_meaningful_traditional_text_round_trip(self):
        guide='作者将比较“將”“發”“髮”，并用简体说明。\n\n> 原文：“將軍的頭髮”。'
        reader=self.render(guide)
        text=self.text(reader)
        self.assertIn('作者将比较“將”“發”“髮”，并用简体说明。',text)
        self.assertIn('原文：“將軍的頭髮”。',text)

    @unittest.skipUnless(Path('/System/Library/Fonts/STHeiti Light.ttc').is_file(), 'macOS font collection required')
    def test_macos_collection_embeds_simplified_face_for_regular_and_bold(self):
        pdf=render_preview(empty_record('简体字体','synthetic'),'将与**將、發、髮**','合成样例。',
            font_path='/System/Library/Fonts/STHeiti Light.ttc')
        reader=PdfReader(BytesIO(pdf))
        fonts={str(font.get_object()['/BaseFont']) for page in reader.pages
            for font in page['/Resources']['/Font'].get_object().values()}
        cjk=[name for name in fonts if 'STHeiti' in name]
        self.assertTrue(cjk)
        self.assertTrue(all('STHeitiSC-Light' in name for name in cjk),fonts)
