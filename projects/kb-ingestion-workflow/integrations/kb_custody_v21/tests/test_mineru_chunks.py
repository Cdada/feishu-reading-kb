"""Synthetic PDFs and local JSON only; never import or start real MinerU."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from pypdf import PdfWriter

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.image_links import rewrite_images
from kb_custody_v21.mineru_chunks import PdfChunks, PdfOptions, PdfHeld, ChunkFailure, file_digest, render_chunk
from kb_custody_v21.mineru_ingestion import checked_mineru_map
import test_conversion_v21 as conversion_fixture


def pdf_bytes(pages=7):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=300)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class SyntheticMineru:
    def __init__(self, fail=None, interrupt=None):
        self.calls, self.fail, self.interrupt = [], fail, interrupt

    def convert(self, pdf, output, *, mode):
        self.calls.append((pdf.stem, mode))
        if self.interrupt and self.interrupt(pdf, mode):
            raise Deferred('synthetic stop')
        if self.fail and self.fail(pdf, mode):
            raise ChunkFailure('synthetic conversion failure')
        first, last = [int(n) for n in pdf.stem[1:].split('-')]
        output.mkdir(parents=True)
        images = output / 'images'
        images.mkdir()
        items, pages = [], []
        for index in range(last-first+1):
            name = f'images/figure-{index}.png'
            Image.new('RGB', (5, 5), (20, 80, 100)).save(output / name)
            items.extend([dict(type='text', text_level=1, text=f'Paper page {first+index}', page_idx=index),
                dict(type='text', text='城市😀研究。Synthetic evidence preserves full page text and its original figure.', page_idx=index),
                dict(type='image', img_path=name, image_caption=['Synthetic figure caption'], page_idx=index)])
            pages.append(dict(page_idx=index, para_blocks=[{}]))
        (output / 'paper_content_list.json').write_text(json.dumps(items))
        (output / 'paper_middle.json').write_text(json.dumps(dict(_version_name='3.1.15', pdf_info=pages)))

    def maintain(self, **kwargs):
        pass

    def close(self):
        pass

    def check_previous(self):
        pass


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / 'synthetic.pdf'
        self.source.write_bytes(pdf_bytes())
        self.digest = file_digest(self.source)
        self.converter = SyntheticMineru()

    def run_pdf(self, converter=None):
        return PdfChunks(self.root / 'chunks', PdfOptions(), converter or self.converter).convert(self.source, self.digest)

    def test_physical_pages_and_same_named_images_survive_merge_and_restart(self):
        result = self.run_pdf()
        self.assertEqual(self.converter.calls, [('p0001-0005','auto'), ('p0006-0007','auto')])
        mapping = json.loads((result / 'source.pages.json').read_text())
        text = (result / 'source.md').read_text()
        checked_mineru_map(mapping, text, self.digest, result)
        self.assertEqual(len(mapping['images']), 7)
        self.assertEqual([p['page_index'] for p in mapping['page_mappings']], list(range(7)))
        for p in mapping['page_mappings']:
            r = p['text_ranges'][0]
            self.assertIn(f"Paper page {p['page_index']+1}", text[r['char_start']:r['char_end']])
        self.assertEqual(self.run_pdf(), result)
        self.assertEqual(len(self.converter.calls), 2)

    def test_failed_second_chunk_keeps_first_and_uses_only_one_ocr_remedy(self):
        converter = SyntheticMineru(fail=lambda pdf, mode: pdf.stem.startswith('p0006'))
        with self.assertRaises(PdfHeld):
            self.run_pdf(converter)
        self.assertEqual(converter.calls, [('p0001-0005','auto'), ('p0006-0007','auto'), ('p0006-0007','ocr')])
        with self.assertRaises(PdfHeld):
            self.run_pdf(converter)
        self.assertEqual(len(converter.calls), 3)
        self.assertFalse((self.root / 'chunks' / self.digest / 'complete').exists())
        manifest = json.loads((self.root / 'chunks' / self.digest / 'manifest.json').read_text())
        self.assertIsNotNone(manifest['chunks'][0]['completed'])

    def test_one_ocr_remedy_can_complete_without_repeating_successful_chunk(self):
        converter = SyntheticMineru(fail=lambda pdf, mode: pdf.stem.startswith('p0006') and mode == 'auto')
        result = self.run_pdf(converter)
        self.assertTrue((result / 'source.md').exists())
        self.assertEqual(converter.calls[-1], ('p0006-0007', 'ocr'))
        self.assertEqual(len(converter.calls), 3)

    def test_intentional_pause_does_not_spend_the_failure_remedy(self):
        converter = SyntheticMineru(interrupt=lambda pdf, mode:pdf.stem.startswith('p0006'))
        for _ in range(3):
            with self.assertRaises(Deferred): self.run_pdf(converter)
        converter.interrupt = None
        result = self.run_pdf(converter)
        self.assertTrue((result / 'source.md').exists())
        self.assertTrue(all(mode == 'auto' for _,mode in converter.calls))
        self.assertEqual(sum(stem=='p0001-0005' for stem,_ in converter.calls),1)

    def test_completed_output_after_lost_receipt_is_reused_before_any_rerun(self):
        converter = SyntheticMineru()
        original = converter.convert
        def interrupted(pdf, output, *, mode):
            original(pdf,output,mode=mode)
            if pdf.stem.startswith('p0006'): raise Deferred('saved result before process exit')
        converter.convert = interrupted
        with self.assertRaises(Deferred): self.run_pdf(converter)
        converter.convert = original
        result = self.run_pdf(converter)
        self.assertTrue((result / 'source.md').exists())
        self.assertEqual(len(converter.calls),2)

    def test_deleted_manifest_chunk_cannot_silently_omit_pages(self):
        self.run_pdf()
        path = self.root/'chunks'/self.digest/'manifest.json'
        value = json.loads(path.read_text()); value['chunks'].pop()
        path.write_text(json.dumps(value))
        with self.assertRaises(ContractError): self.run_pdf()

    def test_changed_completed_chunk_or_original_refuses_reuse(self):
        result = self.run_pdf()
        (result / 'source.md').write_text('changed')
        with self.assertRaises(ContractError):
            self.run_pdf()
        self.source.write_bytes(b'changed')
        with self.assertRaises(ContractError):
            self.run_pdf()
        self.assertEqual(len(self.converter.calls), 2)

    def test_missing_page_missing_image_and_path_escape_never_complete(self):
        for mutation in ('page', 'image', 'escape'):
            with self.subTest(mutation=mutation):
                root = self.root / mutation
                parent = SyntheticMineru()
                class Broken:
                    def convert(_, pdf, output, *, mode):
                        parent.convert(pdf, output, mode=mode)
                        if mutation == 'page':
                            p = output / 'paper_middle.json'
                            value = json.loads(p.read_text()); value['pdf_info'].pop()
                            p.write_text(json.dumps(value))
                        elif mutation == 'image':
                            (output / 'images/figure-0.png').unlink()
                        else:
                            p = output / 'paper_content_list.json'
                            value = json.loads(p.read_text()); value[2]['img_path'] = '../outside.png'
                            p.write_text(json.dumps(value))
                with self.assertRaises(PdfHeld):
                    PdfChunks(root, PdfOptions(), Broken()).convert(self.source, self.digest)
                self.assertEqual(len(parent.calls), 2)

    def test_page_and_image_tampering_is_rejected_at_import(self):
        result = self.run_pdf()
        text = (result / 'source.md').read_text()
        mapping = json.loads((result / 'source.pages.json').read_text())
        for mutation in ('source', 'page', 'image'):
            value = copy.deepcopy(mapping)
            if mutation == 'source': value['source_sha256'] = '0'*64
            elif mutation == 'page': value['page_mappings'][1]['page_index'] = 999
            else: next(iter(value['images'].values()))['page_index'] = 6
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                checked_mineru_map(value, text, self.digest, result)

    def test_native_headers_and_footers_keep_text_on_their_physical_pages(self):
        raw = self.root/'native-margins'
        SyntheticMineru().convert(self.root/'p0006-0007.pdf',raw,mode='auto')
        items = []
        for index in range(2):
            items.extend([
                dict(type='header',text=f'Journal heading {index}',page_idx=index),
                dict(type='text',text=f'Body {index}',page_idx=index),
                dict(type='page_footnote',text=f'Footnote {index}',page_idx=index),
                dict(type='page_number',text=f'42:{index+1}',page_idx=index),
                dict(type='footer',text=f'Publication details {index}',page_idx=index),
            ])
        (raw/'paper_content_list.json').write_text(json.dumps(items))
        result = render_chunk(raw,self.root/'native-normalized',first_page=6,page_count=2,
                              stem='p0006-0007',version='3.1.15')
        self.assertEqual(result['first_page'],6)
        self.assertEqual(result['pages'],[
            f'Journal heading {i}\n\nBody {i}\n\nFootnote {i}\n\n42:{i+1}\n\nPublication details {i}'
            for i in range(2)])

    def test_native_margin_aliases_do_not_relax_text_page_or_unknown_type_checks(self):
        raw = self.root/'invalid-margins'
        SyntheticMineru().convert(self.root/'p0001-0001.pdf',raw,mode='auto')
        cases = [
            (dict(type='header',text=123,page_idx=0),'文字字段'),
            (dict(type='footer',page_idx=0),'文字字段'),
            (dict(type='header',text='heading',page_idx=1),'页码越界'),
            (dict(type='unrecognized_layout',text='retained raw only',page_idx=0),'不支持的识别块'),
        ]
        for number,(item,message) in enumerate(cases):
            (raw/'paper_content_list.json').write_text(json.dumps([item]))
            with self.subTest(item=item),self.assertRaisesRegex(ChunkFailure,message):
                render_chunk(raw,self.root/f'invalid-normalized-{number}',first_page=1,
                             page_count=1,stem='p0001-0001',version='3.1.15')

    def test_formula_table_cell_image_and_code_caption_survive(self):
        raw = self.root/'raw'
        SyntheticMineru().convert(self.root/'p0001-0001.pdf',raw,mode='auto')
        items = [dict(type='equation',text='E=mc^2',img_path='images/',page_idx=0),
            dict(type='table',page_idx=0,table_caption=['Table caption'],
                 table_body='<table><tr><td rowspan="2">A &amp; B<img src="images/figure-0.png"></td></tr></table>'),
            dict(type='code',page_idx=0,code_body='print(1)',code_caption=['Code caption'])]
        (raw/'paper_content_list.json').write_text(json.dumps(items))
        result = render_chunk(raw,self.root/'normalized',first_page=6,page_count=1,stem='p0006-0006',version='3.1.15')
        text = result['pages'][0]
        for expected in ('E=mc^2','A &amp; B','rowspan="2"','Code caption','![](images/'):
            self.assertIn(expected,text)
        self.assertEqual(next(iter(result['images'].values()))['page_index'],5)

    def test_explicit_repair_reuses_successful_chunks_and_keeps_failures(self):
        self.converter.fail = lambda pdf,mode:pdf.stem.startswith('p0006')
        with self.assertRaises(PdfHeld): self.run_pdf()
        raw = self.root/'corrected'
        SyntheticMineru().convert(self.root/'p0006-0007.pdf',raw,mode='ocr')
        engine = PdfChunks(self.root/'chunks',PdfOptions(),self.converter)
        engine.import_repaired_chunk(self.source,self.digest,6,raw,operator_ref='synthetic reviewed repair')
        self.assertTrue((self.run_pdf()/'source.md').exists())
        self.assertEqual(len(self.converter.calls),3)
        manifest = json.loads((self.root/'chunks'/self.digest/'manifest.json').read_text())
        self.assertEqual([r['state'] for r in manifest['chunks'][1]['attempts']],['failed','failed','repaired'])


class MineruImportTests(unittest.TestCase):
    setUp = conversion_fixture.ConversionTests.setUp
    receive = conversion_fixture.ConversionTests.receive
    convert = conversion_fixture.ConversionTests.convert

    def test_real_conversion_import_and_image_rewrite_keep_page_ranges(self):
        entry = self.receive(pdf_bytes(), received_filename='synthetic-paper.pdf')
        row = self.registry.entry(entry)
        source = self.runtime.root / row['relative_path']
        converter = SyntheticMineru()
        result = PdfChunks(self.runtime.root / 'conversion-work/pdf-chunks', PdfOptions(), converter).convert(source, row['sha256'])
        self.conversion.service.custody_pdf_route = 'pdf_mineru_local'
        self.conversion.pdf_result = lambda _: result
        task, mapping = self.convert(entry)
        _, mapping, root = self.conversion.publication_extraction(mapping['extraction_id'])
        self.assertEqual(mapping['extractor'], 'mineru-pipeline')
        self.assertEqual(mapping['locator_precision'], 'page')
        images = self.conversion.registered_images(mapping['extraction_id'])
        self.assertEqual(sorted(i['source_page_index'] for i in images), list(range(7)))
        replacements = {i['original_target']:dict(asset_id=i['asset_id'],local_path='images/'+i['asset_id']+'.png',
            representation_asset_id=i['asset_id']) for i in images}
        content, revised = rewrite_images((root / 'source.md').read_bytes(), mapping, replacements)
        self.assertEqual(revised['locator_precision'], 'page')
        for page in revised['page_mappings']:
            r = page['text_ranges'][0]
            body = content.decode()[r['char_start']:r['char_end']]
            self.assertIn(f"Paper page {page['page_index']+1}", body)
        self.assertEqual([i['source_page_index'] for i in revised['image_mappings']], list(range(7)))


if __name__ == '__main__':
    unittest.main()
