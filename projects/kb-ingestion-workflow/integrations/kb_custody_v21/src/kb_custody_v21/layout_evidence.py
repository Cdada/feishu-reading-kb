"""Keep physical-page furniture out of claims without changing source bytes.

Only typed, positioned, repeated headers/footers (and positioned page numbers)
are excluded. Footnotes, uncertain blocks and ordinary repeated prose remain.
"""
from collections import defaultdict
import hashlib
import re

from .contracts import ContractError

REFERENCE = 'layout-evidence/v1'
KINDS = {'header', 'footer', 'page_header', 'page_footer', 'page_number', 'page_footnote'}
INSTRUCTION = ('evidence_policy.excluded_ranges 是已核对的固定版面信息，不能作为导读、笔记、知识卡片的正文证据，'
    '不得引用或把它与相邻正文拼接作引文。原文为完整保留版本，不代表每段都适合作为证据。'
    '脚注、参考文献、补充说明和图表注释仍须阅读；页面位置本身不构成排除理由。'
    '期刊信息优先从首页或题录记录一次，不能用重复页眉支持文章观点。')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def checked_blocks(mapping, text):
    blocks = mapping.get('layout_blocks', [])
    if not isinstance(blocks, list):
        raise ContractError('layout blocks must be a list')
    previous = 0
    pages = {p['page_index']: p['text_ranges'] for p in mapping.get('page_mappings', [])}
    for b in blocks:
        if (not isinstance(b, dict) or set(b) != {'kind', 'page_index', 'char_start', 'char_end', 'bbox', 'sha256'}
                or b['kind'] not in KINDS or type(b['page_index']) is not int
                or type(b['char_start']) is not int or type(b['char_end']) is not int
                or not 0 <= previous <= b['char_start'] < b['char_end'] <= len(text)
                or digest(text[b['char_start']:b['char_end']]) != b['sha256']
                or not any(r['char_start'] <= b['char_start'] < b['char_end'] <= r['char_end']
                           for r in pages.get(b['page_index'], []))):
            raise ContractError('layout block no longer matches its stored physical page')
        box = b['bbox']
        if box is not None and (not isinstance(box, list) or len(box) != 4
                or any(type(n) not in (int, float) or not 0 <= n <= 1000 for n in box)
                or box[0] >= box[2] or box[1] >= box[3]):
            raise ContractError('invalid normalized layout position')
        previous = b['char_end']
    return blocks


def evidence_policy(mapping, text, authorized=None):
    if 'layout_blocks' not in mapping:
        return None  # Legacy source: no invented layout classification.
    blocks = checked_blocks(mapping, text)
    repeated = defaultdict(set)
    candidates = []
    for b in blocks:
        value = text[b['char_start']:b['char_end']].strip()
        box, kind = b['bbox'], b['kind']
        # Annotation markers protect even a short note mislabeled as a footer.
        if (box is None or len(value) > 160 or re.match(r'^(?:[①-⑳*†‡]|[（(\[]\d+[）)\]]|注[：:]|说明[：:])', value)):
            continue
        edge = (kind in {'header', 'page_header'} and box[3] <= 150
                or kind in {'footer', 'page_footer', 'page_number'} and box[1] >= 850)
        if not edge:
            continue
        key = (kind, re.sub(r'\s+', '', value))
        repeated[key].add(b['page_index'])
        candidates.append((b, value, key))
    excluded = []
    for b, value, key in candidates:
        if (b['kind'] == 'page_number' and re.fullmatch(r'[0-9ivxlcdmIVXLCDM]+', value)
                or b['kind'] != 'page_number' and len(repeated[key]) >= 2):
            a, z = b['char_start'], b['char_end']
            for start, end in authorized if authorized is not None else [[0, len(text)]]:
                if max(a, start) < min(z, end):
                    excluded.append(dict(char_start=max(a,start), char_end=min(z,end), kind=b['kind'], page_index=b['page_index']))
    return dict(reference=REFERENCE, body_sha256=digest(text), excluded_ranges=excluded, instruction=INSTRUCTION)


def checked_policy(policy, text):
    if policy is None:
        return []
    if (not isinstance(policy, dict) or set(policy) != {'reference', 'body_sha256', 'excluded_ranges', 'instruction'}
            or policy['reference'] != REFERENCE or policy['body_sha256'] != digest(text)
            or policy['instruction'] != INSTRUCTION or not isinstance(policy['excluded_ranges'], list)):
        raise ContractError('layout evidence policy no longer matches source')
    previous = 0
    for r in policy['excluded_ranges']:
        if (not isinstance(r, dict) or set(r) != {'char_start', 'char_end', 'kind', 'page_index'}
                or type(r['char_start']) is not int or type(r['char_end']) is not int
                or not previous <= r['char_start'] < r['char_end'] <= len(text)
                or r['kind'] not in KINDS - {'page_footnote'} or type(r['page_index']) is not int or r['page_index'] < 0):
            raise ContractError('invalid excluded layout range')
        previous = r['char_end']
    return policy['excluded_ranges']


def require_citable(policy, text, start, end):
    if any(start < r['char_end'] and r['char_start'] < end for r in checked_policy(policy, text)):
        raise ContractError('quotation uses excluded page header/footer; cite substantive source text')
