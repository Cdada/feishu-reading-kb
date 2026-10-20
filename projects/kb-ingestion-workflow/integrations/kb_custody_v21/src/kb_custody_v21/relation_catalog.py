"""Bounded metadata search over completed materials in the isolated library.

Candidates are clues for the owner, never automatic work/version identities.
No original text, network request, or model invocation is needed here.
"""
import json
import os
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .contracts import ContractError
from .reading_bundle import READING
from .reading_contract import sha
from .reading_publish import read_at
from .reading_store import ReadingStore
from .safe_publish import open_root, root_identity


class RelationProblem(ContractError):
    """A visible local association problem; it does not undo completed intake."""


def need(value, message):
    if not value:
        raise RelationProblem(message)


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def doi(value):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value.strip(), flags=re.I).casefold()
    return value if re.fullmatch(r'10\.\d{4,9}/\S+', value) else ''


def source_url(value):
    if not isinstance(value, str):
        return ''
    try:
        url = urlsplit(value.strip())
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
            return ''
        # Preserve query parameters: removing them can conflate different papers.
        return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path or '/', url.query, ''))
    except ValueError:
        return ''


def read_json_at(fd, path, limit=1024*1024):
    try:
        raw = read_at(fd, path, limit=limit)
        need(raw is not None, '资料关联所需文件缺失：'+path)
        value = json.loads(raw)
        need(isinstance(value, dict), '资料关联文件必须是对象：'+path)
        return value, raw
    except (OSError, ValueError, UnicodeError, ContractError) as error:
        if isinstance(error, RelationProblem):
            raise
        raise RelationProblem('无法读取资料关联文件：'+path) from error


class Catalog:
    def __init__(self, profile, store):
        self.profile, self.store = profile, store
        self.reading = ReadingStore(store)

    def get(self, material_id):
        material = self.reading.get(material_id)
        need(material['state'] == 'completed', '只有完整整理完成的资料才能关联。')
        group = material['detail']['group']
        need(group.startswith(READING+'/'), '关联资料必须位于测试库读书目录。')
        self.profile.check()
        self.profile.target(group+'/metadata.json')
        fd = open_root(self.profile.root, root_identity(self.profile.root))
        try:
            meta, _ = read_json_at(fd, group+'/metadata.json')
            review, _ = read_json_at(fd, group+'/复核记录.json')
        finally:
            os.close(fd)
        need(meta.get('status') == 'completed' and meta.get('processing_status') == 'completed'
             and meta.get('material_id') == material_id and meta.get('binding') == material['binding']
             and meta.get('review_receipt') == material['detail']['review_receipt'],
             '资料身份或完成记录不一致，请先核查。')
        need(sha(review.get('review')) == meta['review_receipt']
             and review.get('input_sha256') == meta.get('input_sha256')
             and sha(review.get('draft')) == review['review'].get('draft_sha256'),
             '复核记录与已完成资料不一致。')
        title = meta.get('title')
        need(isinstance(title, str) and 0 < len(title) <= 500, '资料标题超出关联检索范围。')
        provenance = meta.get('provenance', {})
        need(isinstance(provenance,dict), '资料来源元数据格式异常。')
        record = provenance.get('source_record', {})
        need(isinstance(record,dict), '资料来源记录格式异常。')
        work = record.get('work', {})
        release = record.get('release_context', {})
        need(isinstance(work,dict) and isinstance(release,dict) and isinstance(work.get('authors',[]),list)
             and isinstance(meta.get('tags',[]),list) and isinstance(review.get('draft'),dict), '检索元数据格式异常。')
        need(work.get('title') is None or isinstance(work['title'],str), '来源题名格式异常。')
        authors = [v['name'] for v in work.get('authors', []) if isinstance(v, dict) and isinstance(v.get('name'), str)]
        terms = list(meta.get('tags', []))
        keywords = review['draft'].get('keywords', {})
        need(isinstance(keywords,dict) and all(isinstance(v,list) for v in keywords.values()), '关键词元数据格式异常。')
        for entries in keywords.values():
            terms.extend(v['term'] for v in entries if isinstance(v, dict) and isinstance(v.get('term'), str))
        terms = sorted({normalized(v) for v in terms if isinstance(v, str) and 2 <= len(normalized(v)) <= 60})
        urls = sorted({v for v in map(source_url, (work.get('canonical_url'), work.get('article_url'), release.get('source_url'))) if v})
        return dict(material_id=material_id, document_id=material['document_id'], group=group,
                    source_sha256=material['binding']['source_sha256'], title=title,
                    is_paper=release.get('channel_type') == 'journal',
                    work_title=work.get('title') or title, authors=authors,
                    doi=doi(work.get('doi')), urls=urls, keywords=terms,
                    binding=material['binding'])

    def all(self):
        rows = self.store.rows("SELECT material_id FROM reading_materials WHERE state='completed' ORDER BY material_id LIMIT 5001")
        need(len(rows) <= 5000, '测试库超过本版检索上限，需缩小检索范围。')
        return {row['material_id']: self.get(row['material_id']) for row in rows}


def candidates(current, materials):
    found = []
    a = normalized(current['work_title'])
    for other in materials.values():
        if other['material_id'] == current['material_id'] or other['source_sha256'] == current['source_sha256']:
            continue
        evidence, score = [], 0
        if current['doi'] and current['doi'] == other['doi']:
            evidence.append('已登记的 DOI 相同：'+current['doi']); score += 100
        shared_urls = sorted(set(current['urls']) & set(other['urls']))
        if shared_urls:
            evidence.append('已登记的来源链接相同'); score += 90
        b = normalized(other['work_title'])
        if len(a) >= 6 and a == b:
            evidence.append('题名相同（不据此认定版本）'); score += 50
        elif len(a) >= 8 and len(b) >= 8:
            x, y = {a[i:i+2] for i in range(len(a)-1)}, {b[i:i+2] for i in range(len(b)-1)}
            if 2*len(x & y)/max(1, len(x)+len(y)) >= .68:
                evidence.append('题名部分相似（不据此认定版本）'); score += 30
        authors = set(map(normalized, current['authors'])) & set(map(normalized, other['authors'])) - {''}
        terms = set(current['keywords']) & set(other['keywords'])
        if authors:
            evidence.append('有相同作者'); score += 15
        if len(terms) >= 2:
            evidence.append('共同关键词：'+'、'.join(sorted(terms)[:5])); score += 30 if len(terms)>=3 else 20
        # One author, one broad tag, or publication date alone is insufficient.
        if score >= 30:
            found.append(dict(material_id=other['material_id'], title=other['title'],
                              evidence=evidence, score=score))
    return sorted(found, key=lambda v: (-v['score'], v['material_id']))[:5]
