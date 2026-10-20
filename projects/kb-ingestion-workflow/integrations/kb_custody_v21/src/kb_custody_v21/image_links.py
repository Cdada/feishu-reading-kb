"""CommonMark image spans and byte-preserving, audited expression revisions."""
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import image as image_rule

from .contracts import ContractError, new_id
from .conversion import sha


def image_spans(text, *, allow_external=False):
    parser = MarkdownIt('commonmark')
    if allow_external:
        # Inventory only: accept all destinations without fetching or rendering.
        parser.validateLink = lambda target: True
    def capture(state, silent):
        start = state.pos
        matched = image_rule(state, silent)
        if matched and not silent:
            state.tokens[-1].meta['span'] = (start, state.pos)
        return matched
    parser.inline.ruler.at('image', capture)
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    spans = []
    for token in parser.parse(text):
        if token.type != 'inline' or not any(c.type == 'image' for c in token.children or []):
            continue
        block_start, block_end = offsets[token.map[0]], offsets[token.map[1]]
        block = text[block_start:block_end]
        positions = {}
        direct = block.find(token.content)
        if direct < 0:
            if len(block) > 65536:
                raise ContractError('complex image block requires technical review')
            for a, b, size in SequenceMatcher(None, block, token.content, autojunk=False).get_matching_blocks():
                positions.update((b + n, block_start + a + n) for n in range(size))
        for child in token.children or []:
            if child.type != 'image':
                continue
            start, end = child.meta['span']
            if direct >= 0:
                begin, finish = block_start + direct + start, block_start + direct + end
            else:
                indices = [positions.get(n) for n in range(start, end)]
                if not indices or None in indices or indices != list(range(indices[0], indices[0] + len(indices))):
                    raise ContractError('image syntax cannot be mapped precisely to stored text; manual review required')
                begin, finish = indices[0], indices[-1] + 1
            target = child.attrGet('src')
            parsed = urlsplit(target)
            path = PurePosixPath(unquote(parsed.path))
            if not allow_external and (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or path.is_absolute() or '..' in path.parts):
                raise ContractError('external or unsafe image target forbidden')
            spans.append(dict(char_start=begin, char_end=finish, original_path=target if allow_external else path.as_posix(),
                              caption=child.content))
    return spans


def rewrite_images(content, mapping, replacements):
    """replacements: original target -> {asset_id, local_path, representation_asset_id}."""
    text = content.decode('utf-8')
    spans = image_spans(text)
    if set(replacements) != {s['original_path'] for s in spans}:
        raise ContractError('every image occurrence must have exactly one policy binding')
    result, transformed, mapped = [], [], []
    cursor, length = 0, 0
    for span in spans:
        target = replacements[span['original_path']]
        unchanged = text[cursor:span['char_start']]
        result.append(unchanged)
        length += len(unchanged)
        if target['representation_asset_id'] is None:
            replacement = f"图：{span['caption']}（原图资产：{target['asset_id']}，需手动获取）"
        else:
            local = target['local_path']
            path = PurePosixPath(local)
            if path.is_absolute() or '..' in path.parts or any(c in local for c in '()\n\r') or urlsplit(local).scheme:
                raise ContractError('invalid package image representation path')
            label = span['caption'].replace('[', r'\[').replace(']', r'\]')
            if target.get('placeholder'):
                label += '（占位，细节需原图）'
            replacement = f'![{label}]({local})'
        result.append(replacement)
        mapped.append(dict(image_asset_id=target['asset_id'], char_start=length, char_end=length+len(replacement),
                           original_path=span['original_path'], caption=span['caption'], source_page_index=None,
                           local_representation_asset_id=target['representation_asset_id']))
        if mapping.get('extractor') == 'mineru-pipeline':
            originals = [m for m in mapping['image_mappings'] if (m['char_start'],m['char_end']) == (span['char_start'],span['char_end'])]
            if len(originals) != 1:
                raise ContractError('MinerU image occurrence lost its page binding')
            mapped[-1]['source_page_index'] = originals[0]['source_page_index']
        transformed.append(dict(kind='image_link_policy', old_range=[span['char_start'],span['char_end']],
                                new_range=[length,length+len(replacement)], original_path=span['original_path']))
        length += len(replacement)
        cursor = span['char_end']
    result.append(text[cursor:])
    output = ''.join(result).encode('utf-8')
    revised = deepcopy(mapping)
    if output != content:
        revised.update(extraction_id=new_id('extraction'), output_asset_id=new_id('asset'),
                       output_sha256=sha(output), text_length=len(output.decode()),
                       locator_precision='document', page_mappings=[], text_mappings=[],
                       transformations=mapping['transformations'] + transformed)
        if mapping.get('extractor') == 'mineru-pipeline' and mapping.get('locator_precision') == 'page':
            # Image-path changes alter string lengths. Translate the saved page
            # boundaries by exact edit deltas, retaining physical page identity.
            def translate(position):
                delta = 0
                for edit in transformed:
                    start, end = edit['old_range']
                    if start < position < end:
                        raise ContractError('image edit crosses a physical page boundary')
                    if end <= position:
                        a, b = edit['new_range']
                        delta += (b-a) - (end-start)
                return position + delta
            pages, text_spans = deepcopy(mapping['page_mappings']), []
            result_text = output.decode('utf-8')
            for page in pages:
                for r in page['text_ranges']:
                    r.update(char_start=translate(r['char_start']), char_end=translate(r['char_end']))
                    text_spans.append(dict(mapping_id=f"page-text-{page['page_index']}", **r,
                        source_page_index=page['page_index'], section_id=None, paragraph_id=None))
                page_text = ''.join(result_text[r['char_start']:r['char_end']] for r in page['text_ranges'])
                page['text_sha256'] = sha(page_text.encode())
            revised.update(locator_precision='page', page_mappings=pages, text_mappings=text_spans)
            if 'layout_blocks' in mapping:
                from .layout_evidence import checked_blocks
                checked_blocks(mapping, text)
                for block in revised['layout_blocks']:
                    block.update(char_start=translate(block['char_start']), char_end=translate(block['char_end']))
                    block['sha256'] = sha(result_text[block['char_start']:block['char_end']].encode())
                checked_blocks(revised, result_text)
    revised['image_mappings'] = mapped
    return output, revised
