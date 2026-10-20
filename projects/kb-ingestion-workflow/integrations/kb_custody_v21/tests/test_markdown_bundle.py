"""Synthetic Markdown/asset packages: real conversion, no OCR or network."""
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import warnings
import zipfile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from markdown_bundle import MarkdownBundleError, convert_markdown_bundle
from pack_markdown_bundle import pack_markdown
import test_conversion_v21 as conversion_tests
from local_document_conversion_mcp.service import ConversionService


def png():
    stream = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(stream, format='PNG')
    return stream.getvalue()


def bundle(items=None):
    if items is None:
        items = [('paper/paper.md', '# 合成论文\n\n合成正文用于核对本地资料包，包含图片和表格。\n\n![图 1](images/figure.png)\n'),
                 ('paper/images/figure.png', png()),
                 ('paper/original.pdf', b'%PDF-1.4\n% synthetic only\n'),
                 ('paper/content_list_v2.json', b'[]')]
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, raw in items:
                archive.writestr(name, raw)
    return stream.getvalue()


class MarkdownBundleTests(unittest.TestCase):
    def run_bundle(self, raw):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / 'article.zip'
        source.write_bytes(raw)
        output = root / 'output'
        return source, output

    def test_mineru_shaped_package_preserves_body_images_and_optional_sidecars(self):
        raw = bundle()
        source, output = self.run_bundle(raw)
        main = convert_markdown_bundle(source, output)
        self.assertEqual(main.name, 'paper.md')
        self.assertIn('![图 1](images/figure.png)', main.read_text())
        self.assertEqual((output/'images/figure.png').read_bytes(), png())
        self.assertEqual((output/'content_list_v2.json').read_bytes(), b'[]')
        self.assertTrue((output/'original.pdf').read_bytes().startswith(b'%PDF'))
        self.assertEqual(source.read_bytes(), raw)

    def test_reference_image_percent_encoded_filename_and_code_examples(self):
        body = '# 合成正文\n\n![中文][fig]\n\n[fig]: <images/图 1.png>\n\n```md\n![example](missing.png)\n```\n'
        source, output = self.run_bundle(bundle([('main.md', body), ('images/图 1.png', png())]))
        self.assertEqual(convert_markdown_bundle(source, output).read_text(), body)

    def test_missing_or_external_images_and_unsafe_local_links_refused_before_output(self):
        for target in ('images/missing.png', 'https://example.invalid/a.png', '../a.png',
                       '/a.png', 'file:///a.png', 'data:image/png;base64,abcd', 'images/a.png?v=1'):
            with self.subTest(target=target):
                source, output = self.run_bundle(bundle([('main.md', f'![figure]({target})')]))
                with self.assertRaises(MarkdownBundleError):
                    convert_markdown_bundle(source, output)
                self.assertFalse(output.exists())

    def test_regular_external_citation_preserved_without_fetch(self):
        text = '# Synthetic\n\n[Reference](https://example.invalid/paper)\n'
        source, output = self.run_bundle(bundle([('main.md', text)]))
        self.assertEqual(convert_markdown_bundle(source, output).read_text(), text)

    def test_ambiguous_or_non_markdown_packages_refused(self):
        for items in ([('a.md', '# A'), ('b.md', '# B')], [('images/a.png', png())],
                      [('a.md', '')], [('a.md', b'\xff\xfe')], [('a.md', '# A'), ('run.sh', 'no')]):
            with self.subTest(names=[str(n) for n, _ in items]):
                source, output = self.run_bundle(bundle(items))
                with self.assertRaises(MarkdownBundleError):
                    convert_markdown_bundle(source, output)
                self.assertFalse(output.exists())

    def test_unsafe_members_duplicates_and_symlinks_refused(self):
        symlink = zipfile.ZipInfo('images/link.png')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extra in ([('../outside.png', png())], [('images\\a.png', png())],
                      [('A.md', '# conflict')], [('a.md', '# duplicate')], [(symlink, b'/tmp/other')],
                      [('images/café.png', png()), ('images/cafe\u0301.png', png())]):
            with self.subTest(extra=str(extra[0][0])):
                source, output = self.run_bundle(bundle([('a.md', '# A'), *extra]))
                with self.assertRaises(MarkdownBundleError):
                    convert_markdown_bundle(source, output)
                self.assertFalse(output.exists())

    def test_mac_packaging_metadata_skipped_without_becoming_second_document(self):
        source, output = self.run_bundle(bundle([('main.md', '# Main'), ('__MACOSX/._main.md', b'junk'),
                                                 ('.DS_Store', b'junk')]))
        convert_markdown_bundle(source, output)
        self.assertEqual([p.name for p in output.iterdir()], ['main.md'])

    def test_corrupt_image_and_unhandled_html_media_are_explicit_failures(self):
        for items in ([('main.md', '![bad](images/a.png)'), ('images/a.png', b'not an image')],
                      [('main.md', '<img src="images/a.png">'), ('images/a.png', png())]):
            source, output = self.run_bundle(bundle(items))
            with self.assertRaises(MarkdownBundleError):
                convert_markdown_bundle(source, output)
            self.assertFalse(output.exists())

    def test_size_bomb_or_truncated_zip_never_delivered(self):
        for raw in (bundle([('main.md', 'A'*(2*1024**2))]), bundle()[:-20]):
            source, output = self.run_bundle(raw)
            with self.assertRaises(MarkdownBundleError):
                convert_markdown_bundle(source, output)
            self.assertFalse(output.exists())

    def test_candidate_not_overwritten_and_sibling_assets_not_lost(self):
        source, output = self.run_bundle(bundle())
        convert_markdown_bundle(source, output)
        with self.assertRaises(MarkdownBundleError):
            convert_markdown_bundle(source, output)
        source2, output2 = self.run_bundle(bundle([('paper/main.md', '# Paper'), ('images/a.png', png())]))
        with self.assertRaises(MarkdownBundleError):
            convert_markdown_bundle(source2, output2)
        self.assertFalse(output2.exists())


class MarkdownBundlePackingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root/'images').mkdir()
        (self.root/'images/图 1.png').write_bytes(png())
        self.markdown = self.root/'paper.md'
        self.markdown.write_text('# 论文\n\n![图](<images/图 1.png>)\n')
        self.output = self.root/'article.zip'

    def test_pack_actual_md_images_pdf_and_sidecar_then_reimport(self):
        pdf = self.root/'paper.pdf'; pdf.write_bytes(b'%PDF-1.4\n% synthetic only\n')
        sidecar = self.root/'content_list_v2.json'; sidecar.write_text('[]')
        result = pack_markdown(self.markdown, self.output, original_pdf=pdf, sidecars=[sidecar])
        self.assertEqual(result['files'], 4)
        out = self.root/'unpacked'
        self.assertEqual(convert_markdown_bundle(self.output, out).read_bytes(), self.markdown.read_bytes())
        self.assertEqual((out/'images/图 1.png').read_bytes(), png())
        self.assertEqual((out/'source/original.pdf').read_bytes(), pdf.read_bytes())
        self.assertEqual((out/'source/content_list_v2.json').read_bytes(), b'[]')

    def test_pack_missing_asset_does_not_leave_deliverable(self):
        (self.root/'images/图 1.png').unlink()
        with self.assertRaises(MarkdownBundleError):
            pack_markdown(self.markdown, self.output)
        self.assertFalse(self.output.exists())

    def test_pack_refuses_symlink_to_an_outside_file(self):
        image = self.root/'images/图 1.png'; image.unlink()
        elsewhere = self.root/'separate.png'; elsewhere.write_bytes(png())
        image.symlink_to(elsewhere)
        with self.assertRaises(MarkdownBundleError):
            pack_markdown(self.markdown, self.output)
        self.assertFalse(self.output.exists())

    def test_pack_never_overwrites_existing_result(self):
        self.output.write_bytes(b'prior result')
        with self.assertRaises(MarkdownBundleError):
            pack_markdown(self.markdown, self.output)
        self.assertEqual(self.output.read_bytes(), b'prior result')


class MarkdownBundleWorkerTests(unittest.TestCase):
    setUp = conversion_tests.ConversionTests.setUp
    receive = conversion_tests.ConversionTests.receive
    convert = conversion_tests.ConversionTests.convert

    def test_actual_worker_registers_images_and_keeps_zip_as_original(self):
        raw = bundle()
        entry = self.receive(raw, received_filename='synthetic.zip')
        inspected = self.conversion.inspect(entry)
        self.assertEqual(self.conversion.service.ledger.get_inspection(inspected['inspection_id'])['route'], 'markdown_bundle_offline')
        _, mapping = self.convert(entry)
        row, actual, root = self.conversion.extraction(mapping['extraction_id'])
        images = json.loads(row['inventory_json'])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]['role'], 'extracted_original')
        self.assertEqual((root/images[0]['relative_path']).read_bytes(), png())
        self.assertEqual(actual['image_mappings'][0]['original_path'], 'images/figure.png')
        self.assertEqual(actual['locator_precision'], 'document')
        self.assertEqual(actual['page_mappings'], [])
        original = self.registry.entry(entry)
        self.assertEqual((self.runtime.root/original['relative_path']).read_bytes(), raw)

    def test_dot_relative_image_link_keeps_original_and_gets_registered(self):
        raw=bundle([('main.md', '# Article\n\nSynthetic article text describes the attached figure and preserves its relative path.\n\n![figure](./images/a.png)\n'), ('images/a.png', png())])
        entry=self.receive(raw,received_filename='relative.zip')
        _,mapping=self.convert(entry)
        row,actual,root=self.conversion.extraction(mapping['extraction_id'])
        self.assertEqual(actual['image_mappings'][0]['original_path'],'images/a.png')
        self.assertEqual(json.loads(row['inventory_json'])[0]['role'],'extracted_original')
        self.assertIn('./images/a.png',(root/'source.md').read_text())

    def test_worker_quality_uses_same_commonmark_image_paths(self):
        body = '# Article\n\nSynthetic article with encoded and reference image paths.\n\n![inline](images/图%201.png)\n\n![reference][fig]\n\n[fig]: <images/图 1.png>\n\n```md\n![example](missing.png)\n```\n'
        entry = self.receive(bundle([('main.md', body), ('images/图 1.png', png())]), received_filename='commonmark.zip')
        _, mapping = self.convert(entry)
        self.assertEqual(len(mapping['image_mappings']), 2)
        self.assertTrue(all(i['original_path'] == 'images/图 1.png' for i in mapping['image_mappings']))

    def test_failed_bundle_never_registers_partial_extraction(self):
        entry = self.receive(bundle([('main.md', '![missing](images/missing.png)')]), received_filename='bad.zip')
        inspected = self.conversion.inspect(entry)
        task = self.conversion.submit(inspected['inspection_id'], 'missing-asset-test')
        self.conversion.worker.run_once()
        row = self.conversion.service.ledger.get_task(task['task_id'])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_code'], 'MARKDOWN_BUNDLE_FAILED')
        self.assertEqual(self.registry.rows('SELECT * FROM extraction_snapshots'), [])

    def test_legacy_zip_route_is_unchanged(self):
        source = self.runtime.root/'source-staging'/'legacy.zip'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(bundle([('001.png', png())]))
        service = ConversionService(self.conversion.settings)
        inspection = service.inspect_submission(source_kind='local_staging', source_relpath='legacy.zip')
        self.assertEqual(service.ledger.get_inspection(inspection['inspection_id'])['route'], 'image_zip_local_ocr')
