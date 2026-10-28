"""Rebuildable SQLite FTS5 index with bounded Chinese bigrams and exact offsets.

No embedding process, extra tokenizer service or writes to source documents.
Chinese bigrams retain two-character terms; Latin words remain whole tokens.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import unicodedata

from .contracts import ContractError
from .registry import encoded
from .qa_language import declared_aliases, variants
from datetime import datetime
from zoneinfo import ZoneInfo
from .runtime import no_symlinks
from . import table_evidence

SCHEMA = '''
CREATE TABLE IF NOT EXISTS qa_index_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qa_sources(id TEXT PRIMARY KEY,digest TEXT NOT NULL,metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qa_chunks(id INTEGER PRIMARY KEY,material_id TEXT NOT NULL,
 start INTEGER NOT NULL,end INTEGER NOT NULL,paragraph INTEGER NOT NULL,heading TEXT NOT NULL,text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS qa_chunks_source ON qa_chunks(material_id);
CREATE VIRTUAL TABLE IF NOT EXISTS qa_fts USING fts5(title,keywords,body,tokenize='unicode61');
'''
VERSION = 'qa-bigram-v1'


def tokens(text):
    result = []
    for word in re.findall(r'[\u3400-\u9fff]+|[a-z0-9]+(?:[._+-][a-z0-9]+)*', unicodedata.normalize('NFKC', text).lower()):
        if '\u3400' <= word[0] <= '\u9fff':
            result.extend(word[i:i+2] for i in range(max(1, len(word)-1)))
        else:
            result.append(word)
    return list(dict.fromkeys(result))


def source_tables(source):
    return table_evidence.summaries(table_evidence.inventory(
        source['text'], source.get('pages', []), source['excluded_ranges']))


def chunks(source, tables=None):
    text, heading = source['text'], ''
    tables = source_tables(source) if tables is None else tables
    paragraphs = list(re.finditer(r'\S[^\n]*(?:\n(?!\s*\n)[^\n]+)*', text))
    result = []
    protected = [dict(char_start=t['evidence_start'], char_end=t['char_end']) for t in tables]
    for number, match in enumerate(paragraphs, 1):
        if match.group().startswith('#'):
            heading = match.group().splitlines()[0].lstrip('# ').strip()[:160]
        intervals = [(match.start(), match.end())]
        for r in [*source['excluded_ranges'], *protected]:
            intervals = [(x, y) for a, z in intervals for x, y in
                         ((a, min(z, r['char_start'])), (max(a, r['char_end']), z)) if x < y]
        for a, z in intervals:
            while a < z:
                end = min(a+900, z)
                if text[a:end].strip():
                    result.append((a, end, number, heading, text[a:end]))
                a = end
    for t in tables:
        a, z = t['evidence_start'], t['char_end']
        if any(a < r['char_end'] and r['char_start'] < z for r in source['excluded_ranges']):
            continue
        number = next((i for i, p in enumerate(paragraphs, 1) if p.start() <= a < p.end()), 1)
        # Whole fragments retain multi-row headers, units, notes and merged
        # cells. Cross-page links do not concatenate discontinuous quotations.
        result.append((a, z, number, t['caption'], text[a:z]))
    yield from sorted(result)


class QAIndex:
    def __init__(self, path):
        self.path = no_symlinks(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript(SCHEMA)
            old = db.execute("SELECT value FROM qa_index_meta WHERE key='version'").fetchone()
            if old and old[0] != VERSION:
                raise ContractError('问答索引版本变化，需要显式重建。')
            db.execute("INSERT OR IGNORE INTO qa_index_meta VALUES ('version',?)", (VERSION,))

    @contextmanager
    def connection(self):
        no_symlinks(self.path)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def remove(db, key):
        db.execute('DELETE FROM qa_fts WHERE rowid IN (SELECT id FROM qa_chunks WHERE material_id=?)', (key,))
        db.execute('DELETE FROM qa_chunks WHERE material_id=?', (key,))
        db.execute('DELETE FROM qa_sources WHERE id=?', (key,))

    def sync(self, provider):
        ids = provider.ids()
        changed = removed = 0
        with self.connection() as db:
            old = {r['id']: r['digest'] for r in db.execute('SELECT id,digest FROM qa_sources')}
            for key in old.keys()-set(ids):
                self.remove(db, key); removed += 1
        # Each material commits independently. A query only runs after a full
        # successful sync, so an invalid source cannot leave stale usable hits.
        for key in ids:
            source = provider.get(key)
            metadata = {k: v for k, v in source.items() if k not in ('text', 'evidence')}
            tables = source_tables(source)
            metadata['tables'] = tables
            metadata['chunking_revision'] = table_evidence.VERSION
            source_chunks = list(chunks(source, tables))
            metadata['aliases'] = declared_aliases('\n\n'.join(body for *_, body in source_chunks))
            digest = hashlib.sha256(encoded(metadata).encode()).hexdigest()
            if old.get(key) == digest:
                continue
            with self.connection() as db:
                self.remove(db, key)
                db.execute('INSERT INTO qa_sources VALUES (?,?,?)', (key, digest, encoded(metadata)))
                for a, z, p, heading, body in source_chunks:
                    row = db.execute('INSERT INTO qa_chunks(material_id,start,end,paragraph,heading,text) VALUES (?,?,?,?,?,?)',
                                     (key, a, z, p, heading, body))
                    db.execute('INSERT INTO qa_fts(rowid,title,keywords,body) VALUES (?,?,?,?)',
                               (row.lastrowid, ' '.join(tokens(source['title'])),
                                ' '.join(tokens(' '.join(source['keywords']))), ' '.join(tokens(body))))
                changed += 1
        return dict(total=len(ids), changed=changed, removed=removed)

    def search(self, text, *, material_ids=(), year_from=None, year_to=None, limit=12, time_scope=None, expand=True, body_only=False):
        if time_scope not in (None, 'publication', 'content', 'ingestion'):
            raise ContractError('时间口径无效。')
        materials = [m for m in self.materials() if not material_ids or m['material_id'] in material_ids]
        alternatives = variants(text, materials) if expand else [text]
        term_sets = [set(tokens(v)[:80]) for v in alternatives]
        terms = list(dict.fromkeys(t for v in alternatives for t in tokens(v)))[:240]
        if not terms:
            return []
        if not 1 <= limit <= 40 or len(material_ids) > 4:
            raise ContractError('问答范围超过本版上限。')
        match = ' OR '.join('"'+t.replace('"', '""')+'"' for t in terms)
        # A selected document already has a known title. Weekly evidence must
        # match its body, so repeated metadata cannot promote page numbers.
        if body_only:
            match = 'body : ('+match+')'
        clauses, args = ['qa_fts MATCH ?'], [match]
        if material_ids:
            clauses.append('c.material_id IN ('+','.join('?' for _ in material_ids)+')')
            args.extend(material_ids)
        for year, op in ((year_from, '>='), (year_to, '<=')):
            if year is not None and time_scope in (None, 'publication'):
                clauses.append("json_extract(s.metadata,'$.publication_year') "+op+' ?')
                args.append(year)
            elif year is not None and time_scope == 'ingestion':
                if year == 9999 and op == '<=':
                    continue
                boundary = datetime(year if op=='>=' else year+1, 1, 1, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
                clauses.append("json_extract(s.metadata,'$.ingested_at') "+('>=' if op=='>=' else '<')+' ?')
                args.append(boundary)
        with self.connection() as db:
            rows = db.execute('SELECT c.*,s.metadata,bm25(qa_fts,4,2,1) AS rank FROM qa_fts '
                'JOIN qa_chunks c ON c.id=qa_fts.rowid JOIN qa_sources s ON s.id=c.material_id WHERE '
                +' AND '.join(clauses)+' ORDER BY rank,c.material_id,c.start LIMIT 160', args).fetchall()
        ranked = []
        for row in rows:
            meta = json.loads(row['metadata'])
            present = set(tokens(row['text'] if body_only else
                                 row['text']+' '+meta['title']+' '+' '.join(meta['keywords'])))
            matches = [(len(t & present)/len(t), t) for t in term_sets if t and len(t & present)>=min(2,len(t))]
            if not matches:
                continue
            score, matched = max(matches, key=lambda value:value[0])
            overlap = matched & present
            if time_scope == 'content' and year_from is not None:
                years = [int(y) for y in re.findall(r'(?<![0-9])([12][0-9]{3})(?![0-9])',row['text'])]
                if not any(year_from <= y <= (year_to or year_from) for y in years):
                    continue
            # Discard accidental one-bigram matches in a long natural question.
            if len(overlap) < min(2, len(matched)):
                continue
            table_context = table_evidence.context_for(meta.get('tables', []), row['start'], row['end'])
            end = row['end']
            if any(not t['qa_usable'] for t in table_context):
                # Full text remains searchable, but unsafe/oversize tables are
                # navigation only. Return the caption/opening tag, never a
                # misleading partial row to a model or to a result card.
                t = table_context[0]
                end = min(end, t['char_start']+len('<table>'))
            hit = dict(meta, text=row['text'][:end-row['start']], char_start=row['start'], char_end=end,
                       paragraph=row['paragraph'], heading=row['heading'], score=score, query_expansions=alternatives[1:])
            hit.pop('tables', None)
            hit.pop('chunking_revision', None)
            if table_context:
                hit['table_context'] = table_context
            hit['physical_pages'] = sorted({p['page'] for p in meta.get('pages', [])
                                           if p['char_start'] < row['end'] and row['start'] < p['char_end']})
            hit['evidence_id'] = hashlib.sha256(encoded([meta['material_id'], meta['body_sha256'], row['start'], end]).encode()).hexdigest()[:24]
            ranked.append((hit, row['rank']))
        ranked.sort(key=lambda pair: (-pair[0]['score'], pair[1], pair[0]['material_id'], pair[0]['char_start']))
        # Round-robin source diversity: a long paper must not crowd out others.
        buckets = {}
        for hit, _ in ranked:
            if hit['material_id'] not in buckets and len(buckets) >= 8:
                continue
            buckets.setdefault(hit['material_id'], []).append(hit)
        results = []
        while buckets and len(results) < limit:
            for key in list(buckets):
                results.append(buckets[key].pop(0))
                if not buckets[key]:
                    del buckets[key]
                if len(results) == limit:
                    break
        return results

    def materials(self):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT metadata FROM qa_sources ORDER BY id')]
