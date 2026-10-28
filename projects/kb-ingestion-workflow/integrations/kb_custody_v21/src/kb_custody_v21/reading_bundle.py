"""Render reviewed reading content; keep source, cards and navigation distinct."""
from copy import deepcopy
import hashlib
from html import escape
import json
from pathlib import PurePosixPath
import posixpath
import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
import yaml

from .contracts import ContractError
from . import table_evidence
from .layout_evidence import checked_policy
from .reading_contract import CATEGORIES, require, sha, validate_review
from .registry import encoded
from .safe_publish import manifest, parts

READING = '10_Reading_读书摘录'
CARDS = '20_Cards_知识卡片'
TOPICS = '30_Topics_主题索引'
BRIEFS = '50_Daily_Briefs_知识简报'
BASIS_LABELS = dict(source_statement='来源陈述', interpretation='文本解读',
    fictional_setting='虚构设定', analogy='类比', scenario='情景推演')


class UniqueSafeLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    value = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in value:
            raise ContractError('reading YAML requires unique string keys')
        value[key] = loader.construct_object(value_node, deep=deep)
    return value


UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def frontmatter(raw):
    require(isinstance(raw, str) and raw.startswith('---\n'), 'YAML frontmatter required')
    chunks = raw.split('\n---\n', 1)
    require(len(chunks)==2, 'YAML closing delimiter required')
    try:
        metadata = yaml.load(chunks[0][4:], Loader=UniqueSafeLoader)
    except yaml.YAMLError as error:
        raise ContractError('reading YAML cannot be parsed') from error
    require(isinstance(metadata, dict), 'YAML mapping required')
    return metadata, chunks[1]


def md(metadata, body):
    # JSON is a YAML subset; quoted keys/values cannot introduce YAML tags or
    # ambiguous dates. Existing handwritten YAML is parsed by PyYAML above.
    return ('---\n'+json.dumps(metadata, ensure_ascii=False, indent=2)+'\n---\n\n'+body.strip()+'\n').encode()


def filename(title):
    value = unicodedata.normalize('NFC', title)
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|\[\]#]+', ' ', value).strip(' .')
    return (value or '未命名')[:56]


def label(value):
    return re.sub(r'([\\`*{}\[\]()#+.!<>|])', r'\\\1', value.replace('\n', ' '))


def link(origin, target, title):
    return '['+label(title)+']('+quote(posixpath.relpath(target, posixpath.dirname(origin)), safe='/.-_')+')'


def resolve_link(origin, value):
    parsed = urlsplit(value)
    if parsed.scheme:
        require(parsed.scheme in ('https', 'http', 'mailto'), 'unsupported generated link scheme')
        return None
    require(not parsed.netloc and not parsed.path.startswith('/'), 'absolute generated link prohibited')
    path = unquote(parsed.path)
    require('\\' not in path and '\x00' not in path, 'unsafe local link')
    if not path:
        return origin
    result = posixpath.normpath(posixpath.join(posixpath.dirname(origin), path))
    require(result != '..' and not result.startswith('../'), 'generated link escapes test library')
    return result


def check_links(path, raw, available):
    _, body = frontmatter(raw)
    parser = MarkdownIt('commonmark', {'html': True})
    # Parse prohibited schemes too so validation rejects them explicitly;
    # CommonMark's renderer would otherwise leave them as plain text.
    parser.validateLink = lambda value: True
    def inspect(tokens):
        for token in tokens:
            require(token.type not in ('html_block', 'html_inline'), 'raw HTML in generated content requires review')
            if token.type in ('link_open', 'image'):
                target = resolve_link(path, token.attrGet('href' if token.type=='link_open' else 'src'))
                require(target is None or target in available, 'unresolved generated local link: '+str(target))
            if token.children:
                inspect(token.children)
    inspect(parser.parse(body))
    # Obsidian-style Wikilinks have no CommonMark token; resolve exact paths,
    # or an unambiguous basename among the provided test-library files.
    for target in re.findall(r'\[\[([^\]\n]+)\]\]', body):
        target = target.split('|', 1)[0].split('#', 1)[0]
        if not target:
            continue
        if not target.endswith('.md'):
            target += '.md'
        candidates = {v for v in (target, resolve_link(path,target)) if v in available}
        if '/' not in target:
            candidates |= {v for v in available if PurePosixPath(v).name==target}
        require(len(candidates)==1 and all(v in available for v in candidates), 'missing or ambiguous Wikilink')


def build_bundle(source, draft, review, *, assets, date, existing_files):
    validate_review(review, draft, source)
    require(review['status']=='approve', 'independent review must approve before rendering')
    require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', date)), 'stable publication date required')
    require(isinstance(assets, dict) and '提取/source.md' in assets and '导读.md' in assets,
            'approved original extraction and separate guide required')
    require(assets['提取/source.md']==source['original'].encode(), 'full original must match approved extraction')
    manifest(assets)
    source_id = source['binding']['source_id']
    relative = draft['category']+'/'+(draft['collection']+'/' if draft['collection'] else '')+filename(draft['title'])+'_'+sha(source_id)[:12]
    group = READING+'/'+relative
    note = group+'/综合读书笔记.md'
    private = draft['category']=='06_个人数据与资料包' or source['provenance'].get('sensitivity')=='private'
    new_cards = any(not c['existing_path'] for c in draft['cards'])
    updated_cards = any(c['existing_path'] for c in draft['cards'])
    action = 'create_new_and_update_existing' if new_cards and updated_cards else 'merge_into_existing' if updated_cards else 'create_new'
    metadata = dict(title=draft['title'], source_id=source_id, material_id=source['material_id'],
        type='source_note', workflow_role='supporting', status='active',
        source_family=CATEGORIES[draft['category']], source_category=draft['category'],
        knowledge_base_relative_dir=relative, source_path='入库依据/提取/source.md', source_type=source['source_type'],
        content_nature=draft['content_nature'], source_completeness=draft['source_completeness'],
        processing_status='completed', validation_status=review['validation_status'], review_method='codex',
        review_scope=review['review_scope'], review_receipt=sha(review), input_sha256=sha(source),
        rules_sha256=source['binding']['rules_sha256'], created=date, updated=date, tags=draft['tags'],
        related=[], knowledge_update_action=action, sensitivity='private' if private else 'unspecified',
        brief_visibility='exclude' if private else 'include', binding=source['binding'], provenance=source['provenance'])
    files = {group+'/入库依据/'+path: data for path, data in assets.items()}
    files[group+'/原文.md'] = source['original'].encode()
    for path, raw in assets.items():
        if path.startswith('提取/images/'):
            files[group+'/'+path[len('提取/'):]] = raw
    if source['excerpts']:
        files[group+'/摘录.md'] = source['excerpts'].encode()
    generated = []
    def put(path, meta, body):
        require(path not in files, 'duplicate generated path')
        files[path] = md(meta, body); generated.append(path)
    def local(name):
        return link(note, group+'/'+name, name.removesuffix('.md'))
    overview = '# '+label(draft['title'])+'\n\n'+' · '.join(local(n) for n in ('原文.md','关键词提取.md','文章客观整理.md','摘录关注整理.md','综合读书笔记.md'))
    overview += '\n\n'+link(note, group+'/入库依据/导读.md', '本材料独立导读')
    for path in sorted(assets):
        if path.startswith('原件/') or path.endswith('.pdf'):
            overview += '\n\n'+link(note, group+'/入库依据/'+path, path)
    overview += '\n\n复核方式：Codex；复核范围：'+review['review_scope']
    if draft['limitations']:
        overview += '\n\n## 使用边界\n\n'+'\n\n'.join(draft['limitations'])
    if source['image_boundary']:
        overview += '\n\n图片读取边界：'+source['image_boundary']
    mapping = json.loads(assets.get('提取/extraction-map.json', b'{}'))
    if mapping:
        require(mapping.get('output_sha256') == hashlib.sha256(assets['提取/source.md']).hexdigest()
                and mapping.get('offset_unit') == 'unicode_code_point'
                and mapping.get('range_convention') == 'half_open', 'table page map differs from source')
    pages = [dict(page=p['page_index']+1, **r) for p in mapping.get('page_mappings', []) for r in p['text_ranges']]
    excluded = checked_policy(source['provenance'].get('evidence_policy'), source['original'])
    tables = table_evidence.inventory(source['original'], pages, excluded)
    if tables['tables']:
        path = group+'/表格目录.md'
        inventory_path = group+'/入库依据/提取/tables.json'
        require(inventory_path not in files, 'duplicate table inventory path')
        files[inventory_path] = (encoded(tables)+'\n').encode()
        body = '# 表格目录\n\n结构检查只检查识别结果的行列关系，不证明数字、单位和表头与原始文件相同。\n\n'
        for table in tables['tables']:
            body += '## '+table['id']+'\n\n'+label(table_evidence.notice([table]))+'\n\n'
            body += link(path, group+'/原文.md', '查看完整提取原文')+f"；原文字符 {table['char_start']}–{table['char_end']}\n\n"
        body += link(path, inventory_path, '机器可读的表格、单元格与原文位置')
        for asset in sorted(assets):
            if asset.lower().endswith('.pdf'):
                body += '\n\n'+link(path, group+'/入库依据/'+asset, '对照 PDF 原件：'+asset)
        put(path, dict(metadata, type='table_index', workflow_role='navigation'), body)
        overview += '\n\n'+link(note, path, '表格目录与核验状态')
    put(group+'/来源卡片.md', metadata, overview)
    claims = {c['id']: c for c in draft['claims']}
    def content(sections, origin):
        result = ''
        for section in sections:
            result += '\n\n## '+label(section['heading'])+'\n'
            for cid in section['claim_ids']:
                c = claims[cid]
                result += '\n\n**'+BASIS_LABELS[c['basis']]+'**：'+c['text']
                if c['conditions']:
                    result += '\n\n适用条件与边界：'+'；'.join(c['conditions'])
                for e in c['evidence']:
                    target = group+('/摘录.md' if e['source']=='excerpts' else '/原文.md')
                    result += '\n\n'+link(origin, target, '来源依据')+'：\n\n'+'\n'.join('> '+escape(line, quote=False) for line in e['quote'].splitlines())
        return result
    note_meta = dict(metadata, type='reading_note', workflow_role='formal')
    for name, sections in [('文章客观整理.md', draft['objective']), ('综合读书笔记.md', draft['synthesis'])]:
        path = group+'/'+name
        body = '# '+label(draft['title'])+content(sections,path)+'\n\n'+link(path,group+'/来源卡片.md','来源与使用边界')
        if name=='综合读书笔记.md' and draft['chapters']:
            body += '\n\n## 章节深读\n\n'+'\n'.join('- '+link(path,group+f'/章节/{i:03d}_'+filename(ch['title'])+'.md',ch['title']) for i,ch in enumerate(draft['chapters'],1))
        put(path, note_meta, body)
    for index, chapter in enumerate(draft['chapters'], 1):
        path = group+f'/章节/{index:03d}_'+filename(chapter['title'])+'.md'
        put(path, note_meta, '# '+label(chapter['title'])+content(chapter['sections'],path)+'\n\n'+link(path,note,'综合笔记'))
    keyword_body = '# 关键词提取\n'
    for key, title in [('attention','关注关键词（摘录）'),('objective','客观关键词（原文）'),('entities','关键实体')]:
        keyword_body += '\n\n## '+title+'\n'
        for entry in draft['keywords'][key]:
            keyword_body += '\n\n'+label(entry['term'])+'（'+('直接提取' if entry['kind']=='direct' else '语义归纳')+'）\n\n'
            keyword_body += '\n'.join('> '+escape(line, quote=False) for line in entry['evidence']['quote'].splitlines())
        if not draft['keywords'][key]:
            keyword_body += '\n未提取；不按数量补造。'
    put(group+'/关键词提取.md', dict(metadata,type='source_analysis'), keyword_body)
    attention = '# 摘录关注整理\n\n'
    if source['excerpts']:
        attention += '仅以下明确摘录反映本次材料中的关注线索；不外推为长期偏好。\n\n'+local('摘录.md')
        attention += '\n\n'+'、'.join(label(e['term']) for e in draft['keywords']['attention'])
    else:
        attention += '本次没有提供明确的摘录稿，不能推断个人偏好。'
    put(group+'/摘录关注整理.md', dict(metadata,type='source_analysis'), attention)
    source_key = source['material_id']
    for card in draft['cards']:
        path = card['existing_path'] or CARDS+'/'+filename(card['title'])+'_'+sha([card['title'],card['kind']])[:12]+'.md'
        existing = source['existing_cards'].get(path)
        if not existing:
            require(path not in existing_files, 'card title collides with existing output')
            meta = dict(title=card['title'], type=card['kind'], workflow_role='interpretive' if card['kind']=='fiction_interpretation_card' else 'formal',
                status='active', created=date, updated=date, related=[note], sources=[source_id], reading_materials=[source_key],
                validation_status=review['validation_status'], review_method='codex',
                sensitivity='private' if private else 'unspecified')
            if card['kind']=='fiction_interpretation_card':
                meta.update(epistemic_scope='authorial_speculation', knowledge_status='hypothesis_not_verified')
            body = '# '+label(card['title'])
        else:
            require(existing_files.get(path)==existing['text'].encode(), 'card changed since review input')
            meta, body = frontmatter(existing['text'])
            require(meta.get('type')==card['kind'], 'cannot merge fiction and factual cards')
            for key, entry in [('related',note),('sources',source_id),('reading_materials',source_key)]:
                require(isinstance(meta.get(key,[]),list), 'existing card source list malformed')
                require(entry not in meta.get(key,[]) if key=='reading_materials' else True, 'material already merged')
                meta[key] = list(dict.fromkeys([*meta.get(key,[]),entry]))
            meta['updated'] = date
            if private:
                meta['sensitivity']='private'
        body += '\n\n## 新增来源补充\n\n'+link(path,note,draft['title'])+'\n\n'+card['reuse_reason']
        body += content([dict(heading='内容与依据',claim_ids=card['claim_ids'])],path)
        put(path,meta,body)
    for topic in draft['topics']:
        path = topic['existing_path'] or TOPICS+'/'+filename(topic['title'])+'_'+sha(topic['title'])[:12]+'.md'
        old = source['existing_topics'].get(path)
        if old:
            require(existing_files.get(path)==old['text'].encode(), 'topic changed since review input')
            meta, body = frontmatter(old['text'])
        else:
            require(path not in existing_files, 'topic title collides with existing output')
            meta=dict(title=topic['title'], type='topic_index', workflow_role='navigation', status='draft', created=date, updated=date)
            body='# '+label(topic['title'])+'\n\n本页仅提供来源导航；不据此声称已形成专题综述。'
        heading = '虚构作品中的思辨材料' if draft['content_nature']=='fiction' else '来源导航（保留各自证据边界）'
        body += '\n\n## '+heading+'\n\n- '+link(path,note,draft['title'])
        meta['updated']=date
        put(path,meta,body)
    # One material entry per day. Private source titles and contents never enter
    # even a supposedly harmless count/title-only brief.
    if not private:
        path=BRIEFS+'/'+date+'.md'
        old=existing_files.get(path)
        if old is None:
            meta=dict(title=date+' 测试库知识简报',type='daily_brief',workflow_role='log',date=date,reading_materials=[])
            body='# '+date+' 测试库知识简报'
        else:
            meta,body=frontmatter(old.decode())
        require(isinstance(meta.get('reading_materials'),list) and source_key not in meta['reading_materials'], 'duplicate brief entry')
        meta['reading_materials'].append(source_key)
        body+='\n\n- '+link(path,note,draft['title'])+'；复核级别：'+review['validation_status']
        put(path,meta,body)
    meta_path=group+'/metadata.json'
    files[meta_path]=(encoded(dict(metadata,status='completed'))+'\n').encode()
    proof=group+'/复核记录.json'
    files[proof]=(encoded(dict(input_sha256=sha(source),draft=draft,review=review))+'\n').encode()
    all_files=dict(existing_files,**files)
    for path in generated:
        check_links(path,files[path].decode(),all_files)
    require(json.loads(files[meta_path])['knowledge_base_relative_dir']==group[len(READING)+1:], 'metadata path mismatch')
    manifest(files)
    return dict(files=files, generated=generated, metadata_path=meta_path, group=group, available_paths=sorted(existing_files),
        expected={path: hashlib.sha256(existing_files[path]).hexdigest() if path in existing_files else None for path in files},
        review_receipt=sha(review), input_sha256=sha(source))
