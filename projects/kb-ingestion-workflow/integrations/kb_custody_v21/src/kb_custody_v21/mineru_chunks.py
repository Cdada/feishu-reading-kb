"""Bounded, resumable PDF chunks. This module never starts MinerU itself.

The converter is injected by the explicitly authorized daily adapter. Completed
chunks are immutable and reusable across conversion tasks; attempts are retained.
Physical page offsets come from the split manifest and checked MinerU JSON.
"""
from dataclasses import asdict, dataclass
import hashlib
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil

from pypdf import PdfWriter
from pypdf.errors import PdfReadError

from .contracts import ContractError
from .daily_launcher import write_state
from .daily_store import Deferred
from .runtime import no_symlinks
from .pdf_compatibility import read_pdf


VERSION = 'mineru-page-chunks/v1'
MAX_JSON = 32 * 1024**2
MAX_IMAGE = 32 * 1024**2
MAX_OUTPUT = 256 * 1024**2


class ChunkFailure(Exception):
    """A bounded conversion/result failure, eligible for one OCR remedy."""


class PdfHeld(Exception):
    """This paper needs operator attention; other queue items may continue."""


@dataclass(frozen=True)
class PdfOptions:
    chunk_pages: int = 5
    max_pages: int = 100
    idle_seconds: int = 300
    startup_seconds: int = 1800
    chunk_seconds: int = 1800
    version: str = '3.1.15'

    def validate(self):
        for key, low, high in (('chunk_pages', 1, 10), ('max_pages', 1, 100),
                               ('startup_seconds', 30, 3600), ('chunk_seconds', 30, 3600)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ContractError('invalid bounded PDF option: ' + key)
        if self.idle_seconds != 300 or type(self.idle_seconds) is not int or self.version != '3.1.15':
            raise ContractError('PDF lifecycle/schema differs from the reviewed MinerU profile')


def file_digest(path):
    digest = hashlib.sha256()
    with no_symlinks(Path(path).absolute()).open('rb') as stream:
        for part in iter(lambda: stream.read(1024**2), b''):
            digest.update(part)
    return digest.hexdigest()


def read_json(path):
    path = no_symlinks(Path(path).absolute())
    if not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ChunkFailure('识别 JSON 缺失或超过本地大小限制')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as error:
        raise ChunkFailure('识别 JSON 格式不完整') from error


def relative_file(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ChunkFailure('识别附件路径无效')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or ':' in name:
        raise ChunkFailure('识别附件不能引用输出目录之外的文件')
    target = no_symlinks(root / path)
    if not target.is_file():
        raise ChunkFailure('识别结果引用的图片缺失')
    return target


def snapshot(root):
    result, total = {}, 0
    for path in sorted(root.rglob('*')):
        no_symlinks(path)
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_OUTPUT:
            raise ChunkFailure('单批识别结果超过本地大小限制')
        result[path.relative_to(root).as_posix()] = dict(size_bytes=size, sha256=file_digest(path))
    return result


def checked_snapshot(root, expected):
    if snapshot(root) != expected:
        raise ContractError('completed PDF chunk bytes changed; retain evidence for inspection')


def _strings(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(s, str) for s in value):
        raise ChunkFailure('识别内容列表结构变化')
    return value


class TableHTML(HTMLParser):
    """Retain table text/structure and expose embedded images to the inventory."""
    def __init__(self, image):
        super().__init__(convert_charrefs=True)
        self.image, self.parts, self.figures = image, [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            source = dict(attrs).get('src')
            self.figures.append(self.image(source))
            self.parts.append(f'（表格内图 {len(self.figures)}，见本页附图）')
            return
        if tag not in {'table','thead','tbody','tfoot','tr','td','th','p','div','span','br','sup','sub','b','i','strong','em'}:
            raise ChunkFailure('表格包含不支持的 HTML 标签')
        kept = []
        for key, value in attrs:
            if key in {'rowspan','colspan'}:
                if value is None or not value.isdigit() or not 1 <= int(value) <= 1000:
                    raise ChunkFailure('表格合并单元格结构无效')
                kept.append(f' {key}="{value}"')
        self.parts.append('<' + tag + ''.join(kept) + '>')

    def handle_endtag(self, tag):
        if tag not in {'img','table','thead','tbody','tfoot','tr','td','th','p','div','span','br','sup','sub','b','i','strong','em'}:
            raise ChunkFailure('表格包含不支持的 HTML 标签')
        if tag != 'img':
            self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        self.parts.append(escape(data))


def render_chunk(output, destination, *, first_page, page_count, stem, version):
    """Normalize only the installed content_list schema; never guess page IDs."""
    lists = sorted(output.rglob('*_content_list.json'))
    middles = sorted(output.rglob('*_middle.json'))
    if len(lists) != 1 or len(middles) != 1:
        raise ChunkFailure('单批必须包含唯一内容清单和完整页面清单')
    items, middle = read_json(lists[0]), read_json(middles[0])
    pages = middle.get('pdf_info') if isinstance(middle, dict) else None
    if (not isinstance(items, list) or not isinstance(pages, list) or len(pages) != page_count
            or middle.get('_version_name') != version
            or any(not isinstance(p, dict) or type(p.get('page_idx')) is not int
                   or p['page_idx'] != i for i, p in enumerate(pages))):
        raise ChunkFailure('识别版本、页面数或页序不一致')
    by_page = [[] for _ in pages]
    previous = -1
    for item in items:
        if not isinstance(item, dict):
            raise ChunkFailure('识别内容结构无效')
        index = item.get('page_idx')
        if type(index) is not int or not 0 <= index < page_count or index < previous:
            raise ChunkFailure('识别内容页码越界或顺序错误')
        previous = index
        by_page[index].append(item)
    destination.mkdir(parents=True, exist_ok=False)
    images, rendered, layout_pages = {}, [], []
    for index, records in enumerate(by_page):
        if not records and (pages[index].get('para_blocks') or pages[index].get('preproc_blocks')):
            raise ChunkFailure('页面已有识别块，但正文清单缺失')
        parts, layout_parts = [], {}
        def retain_image(reference, kind='table'):
            source = relative_file(lists[0].parent, reference)
            if source.stat().st_size > MAX_IMAGE or source.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp'}:
                raise ChunkFailure('识别图片过大或格式不支持')
            target = 'images/' + stem + '_' + hashlib.sha256(reference.encode()).hexdigest()[:20] + source.suffix.lower()
            physical = first_page + index
            if target in images and images[target]['page_index'] != physical - 1:
                raise ChunkFailure('同一识别图片被分配到了不同页面')
            if target not in images:
                path = destination / target
                path.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, path)
                images[target] = dict(sha256=file_digest(path), size_bytes=path.stat().st_size,
                                      page_index=physical - 1, kind=kind)
            return '![](' + target + ')'
        for item in records:
            kind = item.get('type')
            # MinerU 3.1.15 content_list v1 emits header/footer; page_* names
            # are retained for existing normalized inputs. Preserve their text.
            if kind in {'text', 'aside_text', 'header', 'footer', 'page_header',
                        'page_footer', 'page_footnote', 'page_number'}:
                value = item.get('text')
                if not isinstance(value, str):
                    raise ChunkFailure('识别文字字段缺失')
                level = item.get('text_level', 0)
                if type(level) is not int or not 0 <= level <= 6:
                    raise ChunkFailure('识别标题层级无效')
                if kind != 'text' and kind != 'aside_text':
                    layout_parts[len(parts)] = dict(kind=kind, bbox=item.get('bbox'))
                parts.append(('#' * level + ' ' if level else '') + value)
            elif kind in {'equation', 'interline_equation', 'seal'}:
                value, reference = item.get('text', ''), item.get('img_path')
                if not isinstance(value, str):
                    raise ChunkFailure('公式或印章文字字段无效')
                if reference and not reference.endswith('/'):
                    parts.append(retain_image(reference, kind))
                elif not value:
                    raise ChunkFailure('公式或印章既无识别文字也无图片')
                if value:
                    parts.append(value if kind == 'seal' else '$$\n' + value + '\n$$')
            elif kind == 'list':
                parts.append('\n'.join('- ' + s for s in _strings(item.get('list_items'))))
            elif kind == 'code':
                value = item.get('code_body', item.get('text'))
                if not isinstance(value, str):
                    raise ChunkFailure('识别代码字段缺失')
                parts.append('````\n' + value + '\n````')
                parts.extend(_strings(item.get('code_caption')))
                parts.extend(_strings(item.get('code_footnote')))
            elif kind in {'image', 'table', 'chart'}:
                reference = item.get('img_path')
                if reference:
                    parts.append(retain_image(reference, kind))
                elif kind != 'table' or not item.get('table_body'):
                    raise ChunkFailure('图表识别块缺少原图或表格内容')
                for field in (kind + '_caption', kind + '_footnote'):
                    parts.extend(_strings(item.get(field)))
                body = item.get('table_body') if kind == 'table' else None
                if body is not None:
                    if not isinstance(body, str):
                        raise ChunkFailure('表格内容结构无效')
                    table = TableHTML(retain_image)
                    table.feed(body); table.close()
                    parts.append(''.join(table.parts))
                    parts.extend(table.figures)
                # Chart's machine-generated content is retained in raw JSON;
                # do not present it as a separate verified image interpretation.
            else:
                raise ChunkFailure('暂不支持的识别块类型：' + str(kind))
        clean, blocks, offset = [], [], 0
        for part_index, part in enumerate(parts):
            value = part.strip()
            if not value:
                continue
            if clean:
                offset += 2
            if part_index in layout_parts:
                blocks.append(dict(layout_parts[part_index], page_index=first_page+index-1,
                    char_start=offset, char_end=offset+len(value), sha256=hashlib.sha256(value.encode()).hexdigest()))
            clean.append(value)
            offset += len(value)
        rendered.append('\n\n'.join(clean))
        layout_pages.append(blocks)
    record = dict(reference=VERSION, first_page=first_page, page_count=page_count,
                  pages=rendered, images=images, layout_pages=layout_pages)
    write_state(destination / 'pages.json', record)
    return record


class PdfChunks:
    def __init__(self, root, options, converter, *, check=lambda: None):
        options.validate()
        self.root, self.options, self.converter, self.check = no_symlinks(Path(root).absolute()), options, converter, check

    def import_repaired_chunk(self, source, source_sha, first_page, raw_output, *, operator_ref):
        """Explicit local repair only; retain failed attempts and model budgets.

        The caller must own the stopped daily service. This method never invokes
        the converter; the operator supplies corrected, versioned local output.
        """
        self.check()
        if not isinstance(operator_ref, str) or not operator_ref.strip() or len(operator_ref) > 1000:
            raise ContractError('explicit bounded operator repair reference required')
        if file_digest(source) != source_sha:
            raise ContractError('PDF repair original changed')
        root = self.root / source_sha
        manifest = read_json(root / 'manifest.json')
        if manifest['identity']['source_sha256'] != source_sha or manifest['identity']['options'] != asdict(self.options):
            raise ContractError('PDF repair profile differs from the original attempt')
        matches = [c for c in manifest['chunks'] if c['first_page'] == first_page]
        if len(matches) != 1 or matches[0]['completed'] is not None:
            raise ContractError('repair must select one unfinished original chunk')
        chunk = matches[0]
        if sum(r['state']=='failed' for r in chunk['attempts']) != 2:
            raise ContractError('manual PDF repair requires the exhausted two-attempt record')
        raw_output = no_symlinks(Path(raw_output).absolute())
        retained = snapshot(raw_output)
        number = len(chunk['attempts']) + 1
        directory = root / chunk['stem'] / str(number)
        chunk['attempts'].append(dict(number=number,state='repair_preparing',mode='operator_supplied',operator_ref=operator_ref))
        write_state(root / 'manifest.json', manifest)
        directory.mkdir(parents=True,exist_ok=False)
        shutil.copytree(raw_output,directory/'raw')
        checked_snapshot(directory/'raw',retained)
        render_chunk(directory/'raw',directory/'normalized',first_page=chunk['first_page'],page_count=chunk['page_count'],
                     stem=chunk['stem'],version=self.options.version)
        chunk['attempts'][-1]['state'] = 'repaired'
        chunk['completed'] = dict(directory=directory.relative_to(root).as_posix(),files=snapshot(directory))
        write_state(root / 'manifest.json',manifest)
        return dict(first_page=first_page,operator_ref=operator_ref,new_conversion_calls=0)

    def convert(self, source, source_sha, *, progress=lambda event: None):
        self.check()
        if file_digest(source) != source_sha:
            raise ContractError('accepted PDF changed before conversion')
        root = self.root / source_sha
        root.mkdir(parents=True, exist_ok=True)
        try:
            reader, compatibility, derived = read_pdf(source)
            if reader.is_encrypted:
                raise PdfHeld('PDF 已加密，请先提供可读取的原件。')
            count = len(reader.pages)
        except (PdfReadError, ValueError, OSError) as error:
            raise PdfHeld('PDF 无法完整读取，请检查或重新提供原件。') from error
        if not 1 <= count <= self.options.max_pages:
            raise PdfHeld(f'首版支持 1–{self.options.max_pages} 页论文，请将较长材料交 Codex 处理。')
        if compatibility:
            if compatibility['source_sha256'] != source_sha:
                raise ContractError('PDF compatibility source binding changed')
            audit_path = root / 'parser-compatibility.json'
            derived_path = root / 'compatible-input.pdf'
            if audit_path.exists() and read_json(audit_path) != compatibility:
                raise ContractError('saved PDF compatibility record changed')
            if derived_path.exists():
                if file_digest(derived_path) != compatibility['derived_sha256']:
                    raise ContractError('saved compatible PDF changed')
            else:
                with derived_path.open('xb') as stream:
                    stream.write(derived)
            if not audit_path.exists():
                write_state(audit_path, compatibility)
        identity = dict(reference=VERSION, source_sha256=source_sha, pages=count, options=asdict(self.options))
        path = root / 'manifest.json'
        if path.exists():
            manifest = read_json(path)
            if manifest.get('identity') != identity:
                raise ContractError('saved PDF chunk identity/profile changed; explicit revision required')
        else:
            manifest = dict(identity=identity, chunks=[])
            for start in range(0, count, self.options.chunk_pages):
                end = min(count, start + self.options.chunk_pages)
                stem = f'p{start+1:04d}-{end:04d}'
                manifest['chunks'].append(dict(stem=stem, first_page=start+1, page_count=end-start, attempts=[], completed=None))
            write_state(path, manifest)
        expected = [(f'p{start+1:04d}-{min(count,start+self.options.chunk_pages):04d}', start+1,
                     min(count,start+self.options.chunk_pages)-start) for start in range(0,count,self.options.chunk_pages)]
        chunks = manifest.get('chunks')
        if (not isinstance(chunks, list) or len(chunks) != len(expected)
                or any(not isinstance(c, dict) or (c.get('stem'),c.get('first_page'),c.get('page_count')) != e
                       or not isinstance(c.get('attempts'), list) for c,e in zip(chunks,expected))):
            raise ContractError('PDF chunk manifest no longer covers the original pages')
        for chunk in manifest['chunks']:
            self.check()
            if any(not isinstance(r,dict) or type(r.get('number')) is not int or r['number'] != i
                   or r.get('state') not in {'preparing','started','paused','interrupted','failed','complete','repair_preparing','repaired'}
                   or r.get('mode') not in {'auto','ocr','operator_supplied'}
                   for i,r in enumerate(chunk['attempts'],1)):
                raise ContractError('PDF attempt record changed')
            if chunk['completed']:
                saved = Path(chunk['completed']['directory'])
                completed = {str(r['number']) for r in chunk['attempts'] if r['state'] in {'complete','repaired'}}
                if (saved.is_absolute() or '..' in saved.parts or len(saved.parts) != 2
                        or saved.parts[0] != chunk['stem'] or saved.parts[1] not in completed):
                    raise ContractError('completed PDF chunk path changed')
                checked_snapshot(root / saved, chunk['completed']['files'])
        def report(stage, chunk=None):
            progress(dict(source_sha=source_sha, total_pages=count,
                completed_pages=sum(c['page_count'] for c in chunks if c['completed']), stage=stage,
                compatibility=compatibility['reference'] if compatibility else None,
                first_page=chunk['first_page'] if chunk else None,
                last_page=chunk['first_page']+chunk['page_count']-1 if chunk else None))
        report('converting')
        for chunk in chunks:
            self.check()
            if chunk['completed']:
                continue
            completed = False
            # Inspect retained output before retrying an interrupted attempt.
            for prior in chunk['attempts']:
                if prior['state'] not in {'started', 'paused', 'preparing'}:
                    continue
                directory = root / chunk['stem'] / str(prior['number'])
                try:
                    normalized = directory / 'normalized'
                    if normalized.exists():
                        import uuid
                        normalized.rename(directory / ('partial-normalized-' + uuid.uuid4().hex))
                    render_chunk(directory / 'raw', normalized, first_page=chunk['first_page'], page_count=chunk['page_count'],
                                 stem=chunk['stem'], version=self.options.version)
                    prior['state'] = 'complete'
                    chunk['completed'] = dict(directory=directory.relative_to(root).as_posix(), files=snapshot(directory))
                    write_state(path, manifest)
                    completed = True
                    break
                except ChunkFailure:
                    prior['state'] = 'failed' if prior['state'] == 'started' else 'interrupted'
                    prior['reason'] = 'Previous attempt stopped before a complete result; original retained.'
                    write_state(path, manifest)
            if completed:
                report('converting')
                continue
            while sum(r['state'] == 'failed' for r in chunk['attempts']) < 2:
                self.check()
                attempt = len(chunk['attempts']) + 1
                directory = root / chunk['stem'] / str(attempt)
                row = dict(number=attempt, state='preparing', mode='ocr' if any(r['state']=='failed' for r in chunk['attempts']) else 'auto')
                chunk['attempts'].append(row)
                write_state(path, manifest)
                directory.mkdir(parents=True, exist_ok=False)
                pdf = directory / (chunk['stem'] + '.pdf')
                writer = PdfWriter()
                for page in range(chunk['first_page']-1, chunk['first_page']-1+chunk['page_count']):
                    writer.add_page(reader.pages[page])
                with pdf.open('xb') as stream:
                    writer.write(stream)
                row['state'] = 'started'
                write_state(path, manifest)
                try:
                    output = directory / 'raw'
                    report('retrying' if row['mode']=='ocr' else 'converting', chunk)
                    self.converter.convert(pdf, output, mode=row['mode'])
                    self.check()
                    render_chunk(output, directory / 'normalized', first_page=chunk['first_page'],
                                 page_count=chunk['page_count'], stem=chunk['stem'], version=self.options.version)
                    row['state'] = 'complete'
                    chunk['completed'] = dict(directory=directory.relative_to(root).as_posix(), files=snapshot(directory))
                    write_state(path, manifest)
                    report('converting')
                    completed = True
                    break
                except ChunkFailure as error:
                    row.update(state='failed', reason=str(error))
                    write_state(path, manifest)
                except Deferred:
                    row['state'] = 'paused'
                    write_state(path, manifest)
                    raise
            if not completed:
                raise PdfHeld(f"原 PDF 第 {chunk['first_page']}–{chunk['first_page']+chunk['page_count']-1} 页仍未完整转换；已保留成功批次，暂停本篇导读，请由 Codex 修复后接续。")
        self.check()
        if file_digest(source) != source_sha:
            raise ContractError('accepted PDF changed during conversion')
        report('merging')
        final = root / 'complete'
        if final.exists():
            saved = read_json(root / 'complete.json')
            checked_snapshot(final, saved['files'])
            report('converted')
            return final
        # A partial merge remains separately named. It never counts as complete.
        import uuid
        pending = root / ('merging-' + uuid.uuid4().hex)
        pending.mkdir()
        texts, images, layout_pages = [], {}, []
        for chunk in manifest['chunks']:
            directory = root / chunk['completed']['directory']
            checked_snapshot(directory, chunk['completed']['files'])
            normalized = read_json(directory / 'normalized/pages.json')
            texts.extend(normalized['pages'])
            layout_pages.extend(normalized.get('layout_pages', [[] for _ in normalized['pages']]))
            for target, record in normalized['images'].items():
                dest = pending / target
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(directory / 'normalized' / target, dest)
                images[target] = record
        if len(texts) != count:
            raise ContractError('merged PDF no longer has every original page')
        text = '\n\n'.join(texts)
        if not text.strip():
            raise PdfHeld('PDF 没有可用于导读的正文，原件及转换结果已保留。')
        offset, page_maps, spans, separators, layout_blocks = 0, [], [], [], []
        for index, content in enumerate(texts):
            if index:
                separators.append(dict(kind='page_separator', char_start=offset, char_end=offset+2))
                offset += 2
            for block in layout_pages[index]:
                layout_blocks.append(dict(block, char_start=block['char_start']+offset, char_end=block['char_end']+offset))
            ranges = []
            if content:
                ranges = [dict(char_start=offset, char_end=offset+len(content))]
                spans.append(dict(mapping_id=f'page-text-{index}', **ranges[0], source_page_index=index, section_id=None, paragraph_id=None))
            page_maps.append(dict(page_index=index, page_label=None, method='MinerU.content_list', text_ranges=ranges,
                text_sha256=hashlib.sha256(content.encode()).hexdigest(), confidence_note='OCR/transcription retained; images unread; physical PDF page.'))
            offset += len(content)
        mapping = dict(schema_version=VERSION, source_sha256=source_sha, output_sha256=hashlib.sha256(text.encode()).hexdigest(),
            extractor='mineru-pipeline', extractor_version=self.options.version, mode='auto_with_one_ocr_remedy',
            text_encoding='UTF-8', offset_unit='unicode_code_point', range_convention='half_open', newline_policy='as_stored',
            text_length=len(text), locator_precision='page', page_mappings=page_maps, text_mappings=spans,
            transformations=separators, images=images, layout_blocks=layout_blocks,
            issues=['OCR and formulas may contain errors; images are retained but unread; cross-page table fragments are not semantically merged.'])
        (pending / 'source.md').write_text(text, encoding='utf-8')
        write_state(pending / 'source.pages.json', mapping)
        # Commit the expected digest before rename so an interrupted rename can
        # be reconciled without treating an arbitrary directory as a result.
        write_state(root / 'complete.json', dict(files=snapshot(pending)))
        pending.rename(final)
        report('converted')
        return final
