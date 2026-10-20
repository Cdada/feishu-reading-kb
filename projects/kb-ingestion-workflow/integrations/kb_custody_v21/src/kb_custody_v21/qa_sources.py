"""Read only completed, reviewed originals; the index is never an authority."""
import hashlib
import json
import os
from pathlib import Path

from .contracts import ContractError
from .f05 import file_sha
from .layout_evidence import checked_policy
from .reading_contract import sha
from .reading_publish import read_at
from .relation_catalog import Catalog, read_json_at
from .runtime import no_symlinks
from .safe_publish import open_root, root_identity


class QASources:
    def __init__(self, authority, store):
        self.authority, self.store = authority, store
        self.catalog = Catalog(authority.reading, store)

    def ids(self):
        rows = self.store.rows("SELECT material_id FROM reading_materials WHERE state='completed' ORDER BY material_id LIMIT 5001")
        if len(rows) > 5000:
            raise ContractError('问答资料超过 5000 份，请先缩小范围。')
        return [r['material_id'] for r in rows]

    def get(self, material_id):
        item = self.catalog.get(material_id)
        material = self.catalog.reading.get(material_id)
        detail = material['detail']
        source_path = no_symlinks(Path(detail['input_path']))
        expected_parent = self.authority.root/'reading-jobs'/material_id
        if source_path.parent != expected_parent or source_path.stat().st_size > 4*1024*1024:
            raise ContractError('问答输入必须来自本资料已复核的整理记录。')
        if file_sha(source_path) != detail['input_file_sha256']:
            raise ContractError('已复核的整理输入发生变化。')
        source = json.loads(source_path.read_bytes())['source']
        root = self.authority.reading.root
        fd = open_root(root, root_identity(root))
        try:
            meta, meta_raw = read_json_at(fd, item['group']+'/metadata.json')
            body_path = item['group']+'/入库依据/提取/source.md'
            raw = read_at(fd, body_path, limit=1024*1024)
        finally:
            os.close(fd)
        if (source['binding'] != material['binding'] or sha(source) != meta['input_sha256']
                or raw is None or raw != source['original'].encode()):
            raise ContractError('已入库原文与复核输入不一致，请核查后再问答。')
        text = raw.decode()
        policy = source.get('provenance', {}).get('evidence_policy')
        excluded = checked_policy(policy, text)
        record = meta.get('provenance', {}).get('source_record', {})
        year = record.get('work', {}).get('publication_year')
        if type(year) is not int or not 1000 <= year <= 9999:
            year = None
        release = record.get('release_context', {}).get('released_at')
        # A verified completion event supplies ingestion time, never mtime.
        events = self.store.rows("SELECT at,payload FROM daily_events WHERE kind='reading_progress' ORDER BY id")
        completed = [r['at'] for r in events if (v := json.loads(r['payload'])).get('material_id') == material_id and v.get('state') == 'completed']
        if len(completed) != 1:
            raise ContractError('问答需要唯一的已完成整理事件。')
        evidence = {str(source_path): detail['input_file_sha256'],
                    str(root/body_path): hashlib.sha256(raw).hexdigest(),
                    str(root/item['group']/'metadata.json'): hashlib.sha256(meta_raw).hexdigest()}
        return dict(item, text=text, body_sha256=hashlib.sha256(raw).hexdigest(),
                    publication_year=year, released_at=release, ingested_at=completed[0],
                    brief_visibility=meta.get('brief_visibility', 'exclude'),
                    completeness=meta.get('source_completeness', 'unknown'),
                    content_nature=meta.get('content_nature', 'unknown'),
                    source_path=body_path, excluded_ranges=excluded, evidence=evidence)

    def verify(self, hits):
        """Re-read selected originals at the model and delivery boundaries."""
        sources = {key: self.get(key) for key in dict.fromkeys(h['material_id'] for h in hits)}
        evidence = {}
        for hit in hits:
            source = sources[hit['material_id']]
            a, z = hit['char_start'], hit['char_end']
            if (source['body_sha256'] != hit['body_sha256'] or source['binding'] != hit['binding']
                    or source['text'][a:z] != hit['text']
                    or any(a < r['char_end'] and r['char_start'] < z for r in source['excluded_ranges'])):
                raise ContractError('检索依据已失效，请重新查询。')
            evidence.update(source['evidence'])
        return evidence
