"""Local PDF text-layer extraction with offsets measured during concatenation."""
import hashlib
from pathlib import Path

import pypdf
from pypdf import PdfReader


def digest(data):
    return hashlib.sha256(data).hexdigest()


def join_pages(pages, source_sha256):
    """Keep extractor text verbatim; separators have no source-page claim."""
    parts, page_mappings, text_mappings, separators = [], [], [], []
    offset = 0
    for index, text in enumerate(pages):
        if index:
            separators.append(dict(kind='page_separator', char_start=offset, char_end=offset + 2))
            parts.append('\n\n')
            offset += 2
        start = offset
        parts.append(text)
        offset += len(text)
        ranges = [dict(char_start=start, char_end=offset)] if text else []
        page_mappings.append(dict(page_index=index, page_label=None, text_ranges=ranges,
            method='pypdf.extract_text', text_sha256=digest(text.encode('utf-8')),
            confidence_note='Text layer only; reading order, images and completeness require review.'
            if text else 'No text layer extracted; blank/scanned page is not distinguished.'))
        if text:
            text_mappings.append(dict(mapping_id=f'page-text-{index}', char_start=start, char_end=offset,
                source_page_index=index, section_id=None, paragraph_id=None))
    body = ''.join(parts)
    mapping = dict(schema_version='local-pdf-text-map/v1', source_sha256=source_sha256,
        output_sha256=digest(body.encode('utf-8')), extractor='pypdf', extractor_version=pypdf.__version__,
        mode='txt', text_encoding='UTF-8', offset_unit='unicode_code_point', range_convention='half_open',
        newline_policy='as_stored', text_length=len(body), locator_precision='page',
        page_mappings=page_mappings, text_mappings=text_mappings, transformations=separators,
        issues=['PDF text-layer extraction only: images, tables, layout and text completeness require review; no OCR was run.'])
    if any(not page['text_ranges'] for page in page_mappings):
        mapping['issues'].append('One or more pages have no extracted text; their text_ranges are empty.')
    return body, mapping


def extract_pdf_text(source: Path):
    # The approved route requires pypdf and does not silently choose another
    # extractor. Encryption needs a separate local workflow; no password guesses.
    data = source.read_bytes()
    from io import BytesIO
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise ValueError('PDF_ENCRYPTED: local text route does not accept encrypted PDFs')
    return join_pages((page.extract_text() or '' for page in reader.pages), digest(data))
