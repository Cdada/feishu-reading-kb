"""Stopped-writer, streaming ZIP64 content snapshots and isolated restores.

Only the managed content tree and selected portable observations are exported.
An external SHA-256 is required before reading an archive. No runtime restore,
credential export, automatic upload, deletion, or live configuration switch.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unicodedata
from uuid import uuid4
import zipfile

from .content_snapshot import MAX_RECORD, export_catalog, validate_content
from .contracts import ContractError
from .manual_backup import local_file
from .registry import encoded, now
from .runtime import no_symlinks
from .safe_publish import directory, open_root, parts, rename_exclusive, root_identity
from .storage_layout import content_relative
from .storage_migration import (BLOCK, digest_file, evidence_at, inventory, regular_at,
                                stamp, state_evidence, stopped_writers, verify_source)

REFERENCE = 'P44-content-backup-v1'
RESERVE = 1024 ** 3
META = '.content-backup/'
SNAPSHOT_FILES = ('files.jsonl', 'catalog.jsonl', 'README.md')
README = '''# 知识库内容恢复副本

资料原件及所有已保存的转换、导读、PDF 和整理成果按原相对路径保留。
打开 reading-library 中的来源卡片、知识卡片、主题索引和简报即可阅读。
files.jsonl 是内容清单；catalog.jsonl 保存稳定编号、状态及必要历史。
未下载成功的文件只有状态记录；没有把它们标为已备份的原件。

这是内容恢复，不含凭据、会话、完整运行队列或可启动的服务配置。
恢复不会清零累计，不会重放发送、模型调用或入库操作。
使用服务备份工具及保存在包外的 SHA-256 验证 ZIP，再恢复到新目录。
与原件处于同一物理磁盘的备份无法覆盖整盘损坏。请单独保管摘要回执。
本工具没有百度同步、定时备份或自动删除原件功能。
'''


def absolute(path):
    value = Path(path)
    if not value.is_absolute() or '..' in value.parts:
        raise ContractError('explicit absolute path without traversal required')
    return no_symlinks(value)


def enough_space(fd, required):
    usage = os.fstatvfs(fd)
    free = usage.f_bavail * usage.f_frsize
    if free < required + RESERVE:
        raise ContractError('insufficient space for content and the 1 GiB reserve')
    return free


def new_file(parent, name):
    if len(parts(name)) != 1:
        raise ContractError('new file requires a single safe name')
    return os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)


def write_json(path, value):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write((encoded(value) + '\n').encode())
        stream.flush(); os.fsync(stream.fileno())


def new_index(path):
    with path.open('xb'):
        pass
    path.chmod(0o600)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE items(path TEXT PRIMARY KEY,path_key TEXT UNIQUE NOT NULL,
        kind TEXT NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,source_stamp TEXT);''')
    return db


def insert_item(db, path, kind, size, digest, binding=None):
    parts(path)
    if not content_relative(path) or kind not in ('file', 'directory') or type(size) is not int or size < 0:
        raise ContractError('invalid content inventory item')
    if not isinstance(digest, str) or kind == 'file' and not re.fullmatch('[0-9a-f]{64}', digest) or kind == 'directory' and (size != 0 or digest != ''):
        raise ContractError('invalid content inventory digest')
    db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
        (path, unicodedata.normalize('NFC', path).casefold(), kind, size, digest, json.dumps(binding)))


def snapshot_info(folder):
    return {name: dict(bytes=(folder / name).stat().st_size, sha256=digest_file(folder / name)) for name in SNAPSHOT_FILES}


def estimate(db, snapshots):
    # ZIP_STORED does not expand file bodies. Reserve more than both ZIP64
    # headers plus UTF-8 filenames per entry, including all snapshot metadata.
    data = db.execute("SELECT coalesce(sum(bytes),0) FROM items WHERE kind='file'").fetchone()[0]
    overhead = sum(1024 + 4 * len(r['path'].encode()) for r in db.execute("SELECT path FROM items WHERE kind='file'"))
    return data + overhead + sum(v['bytes'] + 2048 for v in snapshots.values()) + 4096


def destination(runtime, bundle):
    bundle = absolute(bundle)
    if bundle.suffix.lower() != '.zip':
        raise ContractError('content backup destination must end in .zip')
    for source in (runtime.root, runtime.data_root):
        if bundle.is_relative_to(source) or source.is_relative_to(bundle):
            raise ContractError('backup must be outside operational and content directories')
    if bundle.exists() or bundle.with_name(bundle.name + '.receipt.json').exists():
        raise ContractError('backup or receipt already exists; preserve it and choose a new name')
    return bundle, root_identity(bundle.parent)


def plan_backup(runtime, bundle, *, hook=lambda point: None):
    runtime.gate(); runtime.check_storage()
    bundle, target_identity = destination(runtime, bundle)
    with stopped_writers(runtime):
        folder = runtime.ensure_directory('content-backup-plans') / uuid4().hex
        folder.mkdir(mode=0o700)
        db = new_index(folder / 'plan.sqlite3')
        identity = root_identity(runtime.data_root)
        source = open_root(runtime.data_root, identity)
        target = open_root(bundle.parent, target_identity)
        before = state_evidence(runtime)
        try:
            export_catalog(runtime, folder / 'catalog.jsonl')
            with db:
                for item in inventory(source):
                    insert_item(db, *item)
            with (folder / 'files.jsonl').open('xb') as output:
                os.chmod(folder / 'files.jsonl', 0o600)
                for row in db.execute('SELECT path,kind,bytes,sha256 FROM items ORDER BY path'):
                    output.write((encoded(dict(row)) + '\n').encode())
                output.flush(); os.fsync(output.fileno())
            (folder / 'README.md').write_text(README)
            (folder / 'README.md').chmod(0o600)
            validation = validate_content(source, db, folder / 'catalog.jsonl')
            hook('snapshot')
            verify_source(db, source)
            runtime.check_storage()
            if before != state_evidence(runtime) or identity != root_identity(runtime.data_root):
                raise ContractError('content or operational state changed while preparing backup')
            snapshots = snapshot_info(folder)
            required = estimate(db, snapshots)
            free = enough_space(target, required)
            meta = dict(reference=REFERENCE, created_at=now(), source=identity,
                runtime=root_identity(runtime.root), state_evidence=before,
                destination=str(bundle), destination_parent=target_identity, snapshots=snapshots,
                required_bytes=required, validation=validation,
                files=db.execute("SELECT count(*) FROM items WHERE kind='file'").fetchone()[0],
                total_bytes=db.execute('SELECT coalesce(sum(bytes),0) FROM items').fetchone()[0])
            with db:
                db.execute('INSERT INTO meta VALUES (?,?)', ('plan', encoded(meta)))
        finally:
            db.close(); os.close(source); os.close(target)
        return dict(plan=str(folder / 'plan.sqlite3'), plan_sha256=digest_file(folder / 'plan.sqlite3'),
                    files=meta['files'], total_bytes=meta['total_bytes'], destination=str(bundle),
                    required_free_bytes=required + RESERVE, free_bytes=free,
                    validation=validation, source_retained=True, runtime_restore=False)


@contextmanager
def checked_plan(runtime, plan, digest):
    plan = absolute(plan)
    if not plan.is_relative_to(runtime.root / 'content-backup-plans') or plan.name != 'plan.sqlite3' or digest_file(plan) != digest:
        raise ContractError('exact reviewed local content backup plan required')
    db = sqlite3.connect(plan.as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    try:
        meta = json.loads(db.execute("SELECT value FROM meta WHERE key='plan'").fetchone()[0])
        if meta['reference'] != REFERENCE or meta['runtime'] != root_identity(runtime.root) or meta['source'] != root_identity(runtime.data_root):
            raise ContractError('backup plan belongs to another content/runtime directory')
        if meta['state_evidence'] != state_evidence(runtime) or snapshot_info(plan.parent) != meta['snapshots']:
            raise ContractError('history or metadata changed; prepare a fresh backup plan')
        yield db, meta
        if digest_file(plan) != digest:
            raise ContractError('backup plan changed during use')
    finally:
        db.close()


def stream_member(archive, name, reader, size, digest):
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.file_size = size
    observed = hashlib.sha256(); count = 0
    with archive.open(info, 'w', force_zip64=True) as output:
        for chunk in iter(lambda: reader.read(BLOCK), b''):
            count += len(chunk)
            if count > size:
                raise ContractError('backup source grew during copy')
            observed.update(chunk); output.write(chunk)
    if count != size or observed.hexdigest() != digest:
        raise ContractError('backup source changed during copy')


def create_backup(runtime, plan, plan_sha256, *, hook=lambda point: None):
    runtime.gate(); runtime.check_storage()
    plan = absolute(plan)
    with stopped_writers(runtime), checked_plan(runtime, plan, plan_sha256) as (db, meta):
        bundle, identity = destination(runtime, meta['destination'])
        if identity != meta['destination_parent']:
            raise ContractError('backup destination directory changed')
        source = open_root(runtime.data_root, meta['source'])
        target = open_root(bundle.parent, identity)
        temporary = '.p44-' + uuid4().hex + '.partial'
        try:
            verify_source(db, source)
            enough_space(target, meta['required_bytes'])
            with os.fdopen(new_file(target, temporary), 'w+b') as output:
                with zipfile.ZipFile(output, 'w', allowZip64=True, compression=zipfile.ZIP_STORED) as archive:
                    for row in db.execute("SELECT * FROM items WHERE kind='file' ORDER BY path"):
                        runtime.check_storage()
                        with regular_at(source, row['path']) as (fd, before):
                            with os.fdopen(os.dup(fd), 'rb') as reader:
                                stream_member(archive, row['path'], reader, row['bytes'], row['sha256'])
                            if stamp(before) != stamp(os.fstat(fd)):
                                raise ContractError('backup source changed during copy')
                        hook('file')
                    for name, info in meta['snapshots'].items():
                        with local_file(plan.parent / name) as reader:
                            stream_member(archive, META + name, reader, info['bytes'], info['sha256'])
                output.flush(); os.fsync(output.fileno())
                output.seek(0)
                bundle_sha = hashlib.file_digest(output, 'sha256').hexdigest()
                size = os.fstat(output.fileno()).st_size
            hook('packed')
            verify_source(db, source)
            runtime.check_storage()
            if (state_evidence(runtime) != meta['state_evidence'] or snapshot_info(plan.parent) != meta['snapshots']
                    or root_identity(runtime.data_root) != meta['source'] or digest_file(plan) != plan_sha256):
                raise ContractError('snapshot changed; uncommitted backup retained for review')
            receipt = dict(reference=REFERENCE, created_at=now(), bundle_sha256=bundle_sha,
                bundle_size_bytes=size, files=meta['files'], total_bytes=meta['total_bytes'],
                validation=meta['validation'], runtime_restore=False, restore_test='not_run')
            rename_exclusive(target, temporary, target, bundle.name)
            with os.fdopen(new_file(target, bundle.name + '.receipt.json'), 'wb') as output:
                output.write((encoded(receipt) + '\n').encode()); output.flush(); os.fsync(output.fileno())
            os.fsync(target)
            return dict(receipt, bundle=str(bundle), receipt=str(bundle) + '.receipt.json', state='backup_created')
        finally:
            os.close(source); os.close(target)


def load_inventory(stream, db):
    while raw := stream.readline(MAX_RECORD + 1):
        if len(raw) > MAX_RECORD or not raw.endswith(b'\n'):
            raise ContractError('invalid or oversized backup inventory row')
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {'path', 'kind', 'bytes', 'sha256'}:
            raise ContractError('invalid backup inventory schema')
        insert_item(db, value['path'], value['kind'], value['bytes'], value['sha256'])
    db.commit()


def check_member(info):
    parts(info.filename)
    if (info.orig_filename != info.filename or info.is_dir() or info.flag_bits & 1
            or info.compress_type != zipfile.ZIP_STORED
            or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)):
        raise ContractError('unsafe or unsupported ZIP entry')


def checked_archive(archive, db):
    # zipfile keeps the ZIP central directory in memory; file bodies and the
    # inventory stay streamed/on disk. No full-library body dictionary exists.
    db.execute('CREATE TABLE members(path TEXT PRIMARY KEY,path_key TEXT UNIQUE NOT NULL)')
    for info in archive.infolist():
        check_member(info)
        db.execute('INSERT INTO members VALUES (?,?)', (info.filename, unicodedata.normalize('NFC', info.filename).casefold()))
        row = db.execute('SELECT * FROM items WHERE path=?', (info.filename,)).fetchone()
        if info.filename not in {META + n for n in SNAPSHOT_FILES}:
            if row is None or row['kind'] != 'file' or row['bytes'] != info.file_size:
                raise ContractError('ZIP entries disagree with content inventory')
    count = db.execute('SELECT count(*) FROM members').fetchone()[0]
    if count != db.execute("SELECT count(*)+3 FROM items WHERE kind='file'").fetchone()[0]:
        raise ContractError('backup archive is incomplete')
    for name in SNAPSHOT_FILES:
        if db.execute('SELECT 1 FROM members WHERE path=?', (META + name,)).fetchone() is None:
            raise ContractError('backup metadata is incomplete')


def restore_backup(bundle, sha256, target, *, hook=lambda point: None):
    """Verify a caller-retained external digest, then publish a new content tree."""
    if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ContractError('external trusted backup SHA-256 required')
    bundle, target = absolute(bundle), absolute(target)
    if target.exists() or bundle.is_relative_to(target):
        raise ContractError('restore requires a new, absent target outside the backup file')
    identity = root_identity(target.parent)
    parent = open_root(target.parent, identity)
    staging = '.p44-restore-' + uuid4().hex + '.partial'
    output_root = None
    try:
        with local_file(bundle) as reader, tempfile.TemporaryDirectory(prefix='p44-index-') as scratch:
            original_stamp = stamp(os.fstat(reader.fileno()))
            if hashlib.file_digest(reader, 'sha256').hexdigest() != sha256:
                raise ContractError('archive differs from external trusted digest')
            reader.seek(0)
            db = new_index(Path(scratch) / 'index.sqlite3')
            try:
                with zipfile.ZipFile(reader) as archive:
                    # Reject compressed/unsafe metadata before parsing its rows.
                    for info in archive.infolist():
                        check_member(info)
                    with archive.open(META + 'files.jsonl') as entries:
                        load_inventory(entries, db)
                    checked_archive(archive, db)
                    required = sum(info.file_size for info in archive.infolist())
                    enough_space(parent, required)
                    os.mkdir(staging, mode=0o700, dir_fd=parent)
                    output_root = directory(parent, staging)
                    for row in db.execute("SELECT path FROM items WHERE kind='directory' ORDER BY path"):
                        fd = directory(output_root, row['path'], create=True); os.close(fd)
                    for info in archive.infolist():
                        names = parts(info.filename)
                        folder = directory(output_root, '/'.join(names[:-1]), create=True) if len(names) > 1 else os.dup(output_root)
                        try:
                            observed = hashlib.sha256(); size = 0
                            with archive.open(info) as source, os.fdopen(new_file(folder, names[-1]), 'wb') as output:
                                for chunk in iter(lambda: source.read(BLOCK), b''):
                                    size += len(chunk)
                                    if size > info.file_size:
                                        raise ContractError('ZIP member exceeded its declared size')
                                    observed.update(chunk); output.write(chunk)
                                output.flush(); os.fsync(output.fileno())
                            row = db.execute('SELECT * FROM items WHERE path=?', (info.filename,)).fetchone()
                            if size != info.file_size or row is not None and observed.hexdigest() != row['sha256']:
                                raise ContractError('restored file differs from content digest')
                            os.fsync(folder)
                        finally:
                            os.close(folder)
                        hook('file')
                # Re-read restored bytes; a successful write alone is not restore evidence.
                for row in db.execute("SELECT * FROM items WHERE kind='file'"):
                    if evidence_at(output_root, row['path'])[:2] != (row['bytes'], row['sha256']):
                        raise ContractError('restored content failed read-back verification')
                catalog = target.parent / staging / META / 'catalog.jsonl'
                validation = validate_content(output_root, db, catalog)
                reader.seek(0)
                if hashlib.file_digest(reader, 'sha256').hexdigest() != sha256 or original_stamp != stamp(os.fstat(reader.fileno())):
                    raise ContractError('archive changed during restore')
                hook('verified')
                if root_identity(target.parent) != identity:
                    raise ContractError('restore destination parent changed')
                os.fsync(output_root)
                rename_exclusive(parent, staging, parent, target.name)
                return dict(state='restore_verified', target=str(target), bundle_sha256=sha256,
                    files=db.execute("SELECT count(*) FROM items WHERE kind='file'").fetchone()[0],
                    total_bytes=db.execute('SELECT coalesce(sum(bytes),0) FROM items').fetchone()[0],
                    validation=validation, runtime_restore=False, model_calls=0)
            finally:
                db.close()
    finally:
        if output_root is not None:
            os.close(output_root)
        os.close(parent)
