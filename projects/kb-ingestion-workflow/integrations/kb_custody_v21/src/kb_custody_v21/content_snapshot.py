"""Portable content history, selected explicitly rather than copying live databases."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .contracts import ContractError
from .manual_backup import local_file
from .reading_bundle import READING, CARDS, TOPICS, BRIEFS, check_links, frontmatter
from .reading_publish import read_at
from .registry import encoded
from .relation_library import GRAPH, INDEX, HEADING, REFERENCE, graph_at, related, section, index_bytes
from .runtime import no_symlinks
from .safe_publish import directory, parts

MAX_RECORD = 8 * 1024 * 1024

# These are content identities and observations, not executable requests,
# provider credentials, confirmation tokens, or a resumable service queue.
REGISTRY_EXPORTS = {
    'original': """SELECT a.asset_id,a.filename,a.size_bytes,a.sha256,f.relative_path,
        f.custody_health,r.entry_id,r.status,r.accepted_at FROM assets a
        JOIN staging_files f USING(asset_id) JOIN staging_receipts r USING(asset_id)""",
    'derived': 'SELECT asset_id,source_asset_id,extraction_id,relative_path,size_bytes,sha256 FROM derived_assets',
    'publication': 'SELECT publication_id,source_asset_id,source_id,state,preview_sha256,revision_of,files_json FROM publications_v21',
    'confirmation': 'SELECT publication_id,preview_sha256,confirmed_at FROM publication_confirmations',
    'decision': 'SELECT publication_id,command,decided_at FROM pilot_preview_decisions',
    'custody_history': 'SELECT event_id,event_type,aggregate_id,occurred_at FROM custody_events',
}


@contextmanager
def readonly(path):
    path = no_symlinks(path)
    for suffix in ('-wal', '-shm', '-journal'):
        no_symlinks(str(path) + suffix)
    # SQLite may create WAL/SHM sidecars even for a mode=ro connection. Read
    # stopped-writer bytes into a private temporary copy so observing history
    # cannot change the operational files being fingerprinted. Include the WAL;
    # immutable=1 on the original would incorrectly ignore committed WAL rows.
    with tempfile.TemporaryDirectory(prefix='p44-history-') as temporary:
        copy = Path(temporary) / 'history.sqlite3'
        for suffix in ('', '-wal'):
            original = no_symlinks(str(path) + suffix)
            if not original.exists():
                continue
            target = Path(str(copy) + suffix)
            with local_file(original) as source, target.open('xb') as output:
                os.chmod(target, 0o600)
                shutil.copyfileobj(source, output, 1024 * 1024)
        db = sqlite3.connect(copy)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:
            db.close()


def has_table(db, name):
    return db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', name)).fetchone() is not None


def records(runtime):
    if runtime.db.exists():
        with readonly(runtime.db) as db:
            for kind, sql in REGISTRY_EXPORTS.items():
                # The registry schema is required as a unit, not silently skipped.
                for row in db.execute(sql):
                    value = dict(row)
                    if kind == 'publication':
                        value['files'] = json.loads(value.pop('files_json'))
                        if value['state'] == 'publishing':
                            raise ContractError('publication is unfinished; reconcile it before content backup')
                    yield dict(kind=kind, value=value)
            for table in ('extraction_snapshots', 'expression_snapshots'):
                for row in db.execute('SELECT extraction_id,source_asset_id,output_asset_id,relative_root,map_json FROM ' + table):
                    value = dict(row)
                    mapping = json.loads(value.pop('map_json'))
                    value['output_sha256'] = mapping['output_sha256']
                    yield dict(kind='extraction', value=value)
    queue = runtime.root / 'conversion-work/daily-service/queue.sqlite3'
    if not queue.exists():
        return
    with readonly(queue) as db:
        for row in db.execute('SELECT message_id,filename,phase,source_sha,entry_id,publication_id,created,updated FROM daily_documents'):
            value = dict(row)
            value['document_key'] = hashlib.sha256(value.pop('message_id').encode()).hexdigest()
            yield dict(kind='document_status', value=value)
        if has_table(db, 'reading_materials'):
            for row in db.execute('SELECT material_id,binding,state,detail,created,updated FROM reading_materials'):
                value = dict(row)
                binding, detail = json.loads(value.pop('binding')), json.loads(value.pop('detail'))
                if value['state'] == 'publishing':
                    raise ContractError('reading publication is unfinished; reconcile it before content backup')
                value['binding'] = {k: v for k, v in binding.items() if k != 'document_id'}
                value['group'] = detail.get('group')
                yield dict(kind='reading_material', value=value)
            for row in db.execute("SELECT id,at,kind,payload FROM daily_events WHERE kind IN ('reading_approved','reading_progress','reading_answer') ORDER BY id"):
                value = dict(row)
                payload = json.loads(value.pop('payload'))
                value['detail'] = {k: payload[k] for k in ('material_id','state','revision') if k in payload}
                yield dict(kind='reading_history', value=value)
        yield dict(kind='model_usage_observation', value=dict(db.execute(
            'SELECT count(*) AS cumulative_turns FROM daily_models').fetchone()))


def export_catalog(runtime, path):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        for record in records(runtime):
            raw = (encoded(record) + '\n').encode()
            if len(raw) > MAX_RECORD:
                raise ContractError('content history record exceeds supported metadata size')
            stream.write(raw)
        stream.flush(); os.fsync(stream.fileno())


def read_records(path):
    with path.open('rb') as stream:
        while raw := stream.readline(MAX_RECORD + 1):
            if len(raw) > MAX_RECORD or not raw.endswith(b'\n'):
                raise ContractError('invalid or oversized content history record')
            yield json.loads(raw)


class Available:
    def __init__(self, db):
        self.db = db

    def __contains__(self, relative):
        return self.db.execute("SELECT 1 FROM items WHERE path=? AND kind='file'", ('reading-library/' + relative,)).fetchone() is not None

    def __iter__(self):
        for row in self.db.execute("SELECT path FROM items WHERE kind='file' AND path LIKE 'reading-library/%'"):
            yield row['path'][len('reading-library/'):]


def validate_content(root, db, catalog):
    """Check registered bytes plus readable identities/links without a live queue."""
    def expected(path, size=None, digest=None):
        parts(path)
        row = db.execute("SELECT * FROM items WHERE path=? AND kind='file'", (path,)).fetchone()
        if row is None or size is not None and row['bytes'] != size or digest is not None and row['sha256'] != digest:
            raise ContractError('content reference missing or changed: ' + path)

    for record in read_records(catalog):
        kind, value = record['kind'], record['value']
        if kind == 'original' and value['status'] == 'accepted':
            expected(value['relative_path'], value['size_bytes'], value['sha256'])
        elif kind == 'derived':
            expected(value['relative_path'], value['size_bytes'], value['sha256'])
        elif kind == 'extraction':
            expected(value['relative_root'] + '/source.md', digest=value['output_sha256'])
        elif kind == 'publication':
            suffix = '.candidate' if value['state'] in ('prepared', 'confirmed') else ''
            prefix = 'conversion-work/publications/' + value['publication_id'] + suffix + '/'
            for name, info in value['files'].items():
                expected(prefix + name, info['size_bytes'], info['sha256'])
        elif kind == 'reading_material' and value['state'] == 'completed':
            if not isinstance(value['group'], str):
                raise ContractError('completed reading material has no portable content group')
            base = 'reading-library/' + value['group']
            expected(base + '/metadata.json')
            meta = json.loads(read_at(root, base + '/metadata.json', limit=MAX_RECORD))
            if meta['material_id'] != value['material_id'] or any(meta['binding'].get(k) != v for k, v in value['binding'].items()):
                raise ContractError('reading identity disagrees with snapshot history')

    try:
        library = directory(root, 'reading-library')
    except FileNotFoundError:
        return dict(generated_files_checked=0, active_relations=0)
    checked = 0
    try:
        graph, graph_raw = graph_at(library)
        available = Available(db)
        for path in available:
            if not path.endswith('.md') or '/入库依据/' in path or path.rsplit('/', 1)[-1] in ('原文.md', '摘录.md'):
                continue
            raw = read_at(library, path, limit=MAX_RECORD)
            if not raw.startswith(b'---\n'):
                if path.endswith('/来源卡片.md') or any(path.startswith(p + '/') for p in (CARDS, TOPICS, BRIEFS)):
                    raise ContractError('generated reading entry lost its required metadata')
                continue  # Preserved originals and handwritten documents are not regenerated.
            check_links(path, raw.decode(), available)
            checked += 1
            if path.startswith(READING + '/') and path.endswith('/来源卡片.md'):
                meta, body = frontmatter(raw.decode())
                mid = meta['material_id']
                wanted = related(graph, mid)
                actual = [v for v in meta.get('related', []) if isinstance(v, dict) and v.get('reference') == REFERENCE]
                metadata = json.loads(read_at(library, path.rsplit('/', 1)[0] + '/metadata.json', limit=MAX_RECORD))
                if metadata.get('material_id') != mid or actual != wanted or [v for v in metadata.get('related', []) if isinstance(v, dict) and v.get('reference') == REFERENCE] != wanted:
                    raise ContractError('reciprocal relation metadata disagrees')
                if wanted or HEADING in body:
                    if body.count(HEADING) != 1 or body.split(HEADING)[1].strip() != section(graph, mid, path).strip():
                        raise ContractError('reciprocal relation section disagrees')
        for mid, material in graph['materials'].items():
            raw = read_at(library, material['group'] + '/来源卡片.md', limit=MAX_RECORD)
            meta = frontmatter(raw.decode())[0] if raw is not None else {}
            if meta.get('material_id') != mid or meta.get('binding', {}).get('source_sha256') != material['source_sha256']:
                raise ContractError('relation endpoint is missing or has changed identity')
        if graph_raw is not None and read_at(library, INDEX, limit=MAX_RECORD) != index_bytes(graph):
            raise ContractError('relation navigation disagrees with the confirmed graph')
        return dict(generated_files_checked=checked, active_relations=sum(e['active'] for e in graph['edges'].values()))
    finally:
        os.close(library)
