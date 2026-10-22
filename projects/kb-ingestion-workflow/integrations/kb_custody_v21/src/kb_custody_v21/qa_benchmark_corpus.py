"""Read explicitly selected local ZIP text for an isolated evaluation index.

This is not the Feishu ingestion provider. It never extracts, approves, moves,
edits or promotes source material, and never infers source authority or dates.
"""
import hashlib
from pathlib import PurePosixPath
import stat
import unicodedata
import zipfile

from .contracts import ContractError
from .qa_test_design import ROLES


def _member_name(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.rstrip('/').split('/'))
            or ':' in value.split('/')[0]):
        raise ContractError('unsafe benchmark ZIP member')
    return value


def _metadata(name):
    return any(p == '__MACOSX' or p == '.DS_Store' or p.startswith('._') for p in PurePosixPath(name).parts)


class BenchmarkCorpus:
    """Context-managed provider compatible with QAIndex, selected text only.

    Selection fields: id/path/title/role/canonical_id. Canonical IDs group a
    source with its derived copies; they require review rather than guessing.
    Optional body_sha256 pins decoded UTF-8 text for a previously reviewed case.
    """
    def __init__(self, archive, selection, *, filename_encoding='utf-8',
                 max_entry_bytes=8_000_000, max_total_bytes=64_000_000):
        if (not isinstance(selection, list) or not 1 <= len(selection) <= 5000
                or type(max_entry_bytes) is not int or max_entry_bytes < 1
                or type(max_total_bytes) is not int or max_total_bytes < 1):
            raise ContractError('invalid benchmark selection or limits')
        self.archive, self.filename_encoding = archive, filename_encoding
        self._selected, selected_paths = {}, set()
        for row in selection:
            if (not isinstance(row, dict)
                    or not {'id','path','title','role','canonical_id'} <= set(row)
                    or set(row)-{'id','path','title','role','canonical_id','body_sha256'}
                    or any(not isinstance(row[k], str) or not row[k].strip()
                           for k in ('id','path','title','role','canonical_id'))
                    or row['role'] not in ROLES):
                raise ContractError('explicit benchmark identity and role required')
            path=_member_name(row['path'])
            digest=row.get('body_sha256')
            if digest is not None and (not isinstance(digest,str) or len(digest)!=64
                                      or any(c not in '0123456789abcdef' for c in digest)):
                raise ContractError('invalid benchmark text digest')
            if row['id'] in self._selected or path in selected_paths or _metadata(path):
                raise ContractError('duplicate or metadata-only benchmark selection')
            if PurePosixPath(path).suffix.lower() not in {'.md','.txt'}:
                raise ContractError('benchmark reader supports existing UTF-8 text only')
            self._selected[row['id']]=dict(row)
            selected_paths.add(path)
        self.max_entry_bytes, self.max_total_bytes = max_entry_bytes, max_total_bytes
        self._zip=None

    def __enter__(self):
        if self._zip is not None:
            raise ContractError('benchmark reader is already open')
        archive=zipfile.ZipFile(self.archive, metadata_encoding=self.filename_encoding)
        try:
            members, normalized = {}, set()
            for info in archive.infolist():
                name=_member_name(info.filename)
                canonical=unicodedata.normalize('NFC',name).casefold()
                if canonical in normalized:
                    raise ContractError('ambiguous benchmark ZIP member names')
                normalized.add(canonical)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ContractError('benchmark ZIP contains a symbolic link')
                if not info.is_dir() and not _metadata(name):
                    members[name]=info
            total=0
            for row in self._selected.values():
                info=members.get(row['path'])
                if info is None or info.flag_bits & 1:
                    raise ContractError('missing or encrypted benchmark text')
                if info.file_size > self.max_entry_bytes:
                    raise ContractError('benchmark text exceeds entry limit')
                total+=info.file_size
            if total > self.max_total_bytes:
                raise ContractError('selected benchmark text exceeds total limit')
        except BaseException:
            archive.close()
            raise
        self._zip, self._members = archive, members
        return self

    def __exit__(self, *exc):
        self._zip.close()
        self._zip=None

    def ids(self):
        if self._zip is None:
            raise ContractError('open benchmark reader before use')
        return sorted(self._selected)

    def get(self, key):
        if self._zip is None or key not in self._selected:
            raise ContractError('benchmark source is not selected or reader is closed')
        row=self._selected[key]
        with self._zip.open(self._members[row['path']]) as f:
            raw=f.read(self.max_entry_bytes+1)
        if len(raw)>self.max_entry_bytes:
            raise ContractError('benchmark decoded entry exceeds limit')
        try:
            body=raw.decode('utf-8-sig')
        except UnicodeDecodeError as error:
            raise ContractError('benchmark text must already be valid UTF-8') from error
        if not body.strip() or '\x00' in body:
            raise ContractError('empty or binary benchmark text')
        digest=hashlib.sha256(body.encode('utf-8')).hexdigest()
        if row.get('body_sha256') is not None and row['body_sha256']!=digest:
            raise ContractError('benchmark text changed since evidence review')
        return dict(material_id=key,document_id=row['canonical_id'],title=row['title'],text=body,
            body_sha256=digest,source_sha256=hashlib.sha256(raw).hexdigest(),
            binding=dict(kind='read_only_benchmark',member=row['path'],canonical_id=row['canonical_id']),
            source_role=row['role'],keywords=[],excluded_ranges=[],is_paper=False,
            publication_year=None,released_at=None,ingested_at=None,
            brief_visibility=False,completeness='not_assessed',content_nature=row['role'],
            source_path=row['path'],evidence=[],benchmark_only=True)
