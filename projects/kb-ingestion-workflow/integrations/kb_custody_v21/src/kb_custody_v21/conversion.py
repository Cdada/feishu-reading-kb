"""Accepted-source composition of the existing conversion service and worker.

The conversion ledger holds the entry ID, never an inbound fallback. A lease
spans the entire worker execution, including committing the derived snapshot.
"""
from contextlib import contextmanager
import csv
import hashlib
import json
import os
from pathlib import Path

from local_document_conversion_mcp.config import ServiceSettings, SourceRoot
from local_document_conversion_mcp.service import ConversionService

from .contracts import ContractError, new_id, validate_id
from .registry import encoded, now
from .staging import Staging
from .conversion_recovery import ConversionRecovery, CustodyWorker


def sha(data):
    return hashlib.sha256(data).hexdigest()


def durable_new(path, data):
    """Write a new immutable candidate; caller commits its authority afterwards."""
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CustodyConversion:
    def __init__(self, registry):
        self.registry, self.runtime = registry, registry.runtime
        self.staging = Staging(registry)
        root = self.runtime.checked(self.runtime.root / 'conversion-work')
        self.settings = ServiceSettings(
            source_roots={'local_staging': SourceRoot('local_staging', self.runtime.path('source-staging'))},
            ledger_path=root / 'conversion.sqlite3', output_root=self.runtime.path('conversion-work/output'), delivery_root=self.runtime.path('conversion-work/delivery'),
            conversion_tool_workspace=root / 'tools', minimum_free_bytes=128 * 1024**2,
        )
        self.runtime.checked(self.settings.ledger_path)
        self._active_paths = {}
        # Set only by the authorized PDF adapter after a complete chunk result.
        self.pdf_result = None
        self.service = ConversionService(self.settings, custody_resolver=self._resolve_active,
                                         recover_interrupted=False)
        self.recovery = ConversionRecovery(self)
        self.worker = CustodyWorker(self, accepted_input=self._bind_worker,
                                   record_extraction=self._record_extraction,
                                   process_accepted=self._process_markdown)

    def _resolve_active(self, entry_id):
        if entry_id not in self._active_paths:
            raise ContractError('accepted input must be resolved inside its lease')
        return self._active_paths[entry_id]

    def _ready(self, entry_id):
        row = self.registry.entry(entry_id)
        groups = self.registry.rows('SELECT * FROM source_group_memberships WHERE entry_id=?', (entry_id,))
        for group in groups:
            args = (group['source_group_id'], group['membership_revision'])
            sealed = self.registry.rows('SELECT membership_sealed FROM source_groups WHERE source_group_id=? AND membership_revision=?', args)
            members = self.registry.rows('''SELECT r.status,f.custody_health FROM source_group_memberships m
                JOIN staging_receipts r ON r.entry_id=m.entry_id JOIN staging_files f ON f.asset_id=r.asset_id
                WHERE m.source_group_id=? AND m.membership_revision=? AND m.required=1''', args)
            if not sealed[0]['membership_sealed'] or not members or any(
                    m['status'] != 'accepted' or m['custody_health'] != 'healthy' for m in members):
                raise ContractError('sealed group requires all required originals accepted and healthy')
        return row

    @contextmanager
    def _bind_worker(self, inspection):
        if inspection['source_kind'] != 'custody_v21':
            raise ContractError('V2.1 worker refuses legacy path-bound tasks')
        entry_id = inspection['source_relpath']
        validate_id(entry_id, 'entry')
        self._ready(entry_id)
        task_id = self.worker.active_task['task_id']
        with self.staging.input_binding(entry_id, 'conversion', conversion_task_id=task_id) as binding:
            yield binding

    def inspect(self, entry_id):
        entry = self._ready(entry_id)
        if Path(entry['safe_filename']).suffix.lower() not in {'.md', '.txt', '.webarchive', '.pdf', '.zip', '.docx', '.xlsx'}:
            raise ContractError('Supported local routes: Markdown, TXT, DOCX, XLSX, webarchive, Markdown bundle and PDF; legacy DOC/XLS require conversion first')
        with self.staging.input_binding(entry_id, 'conversion_inspection') as binding:
            self._active_paths[entry_id] = Path(binding['path'])
            try:
                return self.service.inspect_submission(source_kind='custody_v21', source_relpath=entry_id)
            finally:
                del self._active_paths[entry_id]

    def submit(self, inspection_id, idempotency_key, duplicate_action='not_applicable', **options):
        inspection = self.service.ledger.get_inspection(inspection_id)
        if not inspection or inspection['source_kind'] != 'custody_v21':
            raise ContractError('V2.1 accepted-source inspection required')
        entry_id = inspection['source_relpath']
        self._ready(entry_id)
        with self.recovery.lock('admission'), self.staging.input_binding(entry_id, 'conversion_submit') as binding:
            self._active_paths[entry_id] = Path(binding['path'])
            try:
                binding['retain_lease'] = True
                try:
                    result = self.service.submit_conversion(inspection_id=inspection_id,
                        idempotency_key=idempotency_key, duplicate_action=duplicate_action,
                        requested_by='v21-dev', **options)
                except Exception:
                    # A lost result may follow a durable queue write. Only a
                    # successful lookup proving no task exists clears this guard.
                    if self.service.ledger.get_task_by_idempotency(
                            requested_by='v21-dev', idempotency_key=idempotency_key) is None:
                        binding['retain_lease'] = False
                    raise
                self.recovery.register(result, inspection)
                binding['retain_lease'] = False
                return result
            finally:
                del self._active_paths[entry_id]

    def _process_markdown(self, task, inspection, batch):
        entry = self.registry.entry(inspection['source_relpath'])
        if Path(entry['safe_filename']).suffix.lower() in {'.txt', '.docx', '.xlsx'}:
            from .office_ingestion import stage_document
            return stage_document(self, entry, batch)
        if inspection['route'] == 'pdf_mineru_local':
            from .mineru_ingestion import stage_pdf_result
            if self.pdf_result is None:
                raise ContractError('complete authorized MinerU result required before conversion import')
            return stage_pdf_result(self, entry, batch, self.pdf_result(entry))
        if inspection['route'] != 'text_direct' or Path(entry['safe_filename']).suffix.lower() != '.md':
            return False
        from .embedded_markdown import normalize_embedded_markdown, MAX_INPUT_BYTES
        original = self.runtime.checked(self.runtime.path(entry['relative_path']))
        with original.open('rb') as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) != entry['size_bytes'] or sha(raw) != entry['sha256']:
            raise ContractError('accepted Markdown bytes changed or exceed input bound')
        content, assets, report = normalize_embedded_markdown(raw)
        if not report['occurrences']:
            return False  # Keep the established text-only conversion path.
        manifest = batch / '_batch_manifest.csv'
        with manifest.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        if len(rows) != 1:
            raise ContractError('self-contained Markdown requires exactly one accepted original')
        root = self.runtime.checked(batch / '04_知识库输入文件/custody-markdown')
        root.mkdir(parents=True, exist_ok=False)
        durable_new(root / 'source.md', content)
        # The legacy quality checker expects a titled candidate. Keep its
        # generated heading outside the exact source used for guide quotations.
        durable_new(root / 'candidate.md', '# 资料预检查\n\n'.encode() + content)
        for target, data in assets.items():
            path = self.runtime.checked(root / target)
            path.parent.mkdir(exist_ok=True)
            durable_new(path, data)
        durable_new(root / 'image-availability.json', encoded(report).encode())
        rows[0].update(processing_status='processed', kb_folder=root.relative_to(batch).as_posix(),
            primary_input=(root / 'candidate.md').relative_to(batch).as_posix(),
            auxiliary_input=(root / 'source.md').relative_to(batch).as_posix(),
            group_role='', classification='文字型', quality_status='', conversion_method='custody_embedded_markdown_v1',
            notes='原件完整保留；图片缺失需在生成导读前由本人选择，转换完成不代表已批准导读或入库。')
        with manifest.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return True

    def _record_extraction(self, task, inspection, batch):
        with (batch / '_batch_manifest.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != 1 or rows[0]['processing_status'] != 'processed':
            raise ContractError('conversion did not produce one completed source')
        primary = rows[0]['primary_input']
        if (rows[0].get('group_role') == 'main_markdown' or
                rows[0].get('conversion_method') in {'custody_embedded_markdown_v1', 'custody_mineru_pdf_v1', 'custody_office_document_v1'}):
            # Existing V1 wraps its main Markdown in a generated package with
            # operational prose. The actual body is retained as an auxiliary.
            originals = [p.strip() for p in rows[0]['auxiliary_input'].split(';') if p.strip().endswith('.md')]
            if len(originals) != 1:
                raise ContractError('cannot identify one unwrapped extracted Markdown body')
            primary = originals[0]
        relative = Path(primary)
        if relative.is_absolute() or '..' in relative.parts:
            raise ContractError('unsafe converter output path')
        source = self.runtime.checked(batch / relative)
        if source.suffix != '.md':
            raise ContractError('conversion requires an actual Markdown body')
        content = source.read_bytes()
        text = content.decode('utf-8')  # strict decoding; offsets describe stored bytes
        entry = self.registry.entry(inspection['source_relpath'])
        page_map = None
        embedded_assets = None
        image_availability = None
        if inspection['route'] == 'text_direct' and Path(entry['safe_filename']).suffix.lower() == '.md':
            from .embedded_markdown import normalize_embedded_markdown, MAX_INPUT_BYTES
            original = self.runtime.checked(self.runtime.path(entry['relative_path']))
            with original.open('rb') as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
            if len(raw) != entry['size_bytes'] or sha(raw) != entry['sha256']:
                raise ContractError('accepted Markdown bytes changed or exceed input bound')
            normalized, embedded_assets, image_availability = normalize_embedded_markdown(raw)
            if image_availability['occurrences']:
                if normalized != content:
                    raise ContractError('normalized Markdown candidate differs from the accepted source')
            else:
                embedded_assets = image_availability = None
            text = content.decode('utf-8')
        if inspection['route'] == 'pdf_text_local':
            from .pdf_mapping import checked_pdf_map
            sidecar = self.runtime.checked(source.with_suffix('.pages.json'))
            page_map = checked_pdf_map(json.loads(sidecar.read_text(encoding='utf-8')), text, entry['sha256'])
        pdf_images = {}
        if inspection['route'] == 'pdf_mineru_local':
            from .mineru_ingestion import checked_mineru_map
            sidecar = self.runtime.checked(source.with_suffix('.pages.json'))
            page_map, pdf_images = checked_mineru_map(json.loads(sidecar.read_text(encoding='utf-8')), text, entry['sha256'], source.parent)
        extraction_id, output_asset_id = new_id('extraction'), new_id('asset')
        root = self.runtime.checked(self.runtime.path('conversion-work/extractions') / extraction_id)
        root.mkdir(parents=True, exist_ok=False)
        durable_new(root / 'source.md', content)
        # Page precision is supplied only by a validated extraction sidecar.
        # Never infer it from filenames or printed page labels.
        inventory = []
        from .image_links import image_spans
        spans = image_spans(text)
        by_target = {}
        candidates = []
        if embedded_assets is not None:
            candidates = [(target, data) for target, data in sorted(embedded_assets.items())]
        else:
            for image in sorted(source.parent.rglob('*')):
                if image.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tiff', '.bmp'} and image.is_file():
                    self.runtime.checked(image)
                    candidates.append((image.relative_to(source.parent).as_posix(), image.read_bytes()))
        for target, data in candidates:
            aid = new_id('asset')
            copied = root / 'original-images' / (aid + Path(target).suffix.lower())
            copied.parent.mkdir(exist_ok=True)
            durable_new(copied, data)
            linked = any(s['original_path'] == target for s in spans)
            by_target[target] = aid
            inventory.append({'asset_id': aid, 'relative_path': copied.relative_to(root).as_posix(),
                              'original_target': target, 'sha256': sha(data), 'size_bytes': len(data),
                              'role': 'extracted_original' if linked and inspection['route'] in {'text_direct','webarchive_offline','markdown_bundle_offline','pdf_mineru_local','docx_direct','xlsx_direct'} else 'unclassified',
                              'source_page_index': pdf_images[target]['page_index'] if target in pdf_images else None})
        mapping = dict(schema_version='kb-extraction-map/v2.1', extraction_id=extraction_id,
            source_asset_id=entry['asset_id'], output_asset_id=output_asset_id,
            extractor='local-kb-workflow', extractor_version=sha(self.settings.workflow_script.read_bytes()),
            mode=rows[0]['conversion_method'] or inspection['route'], extracted_at=now(),
            source_sha256=entry['sha256'], output_sha256=sha(content), text_encoding='UTF-8',
            offset_unit='unicode_code_point', range_convention='half_open', newline_policy='as_stored',
            text_length=len(text), locator_precision='document', page_mappings=[], text_mappings=[],
            image_mappings=[], transformations=[], issues=['No trustworthy page mapping supplied by collector.'])
        if page_map is not None:
            mapping.update(page_map)
        if rows[0].get('conversion_method') == 'custody_office_document_v1':
            from .office_ingestion import checked_document_map
            mapping.update(checked_document_map(self, entry, source, text))
        if image_availability is not None:
            from . import embedded_markdown
            mapping.update(extractor='custody-embedded-markdown',
                           extractor_version=sha(Path(embedded_markdown.__file__).read_bytes()))
            mapping['image_availability'] = image_availability
            mapping['transformations'] = [dict(kind='embedded_image_extraction' if item['status'] == 'available'
                else 'unavailable_image_marker', old_range=item['original_range'], new_range=item['normalized_range'],
                target_sha256=item['target_sha256']) for item in image_availability['occurrences']]
            if image_availability['missing_count']:
                mapping['issues'].append('Missing images require an explicit owner choice before guide generation.')
        mapping['image_mappings'] = [dict(image_asset_id=by_target[s['original_path']],
            char_start=s['char_start'],char_end=s['char_end'],original_path=s['original_path'],caption=s['caption'],
            source_page_index=pdf_images[s['original_path']]['page_index'] if s['original_path'] in pdf_images else None,
            local_representation_asset_id=by_target[s['original_path']])
            for s in spans if s['original_path'] in by_target]
        if any(s['original_path'] not in by_target for s in spans):
            mapping['issues'].append('Some linked images are unavailable; publication requires resolving them.')
        durable_new(root / 'extraction-map.json', encoded(mapping).encode())
        durable_new(root / 'image-inventory.json', encoded(inventory).encode())
        with self.registry.transaction() as db:
            db.execute('INSERT INTO extraction_snapshots VALUES (?,?,?,?,?,?,?)',
                       (extraction_id, task['task_id'], entry['asset_id'], output_asset_id,
                        self.runtime.relative(root), encoded(mapping), encoded(inventory)))
        return mapping

    def extraction(self, extraction_id):
        validate_id(extraction_id, 'extraction')
        rows = self.registry.rows('SELECT * FROM extraction_snapshots WHERE extraction_id=?', (extraction_id,))
        if not rows:
            rows = self.registry.rows('SELECT * FROM expression_snapshots WHERE extraction_id=?', (extraction_id,))
        if not rows:
            raise ContractError('unknown extraction')
        row = rows[0]
        path = self.runtime.checked(self.runtime.path(row['relative_root']))
        mapping = json.loads(row['map_json'])
        if sha((path / 'source.md').read_bytes()) != mapping['output_sha256']:
            raise ContractError('extracted body identity changed')
        if json.loads((path / 'extraction-map.json').read_text()) != mapping:
            raise ContractError('extraction map changed')
        return row, mapping, path

    def extraction_ancestry(self, extraction_id):
        row, mapping, path = self.extraction(extraction_id)
        chain = [(row, mapping, path)]
        original = row
        seen = {extraction_id}
        while 'parent_extraction_id' in original:
            parent = original['parent_extraction_id']
            if parent in seen or len(seen) >= 16:
                raise ContractError('invalid extraction ancestry')
            seen.add(parent)
            ancestor = self.extraction(parent)
            original = ancestor[0]
            if (original['source_asset_id'], ancestor[1]['source_sha256']) != (row['source_asset_id'], mapping['source_sha256']):
                raise ContractError('extraction ancestry changed original identity')
            chain.append(ancestor)
        return chain

    def publication_extraction(self, extraction_id, *, expected_task_id=None):
        chain = self.extraction_ancestry(extraction_id)
        row, mapping, path = chain[0]
        original = chain[-1][0]
        if expected_task_id is not None and original['conversion_task_id'] != expected_task_id:
            raise ContractError('Pilot extraction does not belong to the selected conversion task')
        task = self.service.ledger.get_task(original['conversion_task_id'])
        if not task or task['status'] not in {'ready_candidate', 'needs_review'}:
            raise ContractError('conversion quality review is unresolved; publication blocked')
        from .material_review import MaterialReviews
        reviews = MaterialReviews(self.registry)
        saved = reviews.checked(extraction_id)
        if task['status'] == 'needs_review' or saved is not None:
            reviews.require_qualified(extraction_id)
        from .image_availability import require_image_choice
        require_image_choice(self, extraction_id)
        return row, mapping, path

    def registered_images(self, extraction_id):
        """Resolve each immutable image at the version that first registered it.

        Markdown expressions inherit images; PDF supplements introduce new ones.
        A missing/corrupt owner file is an error, never a reason to try a copy.
        """
        chain = self.extraction_ancestry(extraction_id)
        row, _, root = chain[0]
        inventory = json.loads(row['inventory_json'])
        if json.loads((root/'image-inventory.json').read_bytes()) != inventory:
            raise ContractError('registered image inventory changed')
        owners = {}
        for ancestor, mapping, directory in reversed(chain):
            items = json.loads(ancestor['inventory_json'])
            if json.loads((directory/'image-inventory.json').read_bytes()) != items:
                raise ContractError('registered image inventory changed')
            for item in items:
                if item['asset_id'] in owners:
                    if owners[item['asset_id']][0] != item:
                        raise ContractError('image identity changed between expression versions')
                else:
                    owners[item['asset_id']] = (item, directory, mapping['extraction_id'])
        result = []
        for item in inventory:
            _, directory, owner = owners[item['asset_id']]
            path = self.runtime.checked(directory/item['relative_path'])
            if not path.is_relative_to(directory) or path.stat().st_size != item['size_bytes'] or sha(path.read_bytes()) != item['sha256']:
                raise ContractError('registered original image bytes changed')
            result.append(dict(item, path=str(path), extraction_id=owner))
        return result

    def reexpress(self, extraction_id, replacements):
        """Store a new immutable source expression; only parsed image spans change."""
        from .image_links import rewrite_images
        parent, mapping, original = self.extraction(extraction_id)
        if 'parent_extraction_id' in parent:
            raise ContractError('reexpress from the original extraction, preserving its version chain')
        content, revised = rewrite_images((original / 'source.md').read_bytes(), mapping, replacements)
        if revised == mapping:
            return content, revised
        root = self.runtime.checked(self.runtime.path('conversion-work/extractions') / revised['extraction_id'])
        root.mkdir(parents=True, exist_ok=False)
        durable_new(root / 'source.md', content)
        durable_new(root / 'extraction-map.json', encoded(revised).encode())
        durable_new(root / 'image-inventory.json', parent['inventory_json'].encode())
        with self.registry.transaction() as db:
            db.execute('INSERT INTO expression_snapshots VALUES (?,?,?,?,?,?,?)',
                (revised['extraction_id'], extraction_id, parent['source_asset_id'], revised['output_asset_id'],
                 self.runtime.relative(root), encoded(revised), parent['inventory_json']))
        return content, revised
