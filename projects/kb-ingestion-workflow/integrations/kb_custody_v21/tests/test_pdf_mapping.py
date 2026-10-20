import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from pdf_text_map import join_pages, extract_pdf_text, digest
from kb_workflow import load_config
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.pdf_mapping import checked_pdf_map
from local_document_conversion_mcp.errors import ServiceError
from local_document_conversion_mcp.service import ROUTES
import test_conversion_v21 as conversion_tests
from test_custody import BASE


def synthetic_pdf():
    output = io.BytesIO()
    canvas = Canvas(output, invariant=1)
    canvas.drawString(60, 760, 'SOURCE-A: Synthetic first page, with its original text layer.')
    canvas.drawString(60, 730, 'Review preserves order and does not infer missing content.')
    canvas.showPage()
    # A graphics-only page has no text. It must remain in the page inventory.
    canvas.setFillColorRGB(0.1, 0.4, 0.8)
    canvas.rect(60, 630, 100, 100, fill=1)
    canvas.showPage()
    canvas.drawString(60, 760, 'SOURCE-Z: Synthetic final page after a page without text.')
    canvas.drawString(60, 730, 'The printed label below is not the source page index.')
    canvas.drawString(280, 40, '997')
    canvas.save()
    return output.getvalue()


class PdfMapUnitTests(unittest.TestCase):
    def test_unicode_newlines_empty_pages_and_separators_have_exact_ranges(self):
        pages = ['城市😀\r\n甲\n', '', '乙\n\n末尾']
        body, mapping = join_pages(pages, 'synthetic-hash')
        self.assertEqual(body, '\n\n'.join(pages))
        self.assertEqual(mapping['text_length'], len(body))
        checked_pdf_map(mapping, body, 'synthetic-hash')
        self.assertEqual(mapping['page_mappings'][1]['text_ranges'], [])
        for page, original in zip(mapping['page_mappings'], pages):
            ranges = page['text_ranges']
            actual = ''.join(body[r['char_start']:r['char_end']] for r in ranges)
            self.assertEqual(actual, original)
        self.assertEqual(mapping['page_mappings'][0]['text_ranges'][0]['char_end'], 7)

    def test_invalid_identity_ranges_and_page_indices_fail_closed(self):
        body, mapping = join_pages(['First😀\n', '', 'Last\n'], 'synthetic-hash')
        mutations = [
            lambda m: m.update(source_sha256='wrong'),
            lambda m: m.update(output_sha256='wrong'),
            lambda m: m['page_mappings'][2].update(page_index=997),
            lambda m: m['page_mappings'][0]['text_ranges'][0].update(char_end=len(body) + 1),
            lambda m: m['page_mappings'][0]['text_ranges'][0].update(char_start=False),
            lambda m: m['page_mappings'][1].update(text_ranges=[dict(char_start=10, char_end=11)]),
            lambda m: m['page_mappings'][0].update(text_sha256='wrong'),
            lambda m: m.update(text_mappings=[]),
            lambda m: m.update(transformations=[]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index), self.assertRaises(ContractError):
                changed = copy.deepcopy(mapping)
                mutate(changed)
                checked_pdf_map(changed, body, 'synthetic-hash')

    def test_legacy_pdf_route_and_config_remain_unchanged(self):
        self.assertEqual(ROUTES['.pdf'], 'local_mineru')
        path = BASE / 'test-output/pdf-config-test.json'
        config = dict(icloud_inbox='input', local_transfer_station='station', conversion_tool_workspace='tools')
        path.write_text(json.dumps(config))
        self.assertFalse(load_config(path).pdf_text_with_map)
        config['pdf_text_with_map'] = 'true'
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'must be boolean'):
            load_config(path)


class PdfWorkerTests(unittest.TestCase):
    setUp = conversion_tests.ConversionTests.setUp
    receive = conversion_tests.ConversionTests.receive

    def run_pdf(self, data=None):
        data = data or synthetic_pdf()
        entry = self.receive(data, received_filename='synthetic-three-pages.pdf')
        inspected = self.conversion.inspect(entry)
        stored = self.conversion.service.ledger.get_inspection(inspected['inspection_id'])
        self.assertEqual(stored['route'], 'pdf_text_local')
        task = self.conversion.submit(inspected['inspection_id'], 'synthetic-pdf-01')
        self.conversion.worker.run_once()
        return entry, self.conversion.service.ledger.get_task(task['task_id'])

    def test_actual_worker_maps_original_pages_and_blocks_unreviewed_publication(self):
        data = synthetic_pdf()
        entry, task = self.run_pdf(data)
        self.assertEqual(task['status'], 'needs_review', task)
        mapping = json.loads(task['quality_json'])['extraction']
        row, actual, root = self.conversion.extraction(mapping['extraction_id'])
        text = (root / 'source.md').read_bytes().decode('utf-8')
        expected_pages = [page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages]
        self.assertEqual(text, '\n\n'.join(expected_pages))
        self.assertEqual(mapping['locator_precision'], 'page')
        self.assertEqual([p['page_index'] for p in mapping['page_mappings']], [0, 1, 2])
        self.assertEqual(mapping['page_mappings'][1]['text_ranges'], [])
        self.assertTrue(all(p['page_label'] is None for p in mapping['page_mappings']))
        self.assertEqual(mapping['source_sha256'], digest(data))
        self.assertEqual(mapping['output_sha256'], digest(text.encode()))
        self.assertIn('997', text)
        self.assertNotIn('MinerU', text)
        self.assertEqual(json.loads(row['inventory_json']), [])
        self.assertIn('images', ' '.join(mapping['issues']))
        with self.assertRaisesRegex(ContractError, 'quality review'):
            self.conversion.publication_extraction(mapping['extraction_id'])
        source = self.runtime.root / self.registry.entry(entry)['relative_path']
        self.assertEqual(source.read_bytes(), data)
        config = json.loads((self.conversion.settings.output_root / 'tasks' / task['task_id'] / 'workflow-config.json').read_text())
        self.assertTrue(config['pdf_text_with_map'])
        self.assertEqual(task['parse_method'], 'txt')
        (BASE / 'test-output/pdf-page-map-sample.local.json').write_text(json.dumps(
            dict(source=str(source), extraction=str(root), task_status=task['status'])))

    def test_encrypted_pdf_fails_without_extraction_or_password_attempts(self):
        writer = PdfWriter()
        writer.append(io.BytesIO(synthetic_pdf()))
        writer.encrypt('synthetic-test-only')
        encrypted = io.BytesIO()
        writer.write(encrypted)
        _, task = self.run_pdf(encrypted.getvalue())
        self.assertEqual(task['status'], 'failed', task)
        self.assertFalse(self.registry.rows('SELECT * FROM extraction_snapshots'))
        self.assertFalse(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))

    def test_malformed_pdf_fails_without_fabricated_page_map(self):
        _, task = self.run_pdf(b'%PDF-1.7\nsynthetic invalid document\n')
        self.assertEqual(task['status'], 'failed', task)
        self.assertFalse(self.registry.rows('SELECT * FROM extraction_snapshots'))

    def test_scanned_profile_refuses_unavailable_ocr_before_queueing(self):
        entry = self.receive(synthetic_pdf(), received_filename='synthetic.pdf')
        inspected = self.conversion.inspect(entry)
        with self.assertRaisesRegex(ServiceError, 'OCR'):
            self.conversion.submit(inspected['inspection_id'], 'synthetic-ocr-01', document_profile='scanned_text')
        self.assertFalse(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))

    def test_tampered_sidecar_never_enters_immutable_registry(self):
        original = self.conversion._record_extraction
        def corrupt(task, inspection, batch):
            for sidecar in (batch / '04_知识库输入文件').rglob('*.pages.json'):
                mapping = json.loads(sidecar.read_text())
                mapping['source_sha256'] = 'wrong'
                sidecar.write_text(json.dumps(mapping))
            return original(task, inspection, batch)
        with patch.object(self.conversion.worker, 'record_extraction', side_effect=corrupt):
            _, task = self.run_pdf()
        self.assertEqual(task['status'], 'failed', task)
        self.assertFalse(self.registry.rows('SELECT * FROM extraction_snapshots'))

    def test_graphics_only_pdf_keeps_empty_ranges_without_ocr(self):
        output = io.BytesIO()
        canvas = Canvas(output, invariant=1)
        canvas.rect(60, 630, 100, 100, fill=1)
        canvas.save()
        source = self.runtime.root / 'conversion-work/graphics-only.pdf'
        source.write_bytes(output.getvalue())
        text, mapping = extract_pdf_text(source)
        self.assertEqual(text, '')
        self.assertEqual(mapping['page_mappings'][0]['text_ranges'], [])
        checked_pdf_map(mapping, text, digest(output.getvalue()))
        _, task = self.run_pdf(output.getvalue())
        self.assertNotEqual(task['status'], 'ready_candidate')
        self.assertFalse(self.registry.rows('SELECT * FROM extraction_snapshots'))
