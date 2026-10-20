import io
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from PIL import Image
from pypdf import PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from kb_custody_v21 import pdf_assets
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.pdf_assets import PdfAssetSupplement, embedded_rasters
from kb_custody_v21.staging import IntegrityError
import test_conversion_v21 as conversion_tests


def illustrated_pdf():
    image = Image.new('RGB', (40, 30), 'white')
    image.putpixel((4, 5), (23, 71, 129))
    original = io.BytesIO()
    image.save(original, 'PNG')
    out = io.BytesIO()
    canvas = Canvas(out, invariant=1)
    for i in range(2):
        canvas.drawString(60, 730, f'Synthetic page {i + 1}: a figure plus text.')
        canvas.drawImage(ImageReader(io.BytesIO(original.getvalue())), 60, 610, 80, 60)
        canvas.showPage()
    canvas.save()
    return out.getvalue(), image.tobytes()


class EmbeddedRasterTests(unittest.TestCase):
    def test_physical_page_aliases_keep_pixels_and_native_object_locators(self):
        raw, pixels = illustrated_pdf()
        items, pages = embedded_rasters(raw)
        self.assertEqual(len(items), 2)
        self.assertEqual([x['source_page_index'] for x in items], [0, 1])
        self.assertEqual(items[0]['sha256'], items[1]['sha256'])
        for item in items:
            self.assertEqual(Image.open(io.BytesIO(item['data'])).convert('RGB').tobytes(), pixels)
            self.assertTrue(item['object_path'])
            self.assertEqual(item['reading_status'], 'not_read')
        self.assertEqual([x['image_indices'] for x in pages], [[0], [1]])
        self.assertTrue(all(x['visual_review'] == 'pending' for x in pages))

    def test_vector_only_page_is_not_falsely_a_complete_image_inventory(self):
        out = io.BytesIO()
        canvas = Canvas(out, invariant=1)
        canvas.rect(20, 20, 100, 100, fill=1)
        canvas.save()
        items, pages = embedded_rasters(out.getvalue())
        self.assertEqual(items, [])
        self.assertEqual(pages[0]['coverage'], 'embedded_rasters_only')
        self.assertEqual(pages[0]['visual_review'], 'pending')

    def test_inline_images_require_review_instead_of_unbounded_decoder(self):
        out = io.BytesIO()
        canvas = Canvas(out, invariant=1)
        canvas.drawInlineImage(Image.new('RGB', (10, 10), 'red'), 10, 10)
        canvas.save()
        with self.assertRaisesRegex(ContractError, 'inline'):
            embedded_rasters(out.getvalue())

    def test_limits_reject_before_producing_a_supplement(self):
        raw, _ = illustrated_pdf()
        for key, limit in [('MAX_SOURCE_BYTES', 2), ('MAX_PAGES', 1), ('MAX_IMAGES', 1),
                           ('MAX_IMAGE_PIXELS', 10), ('MAX_IMAGE_BYTES', 1), ('MAX_TOTAL_BYTES', 1)]:
            with self.subTest(bound=key), patch.object(pdf_assets, key, limit), self.assertRaises(ContractError):
                embedded_rasters(raw)

    def test_encrypted_and_malformed_originals_fail_closed(self):
        raw, _ = illustrated_pdf()
        writer = PdfWriter()
        writer.append(io.BytesIO(raw))
        writer.encrypt('synthetic-password')
        out = io.BytesIO()
        writer.write(out)
        for raw in (out.getvalue(), b'%PDF-1.7\ninvalid synthetic'):
            with self.subTest(kind=raw[:8]), self.assertRaises(ContractError):
                embedded_rasters(raw)


class PdfSupplementTests(unittest.TestCase):
    setUp = conversion_tests.ConversionTests.setUp
    receive = conversion_tests.ConversionTests.receive

    def converted(self):
        raw, _ = illustrated_pdf()
        entry = self.receive(raw, received_filename='synthetic-illustrated.pdf')
        inspection = self.conversion.inspect(entry)
        queued = self.conversion.submit(inspection['inspection_id'], 'synthetic-pdf-supplement')
        self.conversion.worker.run_once()
        task = self.conversion.service.ledger.get_task(queued['task_id'])
        self.assertEqual(task['status'], 'needs_review')
        mapping = json.loads(task['quality_json'])['extraction']
        return entry, task, mapping

    def test_immutable_child_keeps_text_pages_parent_and_pending_qualification(self):
        entry, task, old = self.converted()
        _, _, old_root = self.conversion.extraction(old['extraction_id'])
        old_files = {p.name: p.read_bytes() for p in old_root.iterdir() if p.is_file()}
        result = PdfAssetSupplement(self.registry).prepare(entry, old['extraction_id'])
        new = result['mapping']
        self.assertNotEqual(new['extraction_id'], old['extraction_id'])
        self.assertNotEqual(new['output_asset_id'], old['output_asset_id'])
        self.assertEqual(new['parent_extraction_id'], old['extraction_id'])
        for key in ('output_sha256', 'page_mappings', 'text_mappings', 'transformations', 'issues'):
            self.assertEqual(new[key], old[key])
        self.assertEqual({p.name: p.read_bytes() for p in old_root.iterdir() if p.is_file()}, old_files)
        self.assertEqual(len(result['images']), 2)
        self.assertNotEqual(result['images'][0]['asset_id'], result['images'][1]['asset_id'])
        self.assertEqual(result['images'][0]['sha256'], result['images'][1]['sha256'])
        self.assertFalse(new['pdf_image_inventory']['complete_visual_coverage'])
        self.assertEqual(self.conversion.service.ledger.get_task(task['task_id'])['status'], 'needs_review')
        with self.assertRaisesRegex(ContractError, 'quality review'):
            self.conversion.publication_extraction(new['extraction_id'])
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))
        self.assertFalse(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))

    def test_repeated_operation_reuses_exact_snapshot_without_reextracting(self):
        entry, _, old = self.converted()
        service = PdfAssetSupplement(self.registry)
        result = service.prepare(entry, old['extraction_id'])
        with patch.object(pdf_assets, 'embedded_rasters', side_effect=AssertionError('unexpected repeat')):
            self.assertEqual(service.prepare(entry, old['extraction_id']), result)
        self.assertEqual(len(self.registry.rows('SELECT * FROM expression_snapshots')), 1)

    def test_changed_image_blocks_reuse_and_check(self):
        entry, _, old = self.converted()
        service = PdfAssetSupplement(self.registry)
        result = service.prepare(entry, old['extraction_id'])
        image = Path(result['root']) / result['images'][0]['relative_path']
        image.write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError, 'image changed'):
            service.prepare(entry, old['extraction_id'])

    def test_changed_inventory_blocks_reuse(self):
        entry, _, old = self.converted()
        service = PdfAssetSupplement(self.registry)
        result = service.prepare(entry, old['extraction_id'])
        (Path(result['root']) / 'image-inventory.json').write_text('[]')
        with self.assertRaisesRegex(ContractError, 'inventory changed'):
            service.checked(result['extraction_id'])

    def test_failure_before_registry_commit_keeps_original_and_no_authoritative_child(self):
        entry, _, old = self.converted()
        service = PdfAssetSupplement(self.registry)
        with patch.object(self.registry, 'event', side_effect=OSError('synthetic disk fault')):
            with self.assertRaises(OSError):
                service.prepare(entry, old['extraction_id'])
        self.assertFalse(self.registry.rows('SELECT * FROM expression_snapshots'))
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='pdf_embedded_images_recorded'"))
        self.assertEqual(self.registry.receipt(entry)['source_staging_status'], 'accepted')
        self.assertTrue(self.conversion.extraction(old['extraction_id']))

    def test_source_mutation_during_extraction_leaves_suspect_no_snapshot(self):
        entry, _, old = self.converted()
        original = pdf_assets.embedded_rasters
        def mutate(data):
            result = original(data)
            path = self.runtime.root / self.registry.entry(entry)['relative_path']
            path.write_bytes(b'damaged')
            return result
        with patch.object(pdf_assets, 'embedded_rasters', side_effect=mutate), self.assertRaises(IntegrityError):
            PdfAssetSupplement(self.registry).prepare(entry, old['extraction_id'])
        self.assertEqual(self.registry.receipt(entry)['custody_health'], 'suspect')
        self.assertEqual(self.registry.receipt(entry)['source_staging_status'], 'accepted')
        self.assertFalse(self.registry.rows('SELECT * FROM expression_snapshots'))

    def test_child_and_wrong_source_are_not_reinterpreted_as_new_originals(self):
        entry, _, old = self.converted()
        service = PdfAssetSupplement(self.registry)
        result = service.prepare(entry, old['extraction_id'])
        with self.assertRaisesRegex(ContractError, 'original page-mapped'):
            service.prepare(entry, result['extraction_id'])
        other = self.receive(b'# other', intake_key='synthetic-other', received_filename='other.md')
        with self.assertRaisesRegex(ContractError, 'original page-mapped'):
            service.prepare(other, old['extraction_id'])
