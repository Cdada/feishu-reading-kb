"""Validate local PDF sidecars before assigning page-level precision."""
import hashlib

from .contracts import ContractError


def checked_pdf_map(mapping, text, source_sha256):
    def require(condition):
        if not condition:
            raise ContractError('PDF page mapping identity or ranges invalid')

    require(isinstance(mapping, dict))
    expected = dict(schema_version='local-pdf-text-map/v1', source_sha256=source_sha256,
        output_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(), extractor='pypdf', mode='txt',
        text_encoding='UTF-8', offset_unit='unicode_code_point', range_convention='half_open',
        newline_policy='as_stored', text_length=len(text), locator_precision='page')
    require(all(mapping.get(key) == value for key, value in expected.items()))
    require(isinstance(mapping.get('extractor_version'), str) and bool(mapping['extractor_version']))
    pages = mapping.get('page_mappings')
    require(isinstance(pages, list) and bool(pages))
    offset, spans, separators = 0, [], []
    for index, page in enumerate(pages):
        require(isinstance(page, dict) and type(page.get('page_index')) is int and page['page_index'] == index)
        require(page.get('page_label') is None and page.get('method') == 'pypdf.extract_text')
        require(isinstance(page.get('confidence_note'), str) and bool(page['confidence_note']))
        if index:
            require(text[offset:offset + 2] == '\n\n')
            separators.append(dict(kind='page_separator', char_start=offset, char_end=offset + 2))
            offset += 2
        ranges = page.get('text_ranges')
        require(isinstance(ranges, list) and len(ranges) <= 1)
        start = offset
        if ranges:
            r = ranges[0]
            require(isinstance(r, dict) and type(r.get('char_start')) is int and type(r.get('char_end')) is int)
            require(r['char_start'] == start and start < r['char_end'] <= len(text))
            offset = r['char_end']
            spans.append(dict(mapping_id=f'page-text-{index}', char_start=start, char_end=offset,
                              source_page_index=index, section_id=None, paragraph_id=None))
        require(page.get('text_sha256') == hashlib.sha256(text[start:offset].encode('utf-8')).hexdigest())
    require(offset == len(text) and mapping.get('text_mappings') == spans and mapping.get('transformations') == separators)
    require(isinstance(mapping.get('issues'), list) and bool(mapping['issues']) and
            all(isinstance(item, str) and item for item in mapping['issues']))
    # Return only extraction metadata; never allow a sidecar to replace asset or
    # extraction IDs owned by the custody registry.
    return {key: mapping[key] for key in ('extractor', 'extractor_version', 'mode', 'locator_precision',
             'page_mappings', 'text_mappings', 'transformations', 'issues')}
