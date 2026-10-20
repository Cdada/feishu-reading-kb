"""Bounded, local PDF image supplements; never imply complete visual coverage.

The text-layer snapshot stays immutable. A new expression adds native embedded
rasters, physical page/object locators and hashes. Vector diagrams, placement,
clipping and reading order still need page review; this is not page rendering.
"""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, __version__ as pypdf_version
from pypdf.errors import PdfReadError

from .contracts import ContractError, new_id
from .conversion import CustodyConversion, durable_new, sha
from .registry import encoded, now
from .staging import IntegrityError, Staging


MAX_SOURCE_BYTES = 32 * 1024**2
MAX_PAGES = 12
MAX_IMAGES = 64
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_BYTES = 32 * 1024**2
MAX_TOTAL_BYTES = 64 * 1024**2


def embedded_rasters(data):
    """Return bytes and exact PDF object paths without fetching external assets."""
    if not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise ContractError('PDF original exceeds the local image extraction bound')
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ContractError('encrypted PDF image extraction is unsupported')
        if not 0 < len(reader.pages) <= MAX_PAGES:
            raise ContractError('PDF page count exceeds the local image extraction bound')
        result, pages, total = [], [], 0
        for page_index, page in enumerate(reader.pages):
            # Inline images require an additional decoder path. Keep this first
            # pilot limited to ordinary image XObjects, including nested Forms.
            contents = page.get_contents()
            if contents is not None and any(op == b'INLINE IMAGE' for _, op in contents.operations):
                raise ContractError('inline PDF images need explicit local review')
            keys = page.images.keys()
            if len(result) + len(keys) > MAX_IMAGES:
                raise ContractError('PDF embedded image count exceeds the local bound')
            page_items = []
            for key in keys:
                parts = list(key) if isinstance(key, (tuple, list)) else [key]
                resource = page
                for name in parts:
                    resource = resource['/Resources']['/XObject'][name].get_object()
                width, height = resource.get('/Width'), resource.get('/Height')
                if (not isinstance(width, int) or not isinstance(height, int) or
                        min(width, height) <= 0 or width * height > MAX_IMAGE_PIXELS):
                    raise ContractError('PDF image pixel dimensions exceed the local bound')
                image = page.images[key]
                image_data = image.data
                total += len(image_data)
                if not image_data or len(image_data) > MAX_IMAGE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ContractError('decoded PDF image bytes exceed the local bound')
                with Image.open(BytesIO(image_data)) as checked:
                    size, media_format = list(checked.size), checked.format
                    checked.verify()
                suffix = { 'PNG': '.png', 'JPEG': '.jpg', 'JPEG2000': '.jp2',
                           'TIFF': '.tiff' }.get(media_format)
                if suffix is None or size != [width, height]:
                    raise ContractError('unsupported or inconsistent embedded PDF image')
                item = dict(source_page_index=page_index, object_path=parts,
                    sha256=sha(image_data), size_bytes=len(image_data), resolution=size,
                    suffix=suffix, extraction_method='pypdf.PageObject.images',
                    extraction_version=pypdf_version, representation='decoded_embedded_raster',
                    reading_status='not_read', data=image_data)
                page_items.append(len(result))
                result.append(item)
            pages.append(dict(page_index=page_index, image_indices=page_items,
                coverage='embedded_rasters_only', visual_review='pending'))
        return result, pages
    except ContractError:
        raise
    except (PdfReadError, OSError, ValueError, KeyError, TypeError) as exc:
        # No parser exception may expose source text or a personal filename.
        raise ContractError('PDF embedded image extraction failed; local review required') from exc


class PdfAssetSupplement:
    def __init__(self, registry):
        self.registry = registry
        self.runtime = registry.runtime
        self.conversion = CustodyConversion(registry)
        self.staging = Staging(registry)

    def prepare(self, entry_id, extraction_id):
        """Register a new expression once. Do not change conversion qualification."""
        try:
            return self._prepare(entry_id, extraction_id)
        except IntegrityError:
            # The surrounding snapshot transaction has rolled back first. Do
            # not lose a health change by writing it into that transaction.
            if self.registry.receipt(entry_id)['custody_health'] == 'healthy':
                self.registry.health(entry_id, 'suspect', 'PDF changed during image extraction')
            raise

    def _prepare(self, entry_id, extraction_id):
        self.runtime.gate()
        row, mapping, original = self.conversion.extraction(extraction_id)
        entry = self.registry.entry(entry_id)
        if ('parent_extraction_id' in row or mapping['extractor'] != 'pypdf' or
                mapping['source_asset_id'] != entry['asset_id'] or
                mapping['source_sha256'] != entry['sha256'] or
                mapping['locator_precision'] != 'page' or json.loads(row['inventory_json'])):
            raise ContractError('PDF supplement requires the original page-mapped text snapshot')
        key = 'pdf-embedded-images:' + extraction_id
        with self.staging.input_binding(entry_id, 'pdf_image_extraction') as binding:
            with self.registry.transaction() as db:
                existing = db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?', (key,)).fetchone()
                if existing:
                    saved = json.loads(existing['payload_json'])
                    return self.checked(saved['extraction_id'])
                data = binding['stream'].read(MAX_SOURCE_BYTES + 1)
                if sha(data) != entry['sha256']:
                    raise IntegrityError('accepted PDF changed during image extraction')
                rasters, pages = embedded_rasters(data)
                if len(pages) != len(mapping['page_mappings']):
                    raise ContractError('PDF image and text physical page counts differ')
                body = (original / 'source.md').read_bytes()
                if sha(body) != mapping['output_sha256']:
                    raise ContractError('PDF text changed during image extraction')
                revised = deepcopy(mapping)
                revised.update(extraction_id=new_id('extraction'), output_asset_id=new_id('asset'),
                               extracted_at=now(), parent_extraction_id=extraction_id)
                root = self.runtime.checked(self.runtime.path('conversion-work/extractions') / revised['extraction_id'])
                root.mkdir(parents=True, exist_ok=False)
                durable_new(root / 'source.md', body)
                inventory, image_map = [], []
                for item in rasters:
                    asset_id = new_id('asset')
                    target = root / 'original-images' / (asset_id + item['suffix'])
                    target.parent.mkdir(exist_ok=True)
                    durable_new(target, item['data'])
                    provenance = {k: v for k, v in item.items() if k not in {'data', 'suffix'}}
                    inventory.append(dict(asset_id=asset_id, relative_path=target.relative_to(root).as_posix(),
                        original_target='pdf-embedded/' + target.name, role='extracted_original', **provenance))
                    image_map.append(dict(kind='pdf_embedded_image', image_asset_id=asset_id,
                        local_representation_asset_id=asset_id, source_page_index=item['source_page_index'],
                        object_path=item['object_path'], char_start=None, char_end=None, caption=None))
                revised['image_mappings'] = image_map
                revised['pdf_image_inventory'] = dict(schema_version='pdf-embedded-inventory/v1',
                    source_sha256=entry['sha256'], pages=pages, complete_visual_coverage=False,
                    interpretation='decoded rasters preserve pixels; original PDF remains authoritative',
                    limitations=['No vector extraction, placement reconstruction or OCR.',
                                 'Saving an embedded image does not classify or read it.'])
                durable_new(root / 'extraction-map.json', encoded(revised).encode())
                durable_new(root / 'image-inventory.json', encoded(inventory).encode())
                binding['stream'].seek(0)
                if sha(binding['stream'].read(MAX_SOURCE_BYTES + 1)) != entry['sha256']:
                    raise IntegrityError('accepted PDF changed during image extraction')
                db.execute('INSERT INTO expression_snapshots VALUES (?,?,?,?,?,?,?)',
                    (revised['extraction_id'], extraction_id, entry['asset_id'], revised['output_asset_id'],
                     self.runtime.relative(root), encoded(revised), encoded(inventory)))
                self.registry.event(db, 'pdf_embedded_images_recorded', extraction_id, key,
                    dict(extraction_id=revised['extraction_id'], parent_extraction_id=extraction_id,
                         original_sha256=entry['sha256'], inventory_sha256=sha(encoded(inventory).encode()),
                         qualification='unchanged', image_count=len(inventory)))
        return self.checked(revised['extraction_id'])

    def checked(self, extraction_id):
        row, mapping, root = self.conversion.extraction(extraction_id)
        if not mapping.get('pdf_image_inventory'):
            raise ContractError('not a PDF embedded-image supplement')
        inventory = json.loads(row['inventory_json'])
        if json.loads((root / 'image-inventory.json').read_bytes()) != inventory:
            raise ContractError('PDF image inventory changed')
        for image in inventory:
            path = self.runtime.checked(root / image['relative_path'])
            data = path.read_bytes()
            if len(data) != image['size_bytes'] or sha(data) != image['sha256']:
                raise ContractError('PDF embedded image changed')
        return dict(extraction_id=extraction_id, root=str(root), mapping=mapping, images=inventory,
                    qualification='needs_review', model_image_reading='not_read')
