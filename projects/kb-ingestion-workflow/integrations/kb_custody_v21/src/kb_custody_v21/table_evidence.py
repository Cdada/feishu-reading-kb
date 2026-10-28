"""Derived, source-bound HTML table inventory; never repair OCR or rewrite a source.

MinerU tables are HTML inside Markdown. CommonMark identifies real HTML blocks
so examples inside code fences are not mistaken for evidence. Offsets always
refer to the original Unicode string, including tags and entity spellings.
"""
import hashlib
from html.parser import HTMLParser
import re

from markdown_it import MarkdownIt

VERSION = 'table-evidence-v1'
MAX_TABLE_CHARACTERS = 12000
MAX_CELLS = 4000
MAX_GRID = 16000
MAX_COLUMNS = 200

ISSUES = {
    'unclosed_table': '表格标签未闭合', 'nested_table': '含嵌套表格',
    'invalid_structure': '行或单元格结构不完整', 'invalid_span': '合并单元格范围无效',
    'overlap': '合并单元格相互重叠', 'uneven_rows': '行列数不一致或存在缺格',
    'empty_table': '没有可读取的单元格', 'bounds': '表格结构超过处理上限',
    'unsupported_markup': '含未支持的表内标签',
    'excluded_layout': '表格与已排除的页眉页脚范围重叠',
}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def page_numbers(pages, start, end):
    return sorted({p['page'] for p in pages if p['char_start'] < end and start < p['char_end']})


class TableParser(HTMLParser):
    """Bounded structural parsing. Ambiguity is recorded, never auto-corrected."""
    def __init__(self, text, base):
        super().__init__(convert_charrefs=True)
        self.text, self.base = text, base
        self.lines = [0] + [m.end() for m in re.finditer('\n', text)]
        self.tables, self.table, self.cell = [], None, None
        self.depth, self.row, self.column, self.in_row = 0, 0, 0, False
        self.header = False

    def char_position(self):
        line, column = self.getpos()
        return self.base + self.lines[line-1] + column

    def tag_end(self):
        # HTML permits whitespace before the closing bracket. Measure the
        # actual spelling instead of assuming the length of </td>.
        return self.base+self.text.index('>', self.char_position()-self.base)+1

    def issue(self, key):
        if key not in self.table['issues']:
            self.table['issues'].append(key)

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.depth += 1
            if self.table is not None:
                self.issue('nested_table')
                return
            self.table = dict(char_start=self.char_position(), cells=[], issues=[], grid={}, header_rows=[])
            self.row, self.column, self.in_row, self.header = 0, 0, False, False
            return
        if self.table is None:
            return
        if self.depth != 1:
            return
        if tag == 'thead':
            self.header = True
        elif tag == 'tr':
            if self.in_row or self.cell:
                self.issue('invalid_structure')
            self.row += 1
            self.column, self.in_row = 1, True
        elif tag in ('td', 'th'):
            if not self.in_row or self.cell:
                self.issue('invalid_structure')
            if len(self.table['cells']) >= MAX_CELLS:
                self.issue('bounds')
                return
            spans = {}
            for key in ('rowspan', 'colspan'):
                values = [v for k, v in attrs if k == key]
                raw = values[0] if values else '1'
                if len(values) > 1 or not raw or not re.fullmatch(r'[0-9]{1,4}', raw) or not 1 <= int(raw) <= MAX_COLUMNS:
                    self.issue('invalid_span')
                    raw = '1'  # Used only for a flagged diagnostic grid.
                spans[key] = int(raw)
            grid = self.table['grid']
            while (self.row, self.column) in grid:
                self.column += 1
            if (self.column+spans['colspan']-1 > MAX_COLUMNS or self.row > MAX_CELLS
                    or len(grid)+spans['rowspan']*spans['colspan'] > MAX_GRID):
                self.issue('bounds')
                return
            self.cell = dict(row=self.row, column=self.column, tag=tag,
                             header=tag == 'th' or self.header, char_start=self.char_position(), **spans)
            self.cell['_text'] = []
            self.cell['_tag'] = tag
            number = len(self.table['cells'])+1
            for r in range(self.row, self.row+spans['rowspan']):
                for c in range(self.column, self.column+spans['colspan']):
                    if (r, c) in grid:
                        self.issue('overlap')
                    grid[r, c] = number
            self.column += spans['colspan']
        elif tag == 'br' and self.cell:
            self.cell['_text'].append('\n')
        elif tag not in {'tbody', 'tfoot', 'caption', 'p', 'div', 'span', 'sup', 'sub', 'b', 'i', 'strong', 'em', 'br'}:
            self.issue('unsupported_markup')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag != 'br':
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.cell and self.depth == 1:
            self.cell['_text'].append(data)

    def handle_endtag(self, tag):
        if self.table is None:
            return
        if tag == 'table':
            self.depth -= 1
            if self.depth == 0:
                self.finish(self.tag_end())
            return
        if self.depth != 1:
            return
        if tag in ('td', 'th'):
            if not self.cell or self.cell['_tag'] != tag:
                self.issue('invalid_structure')
            if self.cell:
                self.cell['char_end'] = self.tag_end()
                self.cell['text'] = ''.join(self.cell.pop('_text')).strip()
                self.cell.pop('_tag')
                self.table['cells'].append(self.cell)
                self.cell = None
        elif tag == 'tr':
            if not self.in_row or self.cell:
                self.issue('invalid_structure')
            self.in_row = False
        elif tag == 'thead':
            self.header = False

    def finish(self, end):
        t = self.table
        if self.in_row or self.cell:
            self.issue('invalid_structure')
        if not t['cells']:
            self.issue('empty_table')
        grid = t.pop('grid')
        columns = max((c for _, c in grid), default=0)
        if not grid or max((r for r, _ in grid), default=0) != self.row or len(grid) != self.row*columns:
            self.issue('uneven_rows')
        t.update(char_end=end, rows=self.row, columns=columns)
        t['header_rows'] = [r for r in range(1, min(self.row, MAX_CELLS)+1)
                            if (cells := [c for c in t['cells'] if c['row'] == r]) and all(c['header'] for c in cells)]
        self.tables.append(t)
        self.table, self.cell = None, None

    def close(self):
        super().close()
        if self.table is not None:
            self.issue('unclosed_table')
            self.finish(self.base+len(self.text))


def caption_before(text, start, floor):
    # MinerU writes caption, then table notes, then the table. Keep those exact
    # paragraphs together, but never reach across another table or PDF page.
    prefix = text[max(floor, start-1200):start]
    matches = list(re.finditer(r'(?m)^(?:表\s*\d|续表\s*\d|Table\s+\d)[^\n]*', prefix, re.I))
    if not matches:
        return start, '', '', False
    m = matches[-1]
    between = prefix[m.end():].strip()
    if between and not all(re.match(r'^(?:注|说明|单位|资料来源|数据来源|Note|Source|Unit)\s*[:：.\d ]', p.strip(), re.I)
                           for p in re.split(r'\n\s*\n', between)):
        return start, '', '', False
    caption = m.group().strip()
    identity = re.match(r'(续表|表|Table)\s*(\d+(?:[.\-]\d+)*[A-Za-z]?)', caption, re.I)
    continued = bool(re.search(r'续表|[（(]\s*(?:续|续表|continued)\s*[)）]', caption, re.I))
    return max(floor, start-1200)+m.start(), caption, identity[2].lower(), continued


def inventory(text, pages=(), excluded_ranges=()):
    if '<table' not in text.lower():
        return dict(version=VERSION, source_sha256=digest(text), offset_unit='unicode_code_point',
                    range_convention='half_open', tables=[])
    line_starts = [0]+[m.end() for m in re.finditer('\n', text)]
    tables = []
    for token in MarkdownIt('commonmark', {'html': True}).parse(text):
        if token.type != 'html_block' or '<table' not in token.content.lower():
            continue
        a = line_starts[token.map[0]]
        z = line_starts[token.map[1]] if token.map[1] < len(line_starts) else len(text)
        parser = TableParser(text[a:z], a)
        parser.feed(text[a:z]); parser.close()
        for t in parser.tables:
            if tables and t['char_start'] < tables[-1]['char_end']:
                continue
            if 'unclosed_table' in t['issues']:
                # CommonMark ends an HTML block at a blank line; a valid HTML
                # table may continue across that boundary. Feed through whole
                # closing tags until the outer table closes (nested tables are
                # still flagged). Fences/comments outside tables remain ignored.
                tail = text[t['char_start']:]
                full = TableParser(tail, t['char_start'])
                cursor = 0
                for end in re.finditer(r'</table\s*>', tail, re.I):
                    full.feed(tail[cursor:end.end()]); cursor = end.end()
                    if full.tables:
                        break
                if not full.tables:
                    full.feed(tail[cursor:]); full.close()
                t = full.tables[0]
            tables.append(t)
    for i, t in enumerate(tables):
        a, z = t['char_start'], t['char_end']
        page_floor = max((p['char_start'] for p in pages if p['char_start'] <= a < p['char_end']), default=0)
        floor = max(tables[i-1]['char_end'] if i else 0, page_floor)
        start, caption, number, continued = caption_before(text, a, floor)
        if any(start < r['char_end'] and r['char_start'] < z for r in excluded_ranges):
            t['issues'].append('excluded_layout')
            start, caption, number, continued = a, '', '', False
        t.update(id=f'T{i+1:03d}', evidence_start=start, caption=caption, number=number,
                 continued=continued, physical_pages=page_numbers(pages, a, z),
                 sha256=digest(text[a:z]), content_verification='not_checked_against_pdf',
                 header_status='explicit' if t['header_rows'] else 'not_marked',
                 structure_status='needs_review' if t['issues'] else 'consistent',
                 qa_usable=not t['issues'] and z-start <= MAX_TABLE_CHARACTERS,
                 continuation_of=None, continuation_status='not_requested')
        if continued:
            t['continuation_status'] = 'needs_review'
            previous = tables[i-1] if i else None
            def heading(table):
                return [(c['row'], c['column'], c['rowspan'], c['colspan'], c['text'])
                        for c in table['cells'] if c['row'] in table['header_rows']]
            def title(table):
                s = re.sub(r'[（(]\s*(?:续|续表|continued)\s*[)）]', '', text[table['evidence_start']:table['char_start']], flags=re.I)
                s = re.sub(r'^Table(?=\s)', 'Table', s.replace('续表', '表'), flags=re.I)
                # Unit case and word boundaries carry meaning: mW != MW,
                # and m s != ms. Normalize spacing without erasing boundaries.
                return re.sub(r'[ \t]+', ' ', s).strip()
            # Only exact captions (including units), explicit identical header
            # rows and adjacent physical pages permit linking. No row merging.
            if (previous and previous['structure_status'] == t['structure_status'] == 'consistent'
                    and len(previous['physical_pages']) == len(t['physical_pages']) == 1
                    and previous['physical_pages'][0]+1 == t['physical_pages'][0]
                    and number == previous['number'] and title(t) == title(previous)
                    and heading(t) and heading(t) == heading(previous)):
                t['continuation_of'] = previous['continuation_of'] or previous['id']
                t['continuation_status'] = 'linked_explicit_continuation'
    return dict(version=VERSION, source_sha256=digest(text), offset_unit='unicode_code_point',
                range_convention='half_open', tables=tables)


def summaries(record):
    return [{k: v for k, v in t.items() if k not in ('cells', 'header_rows')} for t in record['tables']]


def context_for(tables, start, end):
    return [t for t in tables if t['evidence_start'] < end and start < t['char_end']]


def notice(tables):
    lines = []
    for t in tables:
        line = t['id']+' '+(t['caption'] or '未识别表名')+f"（{t['rows']} 行 × {t['columns']} 列）"
        if t['physical_pages']:
            line += '；PDF 物理页 '+','.join(map(str, t['physical_pages']))
        line += '；结构检查通过' if t['structure_status'] == 'consistent' else '；结构待核查：'+'、'.join(ISSUES[v] for v in t['issues'])
        if t['header_status'] == 'not_marked':
            line += '；表头未被识别器明确标记，需结合原表判断'
        line += '；数值和单位尚未对照 PDF 原页核验'
        if not t['qa_usable'] and not t['issues']:
            line += '；表格过长，仅提供原表定位，暂不生成表内数值回答'
        if t['continuation_of']:
            line += '；明确续接 '+t['continuation_of']+'（各片段分别保留）'
        elif t['continued']:
            line += '；疑似续表，尚未建立跨页关系'
        lines.append(line)
    return '\n'.join(lines)
