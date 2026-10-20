"""Import a complete MinerU expression into the existing conversion ledger."""
import csv
import hashlib
import json
from pathlib import Path
import shutil

from .contracts import ContractError
from .mineru_chunks import VERSION, checked_snapshot, file_digest, relative_file


def checked_mineru_map(mapping, text, source_sha, root):
    expected = dict(schema_version=VERSION, source_sha256=source_sha,
        output_sha256=hashlib.sha256(text.encode()).hexdigest(), extractor='mineru-pipeline',
        extractor_version='3.1.15', mode='auto_with_one_ocr_remedy', text_encoding='UTF-8',
        offset_unit='unicode_code_point', range_convention='half_open', newline_policy='as_stored',
        text_length=len(text), locator_precision='page')
    if not isinstance(mapping, dict) or any(mapping.get(k) != v for k, v in expected.items()):
        raise ContractError('MinerU page mapping identity changed')
    pages = mapping.get('page_mappings')
    if not isinstance(pages, list) or not 1 <= len(pages) <= 100:
        raise ContractError('MinerU physical page coverage missing')
    offset, spans, separators = 0, [], []
    for index, page in enumerate(pages):
        if (not isinstance(page, dict) or type(page.get('page_index')) is not int or page['page_index'] != index
                or page.get('page_label') is not None or page.get('method') != 'MinerU.content_list'
                or not isinstance(page.get('confidence_note'), str) or not page['confidence_note']):
            raise ContractError('MinerU page identity invalid')
        if index:
            if text[offset:offset+2] != '\n\n':
                raise ContractError('MinerU page separator changed')
            separators.append(dict(kind='page_separator', char_start=offset, char_end=offset+2))
            offset += 2
        start, ranges = offset, page.get('text_ranges')
        if not isinstance(ranges, list) or len(ranges) > 1:
            raise ContractError('MinerU page text range invalid')
        if ranges:
            r = ranges[0]
            if (not isinstance(r, dict) or set(r) != {'char_start', 'char_end'}
                    or type(r['char_start']) is not int or type(r['char_end']) is not int
                    or r['char_start'] != start or not start < r['char_end'] <= len(text)):
                raise ContractError('MinerU page text range changed')
            offset = r['char_end']
            spans.append(dict(mapping_id=f'page-text-{index}', **r, source_page_index=index, section_id=None, paragraph_id=None))
        if page.get('text_sha256') != hashlib.sha256(text[start:offset].encode()).hexdigest():
            raise ContractError('MinerU page text digest changed')
    if offset != len(text) or mapping.get('text_mappings') != spans or mapping.get('transformations') != separators:
        raise ContractError('MinerU page map does not cover the full stored text')
    images = mapping.get('images')
    if not isinstance(images, dict):
        raise ContractError('MinerU image map missing')
    from .image_links import image_spans
    refs = image_spans(text)
    if {r['original_path'] for r in refs} != set(images):
        raise ContractError('MinerU image references differ from the retained inventory')
    for target, record in images.items():
        path = relative_file(root, target)
        if (not isinstance(record, dict) or type(record.get('page_index')) is not int
                or not 0 <= record['page_index'] < len(pages)
                or path.stat().st_size != record.get('size_bytes') or file_digest(path) != record.get('sha256')):
            raise ContractError('MinerU image page or bytes changed')
        ranges = pages[record['page_index']]['text_ranges']
        if not ranges or any(not ranges[0]['char_start'] <= r['char_start'] < r['char_end'] <= ranges[0]['char_end']
                             for r in refs if r['original_path'] == target):
            raise ContractError('MinerU image reference lies outside its mapped physical page')
    if not isinstance(mapping.get('issues'), list) or not mapping['issues']:
        raise ContractError('MinerU extraction limitations must remain explicit')
    from .layout_evidence import checked_blocks
    checked_blocks(mapping, text)
    result = {k: mapping[k] for k in ('extractor', 'extractor_version', 'mode', 'locator_precision',
             'page_mappings', 'text_mappings', 'transformations', 'issues')}
    if 'layout_blocks' in mapping:
        result['layout_blocks'] = mapping['layout_blocks']
    return result, images


def stage_pdf_result(conversion, entry, batch, result):
    result = conversion.runtime.checked(Path(result))
    proof = json.loads((result.parent / 'complete.json').read_bytes())
    checked_snapshot(result, proof['files'])
    content = (result / 'source.md').read_text(encoding='utf-8')
    mapping = json.loads((result / 'source.pages.json').read_bytes())
    checked_mineru_map(mapping, content, entry['sha256'], result)
    manifest = batch / '_batch_manifest.csv'
    with manifest.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    if len(rows) != 1:
        raise ContractError('MinerU import requires one accepted original')
    root = conversion.runtime.checked(batch / '04_知识库输入文件/custody-mineru')
    shutil.copytree(result, root)
    (root / 'candidate.md').write_text('# PDF 转换预检查\n\n' + content, encoding='utf-8')
    rows[0].update(processing_status='processed', kb_folder=root.relative_to(batch).as_posix(),
        primary_input=(root / 'candidate.md').relative_to(batch).as_posix(),
        auxiliary_input=(root / 'source.md').relative_to(batch).as_posix(),
        group_role='', classification='混合型', quality_status='',
        conversion_method='custody_mineru_pdf_v1',
        notes='完整物理页和图片引用已检查；原图未识读，OCR/公式及跨页表格语义未人工校对。')
    with manifest.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return True
